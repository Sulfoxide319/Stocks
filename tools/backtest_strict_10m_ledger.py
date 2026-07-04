#!/usr/bin/env python3
"""Strict 10-minute execution ledger for the current short-term strategy."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from baostock_intraday import BaoStock5mClient, IntradayBar
from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from short_term_strategy_backtest import (
    PlannedTrade,
    build_bar_maps,
    build_signal_rows,
    evaluate_regime,
    max_drawdown,
    passes_strategy,
)
from tech_event_backtest import PriceBar, fetch_yahoo_history
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date
from tools.backtest_existing_strategy_ledger import (
    cached_history,
    default_end_date,
    dynamic_param_overrides,
    market_temperature,
    planned_passes_dynamic_filters,
    selection_key,
    subtract_months,
    write_outputs,
)


def parse_clock(raw: str) -> dt.time:
    hour, minute = raw.split(":", 1)
    return dt.time(int(hour), int(minute))


def first_index_after(bars: list[PriceBar], date_value: dt.date) -> int | None:
    for index, bar in enumerate(bars):
        if bar.date > date_value:
            return index
    return None


def final_holding_date(bars: list[PriceBar], entry_date: dt.date, horizon: int, end_date: dt.date) -> dt.date | None:
    entry_index = next((index for index, bar in enumerate(bars) if bar.date >= entry_date), None)
    if entry_index is None:
        return None
    final_index = min(entry_index + max(1, horizon) - 1, len(bars) - 1)
    return min(bars[final_index].date, end_date)


def next_trading_date_after(bars: list[PriceBar], date_value: dt.date) -> dt.date | None:
    for bar in bars:
        if bar.date > date_value:
            return bar.date
    return None


def close_on_or_before(bars: list[PriceBar], date_value: dt.date) -> float | None:
    value: float | None = None
    for bar in bars:
        if bar.date <= date_value:
            value = bar.close
        else:
            break
    return value


def aggregate_10m(bars: Iterable[IntradayBar]) -> list[IntradayBar]:
    grouped: dict[tuple[dt.date, int], list[IntradayBar]] = {}
    for bar in sorted(bars, key=lambda item: item.moment):
        if not (dt.time(9, 30) <= bar.time <= dt.time(15, 0)):
            continue
        elapsed = (bar.time.hour * 60 + bar.time.minute) - (9 * 60 + 30)
        if elapsed <= 0:
            continue
        bucket = (elapsed - 1) // 10
        grouped.setdefault((bar.date, bucket), []).append(bar)
    result: list[IntradayBar] = []
    for (_date, _bucket), items in sorted(grouped.items()):
        if not items:
            continue
        first = items[0]
        last = items[-1]
        result.append(
            IntradayBar(
                date=first.date,
                time=last.time,
                code=first.code,
                open=first.open,
                high=max(item.high for item in items),
                low=min(item.low for item in items),
                close=last.close,
                volume=sum(item.volume for item in items),
                amount=sum(item.amount for item in items),
            )
        )
    return result


def intraday_vwap_by_moment(bars: list[IntradayBar]) -> dict[dt.datetime, float]:
    values: dict[dt.datetime, float] = {}
    current_date: dt.date | None = None
    amount = 0.0
    volume = 0.0
    for bar in sorted(bars, key=lambda item: item.moment):
        if current_date != bar.date:
            current_date = bar.date
            amount = 0.0
            volume = 0.0
        amount += bar.amount
        volume += bar.volume
        values[bar.moment] = amount / volume if volume > 0 else bar.close
    return values


def is_limit_up(reference: float, price: float, threshold: float) -> bool:
    return reference > 0 and price >= reference * (1 + threshold)


def is_limit_down(reference: float, price: float, threshold: float) -> bool:
    return reference > 0 and price <= reference * (1 - threshold)


def plan_trade_base(
    row: Any,
    bars: list[PriceBar],
    end_date: dt.date,
    args: argparse.Namespace,
) -> PlannedTrade | None:
    signal_date = parse_date(row.date)
    if not signal_date:
        return None
    entry_index = first_index_after(bars, signal_date)
    if entry_index is None:
        return None
    entry_bar = bars[entry_index]
    if entry_bar.date > end_date:
        return None
    target = args.take_profit
    stop = args.hard_stop
    trail = args.trailing_stop
    if args.dynamic_exit:
        raw_target = row.atr_pct / 100 * args.target_atr_mult + row.max_5d_range_pct / 100 * args.target_range_mult
        target = min(args.target_max, max(args.target_min, raw_target))
        stop = min(args.stop_max, max(args.stop_min, row.atr_pct / 100 * args.stop_atr_mult))
        trail = min(args.trail_max, max(args.trail_min, row.atr_pct / 100 * args.trail_atr_mult))
    return PlannedTrade(
        ticker=row.ticker,
        name=row.name,
        signal_date=signal_date,
        entry_date=entry_bar.date,
        exit_date=entry_bar.date,
        entry_price=0.0,
        exit_price=0.0,
        return_pct=0.0,
        exit_reason="planned",
        target_pct=target,
        hard_stop_pct=stop,
        trailing_stop_pct=trail,
        score=row.score,
        setup_type=row.setup_type,
        features={
            "traded_value_ratio": row.traded_value_ratio,
            "atr_pct": row.atr_pct,
            "max_5d_range_pct": row.max_5d_range_pct,
            "change_1d_pct": row.change_1d_pct,
            "momentum_3d_pct": row.momentum_3d_pct,
            "momentum_10d_pct": row.momentum_10d_pct,
            "value_ratio_3d": row.value_ratio_3d,
            "distance_to_ma5_pct": row.distance_to_ma5_pct,
            "distance_to_20d_high_pct": row.distance_to_20d_high_pct,
            "close_position_20d_pct": row.close_position_20d_pct,
            "above_ma5": row.above_ma5,
            "above_ma20": row.above_ma20,
            "sector_group": row.sector_group,
            "sector_momentum_5d_pct": row.sector_momentum_5d_pct,
            "sector_above_ma20_ratio": row.sector_above_ma20_ratio,
        },
    )


def refine_trade_strict_10m(
    planned: PlannedTrade,
    daily_bars: list[PriceBar],
    intraday_bars_5m: list[IntradayBar],
    end_date: dt.date,
    args: argparse.Namespace,
) -> PlannedTrade | None:
    base_final_date = final_holding_date(daily_bars, planned.entry_date, args.horizon, end_date)
    if base_final_date is None:
        return None
    signal_close = close_on_or_before(daily_bars, planned.signal_date)
    if not signal_close:
        return None
    bars = [
        bar
        for bar in aggregate_10m(intraday_bars_5m)
        if planned.entry_date <= bar.date <= base_final_date and dt.time(9, 30) <= bar.time <= dt.time(15, 0)
    ]
    if not bars:
        return None
    entry_day_bars = [bar for bar in bars if bar.date == planned.entry_date]
    if not entry_day_bars:
        return None
    first_bar = entry_day_bars[0]
    gap_pct = first_bar.open / signal_close - 1 if signal_close else 0.0
    if gap_pct > args.max_gap_up or gap_pct < -args.max_gap_down:
        return None
    if args.reject_limit_open and (is_limit_up(signal_close, first_bar.open, args.limit_threshold) or is_limit_down(signal_close, first_bar.open, args.limit_threshold)):
        return None
    if gap_pct > args.gap_volume_threshold and float(planned.features.get("traded_value_ratio") or 0.0) < args.gap_volume_min_ratio:
        return None

    vwap_by_moment = intraday_vwap_by_moment(bars)
    entry_start = parse_clock(args.entry_start_time)
    entry_end = parse_clock(args.entry_end_time)
    entry_bar: IntradayBar | None = None
    entry_vwap = 0.0
    for bar in entry_day_bars:
        if bar.time < entry_start or bar.time > entry_end:
            continue
        current_vwap = vwap_by_moment.get(bar.moment, bar.close)
        trigger = max(signal_close * (1 + args.confirm_buffer), current_vwap * (1 + args.vwap_buffer))
        if bar.close < trigger:
            continue
        if bar.close / signal_close - 1 > args.max_entry_extension:
            continue
        if args.reject_limit_entry and is_limit_up(signal_close, bar.high, args.limit_threshold):
            continue
        entry_bar = bar
        entry_vwap = current_vwap
        break
    if entry_bar is None:
        return None

    entry_price = entry_bar.close * (1 + args.slippage_bps / 10000)
    first_sell_date = next_trading_date_after(daily_bars, planned.entry_date)
    if first_sell_date is None or first_sell_date > base_final_date:
        return None
    exit_bar = next((bar for bar in reversed(bars) if bar.date <= base_final_date), bars[-1])
    exit_price = exit_bar.close * (1 - args.slippage_bps / 10000)
    exit_reason = "time_exit_10m"
    best_high = entry_price
    below_vwap_count = 0
    for bar in bars:
        if bar.moment <= entry_bar.moment:
            continue
        if bar.high > best_high:
            best_high = bar.high
        if bar.date < first_sell_date:
            continue
        current_vwap = vwap_by_moment.get(bar.moment, bar.close)
        if bar.low <= entry_price * (1 - planned.hard_stop_pct):
            if args.reject_limit_exit and is_limit_down(entry_price, bar.low, args.limit_threshold):
                exit_bar = bar
                exit_price = bar.close * (1 - args.slippage_bps / 10000)
                exit_reason = "limit_down_delayed_exit_10m"
                continue
            exit_bar = bar
            exit_price = entry_price * (1 - planned.hard_stop_pct) * (1 - args.slippage_bps / 10000)
            exit_reason = "hard_stop_10m"
            break
        if bar.high >= entry_price * (1 + planned.target_pct):
            exit_bar = bar
            exit_price = entry_price * (1 + planned.target_pct) * (1 - args.slippage_bps / 10000)
            exit_reason = "take_profit_10m"
            break
        if best_high >= entry_price * (1 + max(0.04, planned.target_pct * 0.4)):
            trailing_price = best_high * (1 - planned.trailing_stop_pct)
            if bar.low <= trailing_price:
                exit_bar = bar
                exit_price = trailing_price * (1 - args.slippage_bps / 10000)
                exit_reason = "trailing_stop_10m"
                break
        if bar.close < current_vwap * (1 - args.vwap_fail_buffer) and bar.close < entry_price:
            below_vwap_count += 1
        else:
            below_vwap_count = 0
        if args.vwap_fail_bars > 0 and below_vwap_count >= args.vwap_fail_bars:
            exit_bar = bar
            exit_price = bar.close * (1 - args.slippage_bps / 10000)
            exit_reason = "vwap_fail_10m"
            break

    features = dict(planned.features)
    features.update(
        {
            "entry_time": entry_bar.time.isoformat(timespec="minutes"),
            "exit_time": exit_bar.time.isoformat(timespec="minutes"),
            "entry_vwap": round(entry_vwap, 4),
            "entry_gap_pct": round(gap_pct * 100, 4),
            "execution_interval_minutes": 10,
            "slippage_bps": args.slippage_bps,
        }
    )
    return replace(
        planned,
        entry_price=entry_price,
        exit_date=exit_bar.date,
        exit_price=exit_price,
        return_pct=(exit_price / entry_price - 1) * 100 if entry_price else 0.0,
        exit_reason=exit_reason,
        features=features,
    )


def build_planned_by_entry(
    signal_rows: list[Any],
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
        planned = plan_trade_base(row, price_map.get(row.ticker, []), end_date, args)
        if planned:
            planned_by_entry.setdefault(planned.entry_date, []).append(planned)
    for date_value in planned_by_entry:
        planned_by_entry[date_value].sort(key=lambda item: selection_key(item, args.selection_mode), reverse=True)
    return planned_by_entry


def required_dates_for_planned(planned: dict[dt.date, list[PlannedTrade]], price_map: dict[str, list[PriceBar]], end_date: dt.date, horizon: int) -> dict[str, set[dt.date]]:
    required: dict[str, set[dt.date]] = {}
    bar_maps = build_bar_maps(price_map)
    for candidates in planned.values():
        for item in candidates:
            dates = sorted(date for date in bar_maps.get(item.ticker, {}) if item.entry_date <= date <= end_date)
            needed = dates[: max(1, horizon)]
            required.setdefault(item.ticker, set()).update(needed)
    return required


def prefetch_intraday(required: dict[str, set[dt.date]], args: argparse.Namespace) -> dict[str, list[IntradayBar]]:
    intraday: dict[str, list[IntradayBar]] = {}
    with BaoStock5mClient(cache_dir=Path(args.baostock_cache_dir), sleep_seconds=args.baostock_sleep_seconds) as client:
        total = len(required)
        for index, (ticker, dates) in enumerate(sorted(required.items()), 1):
            try:
                intraday[ticker] = client.fetch_5m_for_dates(ticker, dates)
            except Exception as exc:
                print(f"warning: intraday fetch failed {index}/{total} {ticker}: {type(exc).__name__}: {exc}", flush=True)
                intraday[ticker] = []
            if index == 1 or index % 10 == 0 or index == total:
                print(f"loaded intraday {index}/{total}", flush=True)
    return intraday


def marked_equity(cash: float, open_positions: list[tuple[PlannedTrade, float, float]], bar_maps: dict[str, dict[dt.date, PriceBar]], date_value: dt.date) -> float:
    total = cash
    for planned, shares, _capital in open_positions:
        bar = bar_maps.get(planned.ticker, {}).get(date_value)
        total += shares * (bar.close if bar else planned.entry_price)
    return total


def append_ledger_row(rows: list[dict[str, Any]], period: str, date_value: dt.date, action: str, planned: PlannedTrade, shares: float, price: float, cash: float, equity: float, note: str, realized_pnl: float = 0.0) -> None:
    rows.append(
        {
            "period": period,
            "date": date_value.isoformat(),
            "time": planned.features.get("entry_time" if action == "BUY" else "exit_time", ""),
            "action": action,
            "ticker": planned.ticker,
            "name": planned.name,
            "shares": round(shares, 2),
            "price": round(price, 4),
            "amount": round(shares * price, 2),
            "cash_after": round(cash, 2),
            "total_equity_after": round(equity, 2),
            "realized_pnl": round(realized_pnl, 2),
            "return_pct": round(planned.return_pct, 4) if action == "SELL" else "",
            "reason": planned.exit_reason if action == "SELL" else planned.setup_type,
            "note": note,
        }
    )


def simulate_period(period: str, planned_by_entry: dict[dt.date, list[PlannedTrade]], price_map: dict[str, list[PriceBar]], intraday_map: dict[str, list[IntradayBar]], start_date: dt.date, end_date: dt.date, args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    trading_dates = sorted({bar.date for bars in price_map.values() for bar in bars if start_date <= bar.date <= end_date})
    bar_maps = build_bar_maps(price_map)
    cash = args.initial_cash
    open_positions: list[tuple[PlannedTrade, float, float]] = []
    ledger: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    closed_returns: list[float] = []
    cooldown_until: dt.date | None = None
    fee_rate = args.fee_bps / 10000
    skipped_candidates = 0

    for date_value in trading_dates:
        temperature = market_temperature(price_map, date_value)
        overrides = dynamic_param_overrides(str(temperature["state"]), args)
        regime, cooldown_until = evaluate_regime(
            date_value,
            [],
            daily,
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
            0,
        )
        for raw_planned in planned_by_entry.get(date_value, []):
            if regime.action == "skip":
                break
            if len(open_positions) >= args.max_positions:
                break
            if any(position[0].ticker == raw_planned.ticker for position in open_positions):
                continue
            if not planned_passes_dynamic_filters(raw_planned, overrides):
                continue
            planned = refine_trade_strict_10m(raw_planned, price_map.get(raw_planned.ticker, []), intraday_map.get(raw_planned.ticker, []), end_date, args)
            if not planned:
                skipped_candidates += 1
                continue
            slots = max(1, args.max_positions - len(open_positions))
            capital = cash / slots
            if regime.action == "reduce":
                capital *= max(0.0, min(1.0, args.regime_risk_factor))
            if capital <= 0:
                continue
            shares = capital * (1 - fee_rate) / planned.entry_price
            cash -= capital
            open_positions.append((planned, shares, capital))
            equity = marked_equity(cash, open_positions, bar_maps, date_value)
            append_ledger_row(ledger, period, date_value, "BUY", planned, shares, planned.entry_price, cash, equity, f"score={planned.score:.1f}; market={temperature['state']}")

        still_open: list[tuple[PlannedTrade, float, float]] = []
        for planned, shares, capital in open_positions:
            if planned.exit_date <= date_value:
                proceeds = shares * planned.exit_price * (1 - fee_rate)
                cash += proceeds
                pnl = proceeds - capital
                closed_returns.append(planned.return_pct - args.fee_bps * 2 / 100)
                equity = marked_equity(cash, still_open, bar_maps, date_value)
                append_ledger_row(ledger, period, date_value, "SELL", planned, shares, planned.exit_price, cash, equity, "", pnl)
            else:
                still_open.append((planned, shares, capital))
        open_positions = still_open

        equity = marked_equity(cash, open_positions, bar_maps, date_value)
        daily.append(
            {
                "period": period,
                "date": date_value.isoformat(),
                "cash": round(cash, 2),
                "equity": round(equity, 2),
                "total_equity": round(equity, 2),
                "open_positions": len(open_positions),
                "market_state": temperature["state"],
            }
        )

    final_equity = float(daily[-1]["total_equity"]) if daily else args.initial_cash
    wins = sum(1 for value in closed_returns if value > 0)
    closed = len(closed_returns)
    summary = {
        "period": period,
        "start_date": trading_dates[0].isoformat() if trading_dates else start_date.isoformat(),
        "end_date": trading_dates[-1].isoformat() if trading_dates else end_date.isoformat(),
        "initial_cash": round(args.initial_cash, 2),
        "final_cash": round(cash, 2),
        "final_equity": round(final_equity, 2),
        "return_pct": round((final_equity / args.initial_cash - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown(daily, args.initial_cash), 4),
        "closed_trades": closed,
        "win_rate_pct": round(wins / closed * 100, 4) if closed else 0.0,
        "open_positions_end": len(open_positions),
        "skipped_after_intraday_filters": skipped_candidates,
        "interval_minutes": 10,
        "slippage_bps": args.slippage_bps,
        "event_file": "disabled",
        "event_weight": 0,
    }
    return ledger, daily, summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strict 10-minute execution backtest ledger.")
    parser.add_argument("--watchlist", default="config/watchlist.mainboard_liquid.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES))
    parser.add_argument("--end-date", default=default_end_date().isoformat())
    parser.add_argument("--period-months", default="1,3,6")
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--max-positions", type=int, default=2)
    parser.add_argument("--min-score", type=float, default=90.0)
    parser.add_argument("--selection-mode", choices=["score", "score_quality", "quality", "score_low_heat"], default="score")
    parser.add_argument("--setups", default="VOLUME_BREAKOUT,HIGH_VOLATILITY")
    parser.add_argument("--horizon", type=int, default=3)
    parser.add_argument("--take-profit", type=float, default=0.10)
    parser.add_argument("--hard-stop", type=float, default=0.04)
    parser.add_argument("--trailing-stop", type=float, default=0.035)
    parser.add_argument("--dynamic-exit", action="store_true", default=True)
    parser.add_argument("--target-atr-mult", type=float, default=0.9)
    parser.add_argument("--target-range-mult", type=float, default=0.35)
    parser.add_argument("--target-min", type=float, default=0.05)
    parser.add_argument("--target-max", type=float, default=0.18)
    parser.add_argument("--stop-atr-mult", type=float, default=0.55)
    parser.add_argument("--stop-min", type=float, default=0.025)
    parser.add_argument("--stop-max", type=float, default=0.07)
    parser.add_argument("--trail-atr-mult", type=float, default=0.25)
    parser.add_argument("--trail-min", type=float, default=0.025)
    parser.add_argument("--trail-max", type=float, default=0.06)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--limit-threshold", type=float, default=0.095)
    parser.add_argument("--reject-limit-open", action="store_true", default=True)
    parser.add_argument("--reject-limit-entry", action="store_true", default=True)
    parser.add_argument("--reject-limit-exit", action="store_true", default=True)
    parser.add_argument("--min-traded-value", type=float, default=200_000_000)
    parser.add_argument("--regime-filter", action="store_true", default=True)
    parser.add_argument("--regime-mode", choices=["skip", "reduce"], default="skip")
    parser.add_argument("--regime-lookback-trades", type=int, default=12)
    parser.add_argument("--regime-min-trades", type=int, default=8)
    parser.add_argument("--regime-min-win-rate", type=float, default=0.30)
    parser.add_argument("--regime-max-hard-stop-rate", type=float, default=0.45)
    parser.add_argument("--regime-max-drawdown", type=float, default=0.06)
    parser.add_argument("--regime-risk-factor", type=float, default=0.35)
    parser.add_argument("--market-ma-days", type=int, default=20)
    parser.add_argument("--market-lookback-days", type=int, default=5)
    parser.add_argument("--market-min-return", type=float, default=-0.04)
    parser.add_argument("--ma5-mode", choices=["ignore", "filter", "pullback"], default="ignore")
    parser.add_argument("--ma5-pullback-limit", type=float, default=0.025)
    parser.add_argument("--ma5-extension-limit", type=float, default=0.04)
    parser.add_argument("--sector-mode", choices=["ignore", "filter", "strong"], default="ignore")
    parser.add_argument("--min-sector-momentum-5d", type=float, default=-0.03)
    parser.add_argument("--min-sector-above-ma20-ratio", type=float, default=0.35)
    parser.add_argument("--entry-start-time", default="09:50")
    parser.add_argument("--entry-end-time", default="11:20")
    parser.add_argument("--max-gap-up", type=float, default=0.02)
    parser.add_argument("--max-gap-down", type=float, default=0.03)
    parser.add_argument("--gap-volume-threshold", type=float, default=0.0)
    parser.add_argument("--gap-volume-min-ratio", type=float, default=1.3)
    parser.add_argument("--confirm-buffer", type=float, default=0.0)
    parser.add_argument("--vwap-buffer", type=float, default=0.001)
    parser.add_argument("--max-entry-extension", type=float, default=0.04)
    parser.add_argument("--vwap-fail-bars", type=int, default=3)
    parser.add_argument("--vwap-fail-buffer", type=float, default=0.001)
    parser.add_argument("--dynamic-params", action="store_true", default=True)
    parser.add_argument("--hot-max-5d-range-pct", type=float, default=32.0)
    parser.add_argument("--hot-max-momentum-10d-pct", type=float, default=26.0)
    parser.add_argument("--hot-max-close-position-20d-pct", type=float, default=85.0)
    parser.add_argument("--normal-max-5d-range-pct", type=float, default=32.0)
    parser.add_argument("--normal-max-momentum-10d-pct", type=float, default=26.0)
    parser.add_argument("--normal-max-close-position-20d-pct", type=float, default=85.0)
    parser.add_argument("--cold-max-5d-range-pct", type=float, default=25.0)
    parser.add_argument("--cold-max-momentum-10d-pct", type=float, default=20.0)
    parser.add_argument("--cold-max-close-position-20d-pct", type=float, default=80.0)
    parser.add_argument("--history-timeout", type=float, default=5.0)
    parser.add_argument("--daily-cache-dir", default="output/backtest_daily_cache")
    parser.add_argument("--baostock-cache-dir", default="output/baostock_5m_cache")
    parser.add_argument("--baostock-sleep-seconds", type=float, default=0.15)
    parser.add_argument("--out-dir", default="output/backtest_strict_10m")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    end_date = parse_date(args.end_date)
    if not end_date:
        raise SystemExit("end date must be YYYY-MM-DD")
    periods = sorted({int(item.strip()) for item in args.period_months.split(",") if item.strip()})
    earliest_start = subtract_months(end_date, max(periods))
    fetch_start = earliest_start - dt.timedelta(days=170)
    fetch_end = end_date + dt.timedelta(days=args.horizon * 3 + 10)
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    price_map: dict[str, list[PriceBar]] = {}
    for index, symbol in enumerate(symbols, 1):
        try:
            price_map[symbol.ticker] = cached_history(session, symbol.yahoo_symbol or symbol.ticker, symbol.ticker, fetch_start, fetch_end, Path(args.daily_cache_dir), args.history_timeout)
        except Exception as exc:
            print(f"warning: daily history failed {index}/{len(symbols)} {symbol.ticker}: {type(exc).__name__}: {exc}", flush=True)
            price_map[symbol.ticker] = []
        if index == 1 or index % 50 == 0 or index == len(symbols):
            print(f"loaded daily {index}/{len(symbols)}", flush=True)

    all_planned: dict[dt.date, list[PlannedTrade]] = {}
    period_planned: dict[int, dict[dt.date, list[PlannedTrade]]] = {}
    for months in periods:
        start_date = subtract_months(end_date, months)
        rows = build_signal_rows(symbols, price_map, start_date, end_date, args.horizon, {}, args.min_traded_value, args.take_profit, args.hard_stop, args.trailing_stop)
        planned = build_planned_by_entry(rows, price_map, end_date, args)
        period_planned[months] = planned
        for date_value, items in planned.items():
            all_planned.setdefault(date_value, []).extend(items)
        print(f"planned {months}M candidates={sum(len(items) for items in planned.values())}", flush=True)

    required = required_dates_for_planned(all_planned, price_map, end_date, args.horizon)
    print(f"intraday required tickers={len(required)}", flush=True)
    intraday_map = prefetch_intraday(required, args)

    all_ledger: list[dict[str, Any]] = []
    all_daily: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for months in periods:
        start_date = subtract_months(end_date, months)
        label = f"{months}M"
        print(f"simulating {label}: {start_date} to {end_date}", flush=True)
        ledger, daily, summary = simulate_period(label, period_planned[months], price_map, intraday_map, start_date, end_date, args)
        all_ledger.extend(ledger)
        all_daily.extend(daily)
        summaries.append(summary)
        print(f"{label}: trades={summary['closed_trades']} final_equity={summary['final_equity']} return={summary['return_pct']}% skipped={summary['skipped_after_intraday_filters']}", flush=True)

    prefix = f"strict_10m_no_events_{min(periods)}M_{max(periods)}M_to_{end_date:%Y%m%d}"
    outputs = write_outputs(Path(args.out_dir), prefix, all_ledger, all_daily, summaries)
    for name, path in outputs.items():
        print(f"{name}={path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
