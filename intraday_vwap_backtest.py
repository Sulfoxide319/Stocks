#!/usr/bin/env python3
"""Backtest the short-term model with BaoStock 5-minute VWAP execution."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
import math
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import requests

from baostock_intraday import BaoStock5mClient, IntradayBar
from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from optimize_short_term_strategy import parse_float_list, parse_int_list, parse_str_list
from optimize_time_weighted_strategy import month_windows, recency_weights
from short_term_pattern_miner import PatternRow, event_score_by_symbol
from short_term_strategy_backtest import (
    ExecutedTrade,
    PlannedTrade,
    build_bar_maps,
    build_signal_rows,
    evaluate_regime,
    max_drawdown,
    passes_strategy,
    planned_selection_rank,
    planned_selection_score,
    plan_trade_from_signal,
)
from tech_event_backtest import PriceBar, fetch_yahoo_history
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date


def parse_clock(raw: str) -> dt.time:
    hour, minute = raw.split(":", 1)
    return dt.time(int(hour), int(minute))


def first_index_on_or_after(bars: list[PriceBar], date_value: dt.date) -> int | None:
    for index, bar in enumerate(bars):
        if bar.date >= date_value:
            return index
    return None


def final_holding_date(bars: list[PriceBar], entry_date: dt.date, horizon: int, end_date: dt.date) -> dt.date | None:
    entry_index = first_index_on_or_after(bars, entry_date)
    if entry_index is None:
        return None
    final_index = min(entry_index + horizon - 1, len(bars) - 1)
    return min(bars[final_index].date, end_date)


def next_trading_date_after(bars: list[PriceBar], date_value: dt.date) -> dt.date | None:
    for bar in bars:
        if bar.date > date_value:
            return bar.date
    return None


def close_on_or_before(bars: list[PriceBar], date_value: dt.date) -> float | None:
    result: float | None = None
    for bar in bars:
        if bar.date <= date_value:
            result = bar.close
        else:
            break
    return result


def selection_key(planned: PlannedTrade, mode: str) -> Any:
    features = planned.features
    if mode == "score_quality":
        return planned_selection_rank(planned)
    if mode == "quality":
        return planned_selection_score(planned)
    if mode == "score_low_heat":
        return (
            planned.score,
            -float(features.get("atr_pct") or 0.0),
            -float(features.get("momentum_10d_pct") or 0.0),
            -float(features.get("distance_to_ma5_pct") or 0.0),
            planned_selection_score(planned),
        )
    return planned.score


def passes_overheat_filters(row: PatternRow, args: argparse.Namespace) -> bool:
    if args.max_atr_pct > 0 and row.atr_pct > args.max_atr_pct:
        return False
    if args.max_5d_range_pct > 0 and row.max_5d_range_pct > args.max_5d_range_pct:
        return False
    if args.max_momentum_10d_pct < 999 and row.momentum_10d_pct > args.max_momentum_10d_pct:
        return False
    if args.max_close_position_20d_pct < 100 and row.close_position_20d_pct > args.max_close_position_20d_pct:
        return False
    if args.max_distance_to_20d_high_pct < 999 and row.distance_to_20d_high_pct > args.max_distance_to_20d_high_pct:
        return False
    return True


def moving_average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def bar_index_at_or_before(bars: list[PriceBar], date_value: dt.date) -> int | None:
    found: int | None = None
    for index, bar in enumerate(bars):
        if bar.date <= date_value:
            found = index
        else:
            break
    return found


def market_temperature(price_map: dict[str, list[PriceBar]], date_value: dt.date) -> dict[str, Any]:
    above_ma20 = 0
    return_5d_values: list[float] = []
    return_20d_values: list[float] = []
    eligible = 0
    for bars in price_map.values():
        index = bar_index_at_or_before(bars, date_value)
        if index is None or index < 20:
            continue
        close = bars[index].close
        if close <= 0:
            continue
        eligible += 1
        ma20 = moving_average([bar.close for bar in bars[index - 19 : index + 1]])
        if close >= ma20:
            above_ma20 += 1
        if index >= 5 and bars[index - 5].close:
            return_5d_values.append(close / bars[index - 5].close - 1)
        if index >= 20 and bars[index - 20].close:
            return_20d_values.append(close / bars[index - 20].close - 1)
    breadth = above_ma20 / eligible if eligible else 0.0
    avg_5d = moving_average(return_5d_values)
    avg_20d = moving_average(return_20d_values)
    if breadth >= 0.58 and avg_5d >= 0.01 and avg_20d >= 0:
        state = "hot"
    elif breadth <= 0.42 or avg_5d <= -0.02 or avg_20d <= -0.04:
        state = "cold"
    else:
        state = "normal"
    return {
        "state": state,
        "breadth_ma20": breadth,
        "avg_5d_return": avg_5d,
        "avg_20d_return": avg_20d,
        "eligible_symbols": eligible,
    }


def dynamic_param_overrides(state: str, args: argparse.Namespace) -> dict[str, float]:
    if not getattr(args, "dynamic_params", False):
        return {}
    if state == "hot":
        return {
            "max_gap_up": args.hot_max_gap_up,
            "gap_volume_min_ratio": args.hot_gap_volume_min_ratio,
            "max_5d_range_pct": args.hot_max_5d_range_pct,
            "max_momentum_10d_pct": args.hot_max_momentum_10d_pct,
            "max_close_position_20d_pct": args.hot_max_close_position_20d_pct,
        }
    if state == "cold":
        return {
            "max_gap_up": args.cold_max_gap_up,
            "gap_volume_min_ratio": args.cold_gap_volume_min_ratio,
            "max_5d_range_pct": args.cold_max_5d_range_pct,
            "max_momentum_10d_pct": args.cold_max_momentum_10d_pct,
            "max_close_position_20d_pct": args.cold_max_close_position_20d_pct,
        }
    return {
        "max_gap_up": args.normal_max_gap_up,
        "gap_volume_min_ratio": args.normal_gap_volume_min_ratio,
        "max_5d_range_pct": args.normal_max_5d_range_pct,
        "max_momentum_10d_pct": args.normal_max_momentum_10d_pct,
        "max_close_position_20d_pct": args.normal_max_close_position_20d_pct,
    }


def planned_passes_dynamic_filters(planned: PlannedTrade, overrides: dict[str, float]) -> bool:
    features = planned.features
    max_range = float(overrides.get("max_5d_range_pct", 0.0) or 0.0)
    max_momentum = float(overrides.get("max_momentum_10d_pct", 999.0) or 999.0)
    max_position = float(overrides.get("max_close_position_20d_pct", 100.0) or 100.0)
    if max_range > 0 and float(features.get("max_5d_range_pct") or 0.0) > max_range:
        return False
    if max_momentum < 999 and float(features.get("momentum_10d_pct") or 0.0) > max_momentum:
        return False
    if max_position < 100 and float(features.get("close_position_20d_pct") or 0.0) > max_position:
        return False
    return True


def build_planned_by_entry(
    signal_rows: list[PatternRow],
    price_map: dict[str, list[PriceBar]],
    end_date: dt.date,
    args: argparse.Namespace,
) -> dict[dt.date, list[PlannedTrade]]:
    setup_allow = {item.strip() for item in args.setups.split(",") if item.strip()}
    planned_by_entry: dict[dt.date, list[PlannedTrade]] = {}
    for row in signal_rows:
        if not passes_strategy(
            row,
            args.min_score,
            setup_allow,
            args.ma5_mode,
            args.ma5_pullback_limit,
            args.ma5_extension_limit,
            args.sector_mode,
            args.min_sector_momentum_5d,
            args.min_sector_above_ma20_ratio,
        ):
            continue
        if not passes_overheat_filters(row, args):
            continue
        planned = plan_trade_from_signal(
            row,
            price_map.get(row.ticker, []),
            args.horizon,
            args.take_profit,
            args.hard_stop,
            args.trailing_stop,
            args.dynamic_exit,
            args.target_atr_mult,
            args.target_range_mult,
            args.event_bonus,
            args.target_min,
            args.target_max,
            args.stop_atr_mult,
            args.stop_min,
            args.stop_max,
            args.trail_atr_mult,
            args.trail_min,
            args.trail_max,
            end_date,
            "open",
        )
        if planned:
            planned_by_entry.setdefault(planned.entry_date, []).append(planned)
    for date_value in planned_by_entry:
        planned_by_entry[date_value].sort(key=lambda item: selection_key(item, args.selection_mode), reverse=True)
    return planned_by_entry


def day_vwap(bar: IntradayBar, totals: dict[str, float]) -> float:
    totals["amount"] += bar.amount
    totals["volume"] += bar.volume
    if totals["volume"] <= 0:
        return bar.close
    return totals["amount"] / totals["volume"]


def intraday_vwap_by_moment(bars: list[IntradayBar]) -> dict[dt.datetime, float]:
    values: dict[dt.datetime, float] = {}
    current_date: dt.date | None = None
    totals = {"amount": 0.0, "volume": 0.0}
    for bar in sorted(bars, key=lambda item: item.moment):
        if current_date != bar.date:
            current_date = bar.date
            totals = {"amount": 0.0, "volume": 0.0}
        values[bar.moment] = day_vwap(bar, totals)
    return values


def refine_trade_with_5m(
    planned: PlannedTrade,
    daily_bars: list[PriceBar],
    intraday_bars: list[IntradayBar],
    horizon: int,
    end_date: dt.date,
    entry_start: dt.time,
    entry_end: dt.time,
    max_gap_up: float,
    max_gap_down: float,
    gap_volume_threshold: float,
    gap_volume_min_ratio: float,
    confirm_buffer: float,
    vwap_buffer: float,
    max_entry_extension: float,
    vwap_fail_bars: int,
    vwap_fail_buffer: float,
    max_extension_days: int,
    extend_profit_threshold: float,
) -> PlannedTrade | None:
    base_final_date = final_holding_date(daily_bars, planned.entry_date, horizon, end_date)
    if base_final_date is None:
        return None
    final_date = base_final_date
    if max_extension_days > 0:
        final_date = final_holding_date(daily_bars, planned.entry_date, horizon + max_extension_days, end_date) or base_final_date
    signal_close = close_on_or_before(daily_bars, planned.signal_date)
    if not signal_close:
        return None
    bars = [
        bar
        for bar in intraday_bars
        if planned.entry_date <= bar.date <= final_date and dt.time(9, 30) <= bar.time <= dt.time(15, 0)
    ]
    if not bars:
        return None
    vwap_by_moment = intraday_vwap_by_moment(bars)
    first_entry_day_bar = next((bar for bar in bars if bar.date == planned.entry_date), None)
    if not first_entry_day_bar:
        return None
    gap_pct = first_entry_day_bar.open / signal_close - 1
    if gap_pct > max_gap_up or gap_pct < -max_gap_down:
        return None
    if gap_volume_min_ratio > 0 and gap_pct > gap_volume_threshold:
        value_ratio = float(planned.features.get("traded_value_ratio") or 0.0)
        if value_ratio < gap_volume_min_ratio:
            return None

    entry_bar: IntradayBar | None = None
    entry_vwap = 0.0
    for bar in [item for item in bars if item.date == planned.entry_date]:
        current_vwap = vwap_by_moment.get(bar.moment, bar.close)
        if bar.time < entry_start or bar.time > entry_end:
            continue
        confirm_price = max(signal_close * (1 + confirm_buffer), current_vwap * (1 + vwap_buffer))
        if bar.close < confirm_price:
            continue
        if bar.close / signal_close - 1 > max_entry_extension:
            continue
        entry_bar = bar
        entry_vwap = current_vwap
        break
    if entry_bar is None:
        return None

    entry_price = entry_bar.close
    first_sell_date = next_trading_date_after(daily_bars, planned.entry_date)
    if first_sell_date is None or first_sell_date > final_date:
        return None
    best_high = entry_price
    exit_bar = bars[-1]
    exit_price = exit_bar.close
    exit_reason = "time_exit_5m"
    below_vwap_count = 0
    base_exit_bar = next((bar for bar in reversed(bars) if bar.date <= base_final_date), bars[-1])
    extension_checked = False

    for bar in bars:
        if bar.moment <= entry_bar.moment:
            continue
        current_vwap = vwap_by_moment.get(bar.moment, bar.close)
        if bar.high > best_high:
            best_high = bar.high
        if bar.date < first_sell_date:
            continue
        if bar.moment > base_exit_bar.moment and not extension_checked:
            base_vwap = vwap_by_moment.get(base_exit_bar.moment, base_exit_bar.close)
            extension_allowed = (
                max_extension_days > 0
                and best_high >= entry_price * (1 + extend_profit_threshold)
                and base_exit_bar.close >= entry_price
                and base_exit_bar.close >= base_vwap
            )
            extension_checked = True
            if not extension_allowed:
                exit_bar = base_exit_bar
                exit_price = base_exit_bar.close
                exit_reason = "time_exit_5m"
                break
        if bar.low <= entry_price * (1 - planned.hard_stop_pct):
            exit_bar = bar
            exit_price = entry_price * (1 - planned.hard_stop_pct)
            exit_reason = "hard_stop_5m"
            break
        if bar.high >= entry_price * (1 + planned.target_pct):
            exit_bar = bar
            exit_price = entry_price * (1 + planned.target_pct)
            exit_reason = "take_profit_5m"
            break
        if best_high >= entry_price * (1 + max(0.04, planned.target_pct * 0.4)):
            trailing_price = best_high * (1 - planned.trailing_stop_pct)
            if bar.low <= trailing_price:
                exit_bar = bar
                exit_price = trailing_price
                exit_reason = "trailing_stop_5m"
                break
        if bar.close < current_vwap * (1 - vwap_fail_buffer) and bar.close < entry_price:
            below_vwap_count += 1
        else:
            below_vwap_count = 0
        if vwap_fail_bars > 0 and below_vwap_count >= vwap_fail_bars:
            exit_bar = bar
            exit_price = bar.close
            exit_reason = "vwap_fail_5m"
            break

    if exit_bar.date > end_date:
        return None
    features = dict(planned.features)
    features["intraday_entry_time"] = entry_bar.time.isoformat(timespec="minutes")
    features["intraday_exit_time"] = exit_bar.time.isoformat(timespec="minutes")
    features["entry_day_vwap"] = round(entry_vwap, 4)
    features["entry_gap_pct"] = round(gap_pct * 100, 4)
    return replace(
        planned,
        entry_price=entry_price,
        exit_date=exit_bar.date,
        exit_price=exit_price,
        return_pct=(exit_price / entry_price - 1) * 100 if entry_price else 0.0,
        exit_reason=exit_reason,
        features=features,
    )


def simulate_intraday_portfolio(
    planned_by_entry: dict[dt.date, list[PlannedTrade]],
    price_map: dict[str, list[PriceBar]],
    intraday_map: dict[str, list[IntradayBar]],
    start_date: dt.date,
    end_date: dt.date,
    args: argparse.Namespace,
) -> tuple[list[ExecutedTrade], list[dict[str, Any]], float]:
    trading_dates = sorted(
        {
            bar.date
            for bars in price_map.values()
            for bar in bars
            if start_date <= bar.date <= end_date
        }
    )
    cash = args.initial_cash
    open_positions: list[tuple[PlannedTrade, float]] = []
    trades: list[ExecutedTrade] = []
    equity_curve: list[dict[str, Any]] = []
    fee_rate = args.fee_bps / 10000
    bar_maps = build_bar_maps(price_map)
    cooldown_until: dt.date | None = None
    symbol_cooldown_until: dict[str, dt.date] = {}
    last_regime_trade_count = 0
    entry_start = parse_clock(args.entry_start_time)
    entry_end = parse_clock(args.entry_end_time)

    for date_value in trading_dates:
        temperature = market_temperature(price_map, date_value)
        overrides = dynamic_param_overrides(str(temperature["state"]), args)
        active_max_gap_up = float(overrides.get("max_gap_up", args.max_gap_up))
        active_gap_volume_min_ratio = float(overrides.get("gap_volume_min_ratio", args.gap_volume_min_ratio))
        regime, cooldown_until = evaluate_regime(
            date_value,
            trades,
            equity_curve,
            args.initial_cash,
            cooldown_until,
            args.regime_filter,
            args.regime_mode,
            args.regime_lookback_trades,
            args.regime_min_trades,
            args.regime_min_win_rate,
            args.regime_max_hard_stop_rate,
            args.regime_max_drawdown,
            [],
            args.market_ma_days,
            args.market_lookback_days,
            args.market_min_return,
            last_regime_trade_count,
        )
        has_fresh_risk = any(not reason.startswith("cooldown_until=") for reason in regime.reasons)
        if regime.state == "risk_off" and args.regime_filter and args.regime_cooldown_days > 0 and has_fresh_risk:
            cooldown_until = max(cooldown_until or date_value, date_value + dt.timedelta(days=args.regime_cooldown_days))
            last_regime_trade_count = len(trades)

        candidates = planned_by_entry.get(date_value, [])
        for raw_planned in candidates:
            if regime.action == "skip":
                break
            if len(open_positions) >= args.max_positions:
                break
            if any(position[0].ticker == raw_planned.ticker for position in open_positions):
                continue
            if args.symbol_cooldown_days > 0 and date_value <= symbol_cooldown_until.get(raw_planned.ticker, dt.date.min):
                continue
            if not planned_passes_dynamic_filters(raw_planned, overrides):
                continue
            planned = refine_trade_with_5m(
                raw_planned,
                price_map.get(raw_planned.ticker, []),
                intraday_map.get(raw_planned.ticker, []),
                args.horizon,
                end_date,
                entry_start,
                entry_end,
                active_max_gap_up,
                args.max_gap_down,
                args.gap_volume_threshold,
                active_gap_volume_min_ratio,
                args.confirm_buffer,
                args.vwap_buffer,
                args.max_entry_extension,
                args.vwap_fail_bars,
                args.vwap_fail_buffer,
                args.max_extension_days,
                args.extend_profit_threshold,
            )
            if not planned or cash <= 0:
                continue
            slots = max(1, args.max_positions - len(open_positions))
            capital = cash / slots
            if regime.action == "reduce":
                capital *= max(0.0, min(1.0, args.regime_risk_factor))
            shares = capital * (1 - fee_rate) / planned.entry_price
            cash -= capital
            open_positions.append((planned, shares))

        still_open: list[tuple[PlannedTrade, float]] = []
        for planned, shares in open_positions:
            if planned.exit_date <= date_value:
                cash += shares * planned.exit_price * (1 - fee_rate)
                trades.append(
                    ExecutedTrade(
                        ticker=planned.ticker,
                        name=planned.name,
                        setup_type=planned.setup_type,
                        signal_date=planned.signal_date.isoformat(),
                        entry_date=f"{planned.entry_date.isoformat()} {planned.features.get('intraday_entry_time', '')}".strip(),
                        exit_date=f"{planned.exit_date.isoformat()} {planned.features.get('intraday_exit_time', '')}".strip(),
                        entry_price=round(planned.entry_price, 4),
                        exit_price=round(planned.exit_price, 4),
                        return_pct=round(planned.return_pct - 2 * fee_rate * 100, 4),
                        exit_reason=planned.exit_reason,
                        target_pct=round(planned.target_pct * 100, 2),
                        hard_stop_pct=round(planned.hard_stop_pct * 100, 2),
                        trailing_stop_pct=round(planned.trailing_stop_pct * 100, 2),
                        score=planned.score,
                        cash_after=round(cash, 2),
                        selection_features=json.dumps(planned.features, ensure_ascii=False, sort_keys=True),
                    )
                )
                if (
                    args.symbol_cooldown_days > 0
                    and planned.return_pct <= 0
                    and planned.exit_reason in {"vwap_fail_5m", "hard_stop", "intraday_fail"}
                ):
                    symbol_cooldown_until[planned.ticker] = date_value + dt.timedelta(days=args.symbol_cooldown_days)
            else:
                still_open.append((planned, shares))
        open_positions = still_open

        marked = cash
        for planned, shares in open_positions:
            bar = bar_maps.get(planned.ticker, {}).get(date_value)
            marked += shares * (bar.close if bar else planned.entry_price)
        equity_curve.append(
            {
                "date": date_value.isoformat(),
                "equity": round(marked, 2),
                "cash": round(cash, 2),
                "open_positions": len(open_positions),
                "regime_state": regime.state,
                "regime_action": regime.action,
                "regime_reasons": ";".join(regime.reasons),
                "market_state": temperature["state"],
                "market_breadth_ma20": round(float(temperature["breadth_ma20"]), 4),
                "market_avg_5d_return": round(float(temperature["avg_5d_return"]) * 100, 4),
                "dynamic_max_gap_up": active_max_gap_up,
                "dynamic_gap_volume_min_ratio": active_gap_volume_min_ratio,
                "dynamic_max_5d_range_pct": overrides.get("max_5d_range_pct", args.max_5d_range_pct),
                "dynamic_max_momentum_10d_pct": overrides.get("max_momentum_10d_pct", args.max_momentum_10d_pct),
                "dynamic_max_close_position_20d_pct": overrides.get("max_close_position_20d_pct", args.max_close_position_20d_pct),
                "entry_candidates": len(candidates),
            }
        )
    final_equity = equity_curve[-1]["equity"] if equity_curve else args.initial_cash
    return trades, equity_curve, final_equity


def summarize_result(trades: list[ExecutedTrade], curve: list[dict[str, Any]], final_equity: float, initial_cash: float) -> dict[str, float]:
    total_return = (final_equity / initial_cash - 1) * 100
    wins = sum(1 for trade in trades if trade.return_pct > 0)
    return {
        "return_pct": round(total_return, 4),
        "drawdown_pct": round(max_drawdown(curve, initial_cash), 4),
        "trades": float(len(trades)),
        "win_rate_pct": round(wins / len(trades) * 100, 4) if trades else 0.0,
        "avg_trade_pct": round(sum(trade.return_pct for trade in trades) / len(trades), 4) if trades else 0.0,
    }


def write_trades(path: Path, trades: list[ExecutedTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(trades[0]).keys()) if trades else [])
        if trades:
            writer.writeheader()
            for trade in trades:
                writer.writerow(asdict(trade))


def write_curve(path: Path, curve: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(curve[0].keys()) if curve else [])
        if curve:
            writer.writeheader()
            writer.writerows(curve)


def write_report(
    path: Path,
    trades_csv: Path,
    best: dict[str, Any],
    monthly_rows: list[dict[str, Any]],
    trades: list[ExecutedTrade],
    args: argparse.Namespace,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# BaoStock 5m VWAP Backtest - {args.start_date} to {args.end_date}",
        "",
        "Daily signals are selected by the existing time-weighted A-share model; entries and exits are executed with BaoStock 5-minute bars and VWAP rules.",
        "",
        f"- Total return: `{best['full_return_pct']:.2f}%`",
        f"- Max drawdown: `{best['full_drawdown_pct']:.2f}%`",
        f"- Trades: `{int(best['full_trades'])}`",
        f"- Win rate: `{best['full_win_rate_pct']:.2f}%`",
        f"- Weighted monthly return: `{best['weighted_return_pct']:.2f}%`",
        f"- Monthly compound: `{best['monthly_compound_pct']:.2f}%`",
        f"- Objective: `{best['objective']:.2f}`",
        f"- Trades CSV: `{trades_csv}`",
        f"- Dynamic params: `{'on' if getattr(args, 'dynamic_params', False) else 'off'}`",
        "",
        "## Final Intraday Rules",
        "",
        f"- Entry window: `{args.entry_start_time}` to `{best['entry_end_time']}`",
        "- Exit constraint: A-share `T+1`; positions bought today cannot be sold until the next trading day",
        f"- Max gap up/down: `{best['max_gap_up']:.1%}` / `{best['max_gap_down']:.1%}`",
        f"- Gap-volume guard: if open gap > `{best.get('gap_volume_threshold', 0.0):.1%}`, require signal-day value ratio >= `{best.get('gap_volume_min_ratio', 0.0):.2f}`",
        f"- Confirm price: `max(signal close * (1 + {best['confirm_buffer']:.1%}), VWAP * (1 + {best['vwap_buffer']:.1%}))`",
        f"- Max entry extension over signal close: `{best['max_entry_extension']:.1%}`",
        f"- VWAP fail exit: `{int(best['vwap_fail_bars'])}` consecutive 5m closes below VWAP and below entry",
        f"- Strong-trend extension: up to `{int(best.get('max_extension_days', getattr(args, 'max_extension_days', 0)))}` extra trading days after `{args.horizon}` days if profit and VWAP trend remain positive",
    ]
    if getattr(args, "dynamic_params", False):
        lines.extend(
            [
                f"- Hot profile: gap<={args.hot_max_gap_up:.1%}, value_ratio>={args.hot_gap_volume_min_ratio:.2f}, range5<={args.hot_max_5d_range_pct:.1f}, momentum10<={args.hot_max_momentum_10d_pct:.1f}, pos20<={args.hot_max_close_position_20d_pct:.1f}",
                f"- Normal profile: gap<={args.normal_max_gap_up:.1%}, value_ratio>={args.normal_gap_volume_min_ratio:.2f}, range5<={args.normal_max_5d_range_pct:.1f}, momentum10<={args.normal_max_momentum_10d_pct:.1f}, pos20<={args.normal_max_close_position_20d_pct:.1f}",
                f"- Cold profile: gap<={args.cold_max_gap_up:.1%}, value_ratio>={args.cold_gap_volume_min_ratio:.2f}, range5<={args.cold_max_5d_range_pct:.1f}, momentum10<={args.cold_max_momentum_10d_pct:.1f}, pos20<={args.cold_max_close_position_20d_pct:.1f}",
            ]
        )
    lines.extend(
        [
            "",
            "## Monthly Blind Test",
            "",
            "| Month | Return | Drawdown | Trades | Win Rate | Weight |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in monthly_rows:
        lines.append(
            f"| {row['month']} | {row['return_pct']:.2f}% | {row['drawdown_pct']:.2f}% | {int(row['trades'])} | {row['win_rate_pct']:.2f}% | {row['weight']:.4f} |"
        )
    lines.extend(
        [
            "",
            "## Trades",
            "",
            "| Ticker | Setup | Entry | Exit | Return | Reason | Score |",
            "|---|---|---|---|---:|---|---:|",
        ]
    )
    for trade in trades:
        lines.append(
            f"| {trade.ticker} | {trade.setup_type} | {trade.entry_date} @ {trade.entry_price:.2f} | {trade.exit_date} @ {trade.exit_price:.2f} | {trade.return_pct:.2f}% | {trade.exit_reason} | {trade.score:.1f} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_daily_context(args: argparse.Namespace) -> tuple[list[Any], dict[str, list[PriceBar]], dict[str, int]]:
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("dates must be YYYY-MM-DD")
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    event_scores = event_score_by_symbol(Path(args.events))
    fetch_start = start_date - dt.timedelta(days=120)
    fetch_end = end_date + dt.timedelta(days=args.horizon * 3 + 15)
    price_map: dict[str, list[PriceBar]] = {}
    for symbol in symbols:
        try:
            price_map[symbol.ticker] = fetch_yahoo_history(session, symbol.yahoo_symbol or symbol.ticker, fetch_start, fetch_end)
        except Exception:
            price_map[symbol.ticker] = []
        time.sleep(0.03)
    return symbols, price_map, event_scores


def result_for_args(
    symbols: list[Any],
    price_map: dict[str, list[PriceBar]],
    event_scores: dict[str, int],
    intraday_map: dict[str, list[IntradayBar]],
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[ExecutedTrade], list[dict[str, Any]], float]:
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("dates must be YYYY-MM-DD")
    windows = month_windows(start_date, end_date)
    weights = recency_weights(len(windows), args.decay)
    monthly_rows: list[dict[str, Any]] = []
    weighted_return = 0.0
    weighted_drawdown = 0.0
    monthly_returns: list[float] = []
    monthly_trades = 0
    monthly_wins = 0
    for (window_start, window_end, label), weight in zip(windows, weights):
        rows = build_signal_rows(symbols, price_map, window_start, window_end, args.horizon, event_scores, args.min_traded_value, args.take_profit, args.hard_stop, args.trailing_stop)
        planned = build_planned_by_entry(rows, price_map, window_end, args)
        trades, curve, final_equity = simulate_intraday_portfolio(planned, price_map, intraday_map, window_start, window_end, args)
        summary = summarize_result(trades, curve, final_equity, args.initial_cash)
        monthly_returns.append(summary["return_pct"])
        weighted_return += summary["return_pct"] * weight
        weighted_drawdown += summary["drawdown_pct"] * weight
        monthly_trades += int(summary["trades"])
        monthly_wins += sum(1 for trade in trades if trade.return_pct > 0)
        monthly_rows.append({"month": label, "weight": round(weight, 4), **summary})
    full_rows = build_signal_rows(symbols, price_map, start_date, end_date, args.horizon, event_scores, args.min_traded_value, args.take_profit, args.hard_stop, args.trailing_stop)
    full_planned = build_planned_by_entry(full_rows, price_map, end_date, args)
    full_trades, full_curve, full_equity = simulate_intraday_portfolio(full_planned, price_map, intraday_map, start_date, end_date, args)
    full_summary = summarize_result(full_trades, full_curve, full_equity, args.initial_cash)
    negative_months = sum(1 for ret in monthly_returns if ret < 0)
    worst_month = min(monthly_returns) if monthly_returns else 0.0
    monthly_compound = (math.prod(1 + ret / 100 for ret in monthly_returns) - 1) * 100 if monthly_returns else 0.0
    objective = weighted_return - args.drawdown_penalty * weighted_drawdown - args.negative_month_penalty * negative_months + args.worst_month_penalty * worst_month
    result = {
        "objective": round(objective, 4),
        "weighted_return_pct": round(weighted_return, 4),
        "weighted_drawdown_pct": round(weighted_drawdown, 4),
        "monthly_compound_pct": round(monthly_compound, 4),
        "worst_month_pct": round(worst_month, 4),
        "negative_months": negative_months,
        "monthly_trades": monthly_trades,
        "monthly_win_rate_pct": round(monthly_wins / monthly_trades * 100, 4) if monthly_trades else 0.0,
        "full_return_pct": full_summary["return_pct"],
        "full_drawdown_pct": full_summary["drawdown_pct"],
        "full_trades": int(full_summary["trades"]),
        "full_win_rate_pct": full_summary["win_rate_pct"],
        "entry_end_time": args.entry_end_time,
        "max_gap_up": args.max_gap_up,
        "max_gap_down": args.max_gap_down,
        "gap_volume_threshold": args.gap_volume_threshold,
        "gap_volume_min_ratio": args.gap_volume_min_ratio,
        "confirm_buffer": args.confirm_buffer,
        "vwap_buffer": args.vwap_buffer,
        "max_entry_extension": args.max_entry_extension,
        "vwap_fail_bars": args.vwap_fail_bars,
        "max_extension_days": args.max_extension_days,
        "extend_profit_threshold": args.extend_profit_threshold,
    }
    return result, monthly_rows, full_trades, full_curve, full_equity


def result_for_preplanned(
    windows: list[tuple[dt.date, dt.date, str]],
    weights: list[float],
    monthly_planned: dict[str, dict[dt.date, list[PlannedTrade]]],
    full_planned: dict[dt.date, list[PlannedTrade]],
    price_map: dict[str, list[PriceBar]],
    intraday_map: dict[str, list[IntradayBar]],
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[ExecutedTrade], list[dict[str, Any]], float]:
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("dates must be YYYY-MM-DD")
    monthly_rows: list[dict[str, Any]] = []
    weighted_return = 0.0
    weighted_drawdown = 0.0
    monthly_returns: list[float] = []
    monthly_trades = 0
    monthly_wins = 0
    for window_start, window_end, label in windows:
        trades, curve, final_equity = simulate_intraday_portfolio(
            monthly_planned.get(label, {}),
            price_map,
            intraday_map,
            window_start,
            window_end,
            args,
        )
        summary = summarize_result(trades, curve, final_equity, args.initial_cash)
        weight = weights[len(monthly_rows)]
        monthly_returns.append(summary["return_pct"])
        weighted_return += summary["return_pct"] * weight
        weighted_drawdown += summary["drawdown_pct"] * weight
        monthly_trades += int(summary["trades"])
        monthly_wins += sum(1 for trade in trades if trade.return_pct > 0)
        monthly_rows.append({"month": label, "weight": round(weight, 4), **summary})
    full_trades, full_curve, full_equity = simulate_intraday_portfolio(full_planned, price_map, intraday_map, start_date, end_date, args)
    full_summary = summarize_result(full_trades, full_curve, full_equity, args.initial_cash)
    negative_months = sum(1 for ret in monthly_returns if ret < 0)
    worst_month = min(monthly_returns) if monthly_returns else 0.0
    monthly_compound = (math.prod(1 + ret / 100 for ret in monthly_returns) - 1) * 100 if monthly_returns else 0.0
    objective = weighted_return - args.drawdown_penalty * weighted_drawdown - args.negative_month_penalty * negative_months + args.worst_month_penalty * worst_month
    result = {
        "objective": round(objective, 4),
        "weighted_return_pct": round(weighted_return, 4),
        "weighted_drawdown_pct": round(weighted_drawdown, 4),
        "monthly_compound_pct": round(monthly_compound, 4),
        "worst_month_pct": round(worst_month, 4),
        "negative_months": negative_months,
        "monthly_trades": monthly_trades,
        "monthly_win_rate_pct": round(monthly_wins / monthly_trades * 100, 4) if monthly_trades else 0.0,
        "full_return_pct": full_summary["return_pct"],
        "full_drawdown_pct": full_summary["drawdown_pct"],
        "full_trades": int(full_summary["trades"]),
        "full_win_rate_pct": full_summary["win_rate_pct"],
        "entry_end_time": args.entry_end_time,
        "max_gap_up": args.max_gap_up,
        "max_gap_down": args.max_gap_down,
        "gap_volume_threshold": args.gap_volume_threshold,
        "gap_volume_min_ratio": args.gap_volume_min_ratio,
        "confirm_buffer": args.confirm_buffer,
        "vwap_buffer": args.vwap_buffer,
        "max_entry_extension": args.max_entry_extension,
        "vwap_fail_bars": args.vwap_fail_bars,
    }
    return result, monthly_rows, full_trades, full_curve, full_equity


def prefetch_intraday(
    planned_by_entry: dict[dt.date, list[PlannedTrade]],
    args: argparse.Namespace,
) -> dict[str, list[IntradayBar]]:
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("dates must be YYYY-MM-DD")
    tickers = sorted({planned.ticker for plans in planned_by_entry.values() for planned in plans})
    intraday_map: dict[str, list[IntradayBar]] = {}
    with BaoStock5mClient(Path(args.baostock_cache_dir)) as client:
        for index, ticker in enumerate(tickers, 1):
            try:
                intraday_map[ticker] = client.fetch_5m(ticker, start_date, end_date + dt.timedelta(days=args.horizon * 5 + 10))
            except Exception as exc:
                print(f"warning: baostock fetch failed for {ticker}: {exc}")
                intraday_map[ticker] = []
            if index % 10 == 0:
                print(f"prefetched {index}/{len(tickers)} tickers")
    return intraday_map


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BaoStock 5m VWAP backtest for the A-share short-term model.")
    parser.add_argument("--watchlist", default="config/watchlist.a_share_expanded.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES), help="buyable ticker prefixes; use 'all' to disable")
    parser.add_argument("--events", default="output/tech_event_radar_20260702.json")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--decay", type=float, default=0.72)
    parser.add_argument("--drawdown-penalty", type=float, default=0.55)
    parser.add_argument("--negative-month-penalty", type=float, default=5.0)
    parser.add_argument("--worst-month-penalty", type=float, default=0.8)
    parser.add_argument("--horizon", type=int, default=3)
    parser.add_argument("--max-positions", type=int, default=2)
    parser.add_argument("--min-score", type=float, default=80.0)
    parser.add_argument("--selection-mode", choices=["score", "score_quality", "quality", "score_low_heat"], default="score")
    parser.add_argument("--setups", default="EVENT_PLUS_VOLATILITY,VOLUME_BREAKOUT,HIGH_VOLATILITY")
    parser.add_argument("--take-profit", type=float, default=0.10)
    parser.add_argument("--hard-stop", type=float, default=0.04)
    parser.add_argument("--trailing-stop", type=float, default=0.035)
    parser.add_argument("--dynamic-exit", action="store_true", default=True)
    parser.add_argument("--target-atr-mult", type=float, default=0.9)
    parser.add_argument("--target-range-mult", type=float, default=0.35)
    parser.add_argument("--event-bonus", type=float, default=0.02)
    parser.add_argument("--target-min", type=float, default=0.05)
    parser.add_argument("--target-max", type=float, default=0.18)
    parser.add_argument("--stop-atr-mult", type=float, default=0.55)
    parser.add_argument("--stop-min", type=float, default=0.025)
    parser.add_argument("--stop-max", type=float, default=0.07)
    parser.add_argument("--trail-atr-mult", type=float, default=0.25)
    parser.add_argument("--trail-min", type=float, default=0.025)
    parser.add_argument("--trail-max", type=float, default=0.06)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--min-traded-value", type=float, default=200_000_000)
    parser.add_argument("--regime-filter", action="store_true", default=True)
    parser.add_argument("--regime-mode", choices=["skip", "reduce"], default="skip")
    parser.add_argument("--regime-lookback-trades", type=int, default=12)
    parser.add_argument("--regime-min-trades", type=int, default=8)
    parser.add_argument("--regime-min-win-rate", type=float, default=0.30)
    parser.add_argument("--regime-max-hard-stop-rate", type=float, default=0.45)
    parser.add_argument("--regime-max-drawdown", type=float, default=0.06)
    parser.add_argument("--regime-cooldown-days", type=int, default=5)
    parser.add_argument("--regime-risk-factor", type=float, default=0.35)
    parser.add_argument("--symbol-cooldown-days", type=int, default=0)
    parser.add_argument("--market-ma-days", type=int, default=20)
    parser.add_argument("--market-lookback-days", type=int, default=5)
    parser.add_argument("--market-min-return", type=float, default=-0.04)
    parser.add_argument("--ma5-mode", choices=["ignore", "filter", "pullback"], default="ignore")
    parser.add_argument("--ma5-pullback-limit", type=float, default=0.025)
    parser.add_argument("--ma5-extension-limit", type=float, default=0.05)
    parser.add_argument("--max-atr-pct", type=float, default=0.0)
    parser.add_argument("--max-5d-range-pct", type=float, default=0.0)
    parser.add_argument("--max-momentum-10d-pct", type=float, default=999.0)
    parser.add_argument("--max-close-position-20d-pct", type=float, default=100.0)
    parser.add_argument("--max-distance-to-20d-high-pct", type=float, default=999.0)
    parser.add_argument("--dynamic-params", action="store_true")
    parser.add_argument("--hot-max-gap-up", type=float, default=0.03)
    parser.add_argument("--hot-gap-volume-min-ratio", type=float, default=1.2)
    parser.add_argument("--hot-max-5d-range-pct", type=float, default=36.0)
    parser.add_argument("--hot-max-momentum-10d-pct", type=float, default=32.0)
    parser.add_argument("--hot-max-close-position-20d-pct", type=float, default=90.0)
    parser.add_argument("--normal-max-gap-up", type=float, default=0.02)
    parser.add_argument("--normal-gap-volume-min-ratio", type=float, default=1.3)
    parser.add_argument("--normal-max-5d-range-pct", type=float, default=32.0)
    parser.add_argument("--normal-max-momentum-10d-pct", type=float, default=26.0)
    parser.add_argument("--normal-max-close-position-20d-pct", type=float, default=85.0)
    parser.add_argument("--cold-max-gap-up", type=float, default=0.01)
    parser.add_argument("--cold-gap-volume-min-ratio", type=float, default=1.5)
    parser.add_argument("--cold-max-5d-range-pct", type=float, default=28.0)
    parser.add_argument("--cold-max-momentum-10d-pct", type=float, default=20.0)
    parser.add_argument("--cold-max-close-position-20d-pct", type=float, default=80.0)
    parser.add_argument("--sector-mode", choices=["ignore", "filter", "strong"], default="filter")
    parser.add_argument("--min-sector-momentum-5d", type=float, default=-0.03)
    parser.add_argument("--min-sector-above-ma20-ratio", type=float, default=0.65)
    parser.add_argument("--entry-start-time", default="09:45")
    parser.add_argument("--entry-end-time", default="14:30")
    parser.add_argument("--max-gap-up", type=float, default=0.04)
    parser.add_argument("--max-gap-down", type=float, default=0.03)
    parser.add_argument("--gap-volume-threshold", type=float, default=0.0)
    parser.add_argument("--gap-volume-min-ratio", type=float, default=0.0)
    parser.add_argument("--confirm-buffer", type=float, default=0.003)
    parser.add_argument("--vwap-buffer", type=float, default=0.001)
    parser.add_argument("--max-entry-extension", type=float, default=0.05)
    parser.add_argument("--vwap-fail-bars", type=int, default=3)
    parser.add_argument("--vwap-fail-buffer", type=float, default=0.001)
    parser.add_argument("--max-extension-days", type=int, default=0, help="extra trading days allowed after the base horizon when trend is strong")
    parser.add_argument("--extend-profit-threshold", type=float, default=0.06, help="best intraday profit needed before extending beyond the base horizon")
    parser.add_argument("--optimize", action="store_true")
    parser.add_argument("--entry-end-times", default="11:15,14:30")
    parser.add_argument("--max-gap-ups", default="0.03,0.04,0.05")
    parser.add_argument("--confirm-buffers", default="0.0,0.003")
    parser.add_argument("--vwap-buffers", default="0,0.001,0.003")
    parser.add_argument("--max-entry-extensions", default="0.04,0.05")
    parser.add_argument("--vwap-fail-bars-list", default="0,2,3,4")
    parser.add_argument("--baostock-cache-dir", default="output/baostock_5m_cache")
    parser.add_argument("--out", default="output/a_share_expanded_202601_202606_baostock_5m_vwap.md")
    parser.add_argument("--csv-out", default="output/a_share_expanded_202601_202606_baostock_5m_vwap_trades.csv")
    parser.add_argument("--curve-out", default="")
    parser.add_argument("--opt-out", default="output/a_share_expanded_202601_202606_baostock_5m_vwap_grid.csv")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("dates must be YYYY-MM-DD")
    symbols, price_map, event_scores = load_daily_context(args)
    all_rows = build_signal_rows(symbols, price_map, start_date, end_date, args.horizon, event_scores, args.min_traded_value, args.take_profit, args.hard_stop, args.trailing_stop)
    all_planned = build_planned_by_entry(all_rows, price_map, end_date, args)
    windows = month_windows(start_date, end_date)
    weights = recency_weights(len(windows), args.decay)
    monthly_planned: dict[str, dict[dt.date, list[PlannedTrade]]] = {}
    for window_start, window_end, label in windows:
        rows = build_signal_rows(
            symbols,
            price_map,
            window_start,
            window_end,
            args.horizon,
            event_scores,
            args.min_traded_value,
            args.take_profit,
            args.hard_stop,
            args.trailing_stop,
        )
        monthly_planned[label] = build_planned_by_entry(rows, price_map, window_end, args)
    intraday_map = prefetch_intraday(all_planned, args)

    results: list[dict[str, Any]] = []
    best_payload: tuple[dict[str, Any], list[dict[str, Any]], list[ExecutedTrade], list[dict[str, Any]], float] | None = None
    combos: list[tuple[str, float, float, float, float, int]]
    if args.optimize:
        combos = list(
            itertools.product(
                parse_str_list(args.entry_end_times),
                parse_float_list(args.max_gap_ups),
                parse_float_list(args.confirm_buffers),
                parse_float_list(args.vwap_buffers),
                parse_float_list(args.max_entry_extensions),
                parse_int_list(args.vwap_fail_bars_list),
            )
        )
    else:
        combos = [(args.entry_end_time, args.max_gap_up, args.confirm_buffer, args.vwap_buffer, args.max_entry_extension, args.vwap_fail_bars)]
    for entry_end_time, max_gap_up, confirm_buffer, vwap_buffer, max_entry_extension, vwap_fail_bars in combos:
        combo_args = argparse.Namespace(**vars(args))
        combo_args.entry_end_time = entry_end_time
        combo_args.max_gap_up = max_gap_up
        combo_args.confirm_buffer = confirm_buffer
        combo_args.vwap_buffer = vwap_buffer
        combo_args.max_entry_extension = max_entry_extension
        combo_args.vwap_fail_bars = vwap_fail_bars
        payload = result_for_preplanned(windows, weights, monthly_planned, all_planned, price_map, intraday_map, combo_args)
        result = payload[0]
        results.append(result)
        if best_payload is None or (result["objective"], result["weighted_return_pct"]) > (best_payload[0]["objective"], best_payload[0]["weighted_return_pct"]):
            best_payload = payload
        print(result)

    results.sort(key=lambda item: (item["objective"], item["weighted_return_pct"]), reverse=True)
    opt_path = Path(args.opt_out)
    opt_path.parent.mkdir(parents=True, exist_ok=True)
    with opt_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()) if results else [])
        if results:
            writer.writeheader()
            writer.writerows(results)
    if best_payload is None:
        raise SystemExit("no result")
    best, monthly_rows, trades, curve, _ = best_payload
    final_args = argparse.Namespace(**vars(args))
    final_args.entry_end_time = str(best["entry_end_time"])
    final_args.max_gap_up = float(best["max_gap_up"])
    final_args.confirm_buffer = float(best["confirm_buffer"])
    final_args.vwap_buffer = float(best["vwap_buffer"])
    final_args.max_entry_extension = float(best["max_entry_extension"])
    final_args.vwap_fail_bars = int(best["vwap_fail_bars"])
    trades_csv = Path(args.csv_out)
    write_trades(trades_csv, trades)
    if args.curve_out:
        write_curve(Path(args.curve_out), curve)
    write_report(Path(args.out), trades_csv, best, monthly_rows, trades, final_args)
    print(f"best={best}")
    print(f"report={args.out}")
    print(f"trades_csv={trades_csv}")
    print(f"grid_csv={opt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
