#!/usr/bin/env python3
"""Export a simple ledger for the current short-term strategy backtest."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

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
    elif breadth <= 0.42 and avg_5d >= 0.005 and avg_20d > -0.04:
        state = "narrow_rally"
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

    def profile(prefix: str) -> dict[str, float]:
        base_max_gap_up = getattr(args, "max_gap_up", 0.0)
        base_gap_volume = getattr(args, "gap_volume_min_ratio", 0.0)
        base_range = getattr(args, "max_5d_range_pct", 0.0)
        base_momentum = getattr(args, "max_momentum_10d_pct", 999.0)
        base_position = getattr(args, "max_close_position_20d_pct", 100.0)
        base_min_score = getattr(args, "min_score", 0.0)
        return {
            "min_score": getattr(args, f"{prefix}_min_score", base_min_score),
            "max_gap_up": getattr(args, f"{prefix}_max_gap_up", base_max_gap_up),
            "gap_volume_min_ratio": getattr(args, f"{prefix}_gap_volume_min_ratio", base_gap_volume),
            "max_5d_range_pct": getattr(args, f"{prefix}_max_5d_range_pct", base_range),
            "max_momentum_10d_pct": getattr(args, f"{prefix}_max_momentum_10d_pct", base_momentum),
            "max_close_position_20d_pct": getattr(args, f"{prefix}_max_close_position_20d_pct", base_position),
        }

    if state == "hot":
        return profile("hot")
    if state == "narrow_rally":
        return profile("narrow_rally")
    if state == "cold":
        return profile("cold")
    return profile("normal")


def planned_passes_dynamic_filters(planned: PlannedTrade, overrides: dict[str, float]) -> bool:
    features = planned.features
    min_score = float(overrides.get("min_score") or 0.0)
    max_range = float(overrides.get("max_5d_range_pct", 0.0) or 0.0)
    max_momentum = float(overrides.get("max_momentum_10d_pct", 999.0) or 999.0)
    max_position = float(overrides.get("max_close_position_20d_pct", 100.0) or 100.0)
    if min_score > 0 and planned.score < min_score:
        return False
    if max_range > 0 and float(features.get("max_5d_range_pct") or 0.0) > max_range:
        return False
    if max_momentum < 999 and float(features.get("momentum_10d_pct") or 0.0) > max_momentum:
        return False
    if max_position < 100 and float(features.get("close_position_20d_pct") or 0.0) > max_position:
        return False
    return True


def selection_key(planned: PlannedTrade, mode: str) -> Any:
    if mode == "score":
        return planned.score
    features = planned.features
    quality = (
        planned.score,
        float(features.get("traded_value_ratio") or 0.0),
        -float(features.get("distance_to_ma5_pct") or 0.0),
    )
    return quality


def first_index_after(bars: list[PriceBar], date_value: dt.date) -> int | None:
    for index, bar in enumerate(bars):
        if bar.date > date_value:
            return index
    return None


def plan_trade_t1_from_signal(
    row: Any,
    bars: list[PriceBar],
    horizon: int,
    take_profit: float,
    hard_stop: float,
    trailing_stop: float,
    dynamic_exit: bool,
    target_atr_mult: float,
    target_range_mult: float,
    event_bonus: float,
    target_min: float,
    target_max: float,
    stop_atr_mult: float,
    stop_min: float,
    stop_max: float,
    trail_atr_mult: float,
    trail_min: float,
    trail_max: float,
    end_date: dt.date,
    max_gap_up: float,
    max_gap_down: float,
    confirm_buffer: float,
    max_entry_extension: float,
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
    signal_close = row.close
    gap_pct = entry_bar.open / signal_close - 1 if signal_close else 0.0
    if gap_pct > max_gap_up or gap_pct < -max_gap_down:
        return None
    entry_price = max(entry_bar.open, signal_close * (1 + confirm_buffer))
    if entry_bar.high < entry_price:
        return None
    if signal_close and entry_price / signal_close - 1 > max_entry_extension:
        return None

    target_pct = take_profit
    hard_stop_pct = hard_stop
    trailing_stop_pct = trailing_stop
    if dynamic_exit:
        raw_target = row.atr_pct / 100 * target_atr_mult + row.max_5d_range_pct / 100 * target_range_mult + (event_bonus if row.setup_type == "EVENT_PLUS_VOLATILITY" else 0)
        target_pct = min(target_max, max(target_min, raw_target))
        hard_stop_pct = min(stop_max, max(stop_min, row.atr_pct / 100 * stop_atr_mult))
        trailing_stop_pct = min(trail_max, max(trail_min, row.atr_pct / 100 * trail_atr_mult))

    first_sell_index = entry_index + 1
    if first_sell_index >= len(bars):
        return None
    final_index = min(entry_index + horizon - 1, len(bars) - 1)
    if final_index < first_sell_index:
        final_index = first_sell_index
    exit_bar = bars[final_index]
    exit_price = exit_bar.close
    exit_reason = "time_exit_t1"
    best_high = entry_bar.high

    for index in range(first_sell_index, final_index + 1):
        bar = bars[index]
        if bar.date > end_date:
            break
        if bar.low <= entry_price * (1 - hard_stop_pct):
            exit_bar = bar
            exit_price = entry_price * (1 - hard_stop_pct)
            exit_reason = "hard_stop_t1"
            break
        if bar.high > best_high:
            best_high = bar.high
        if bar.high >= entry_price * (1 + target_pct):
            exit_bar = bar
            exit_price = entry_price * (1 + target_pct)
            exit_reason = "take_profit_t1"
            break
        if best_high >= entry_price * (1 + max(0.04, target_pct * 0.4)) and bar.low <= best_high * (1 - trailing_stop_pct):
            exit_bar = bar
            exit_price = best_high * (1 - trailing_stop_pct)
            exit_reason = "trailing_stop_t1"
            break

    if exit_bar.date > end_date:
        return None
    return PlannedTrade(
        ticker=row.ticker,
        name=row.name,
        signal_date=signal_date,
        entry_date=entry_bar.date,
        exit_date=exit_bar.date,
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=(exit_price / entry_price - 1) * 100 if entry_price else 0.0,
        exit_reason=exit_reason,
        target_pct=target_pct,
        hard_stop_pct=hard_stop_pct,
        trailing_stop_pct=trailing_stop_pct,
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


def subtract_months(date_value: dt.date, months: int) -> dt.date:
    month = date_value.month - months
    year = date_value.year
    while month <= 0:
        month += 12
        year -= 1
    days_in_month = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month - 1]
    return dt.date(year, month, min(date_value.day, days_in_month))


def default_end_date() -> dt.date:
    today = dt.date.today()
    if today.weekday() == 5:
        return today - dt.timedelta(days=1)
    if today.weekday() == 6:
        return today - dt.timedelta(days=2)
    return today


def price_bars_to_dicts(bars: list[PriceBar]) -> list[dict[str, Any]]:
    return [
        {
            "date": bar.date.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]


def price_bars_from_dicts(rows: list[dict[str, Any]]) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for row in rows:
        try:
            bars.append(
                PriceBar(
                    date=dt.date.fromisoformat(str(row["date"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row.get("volume") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def cached_history(
    session: requests.Session,
    yahoo_symbol: str,
    ticker: str,
    fetch_start: dt.date,
    fetch_end: dt.date,
    cache_dir: Path,
    timeout_seconds: float,
) -> list[PriceBar]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{ticker}_{fetch_start:%Y%m%d}_{fetch_end:%Y%m%d}.json"
    if cache_path.exists():
        try:
            return price_bars_from_dicts(json.loads(cache_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    bars = fetch_yahoo_history(session, yahoo_symbol, fetch_start, fetch_end, timeout_seconds=timeout_seconds)
    cache_path.write_text(json.dumps(price_bars_to_dicts(bars), ensure_ascii=False), encoding="utf-8")
    return bars


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
        planned = plan_trade_t1_from_signal(
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
            args.max_gap_up,
            args.max_gap_down,
            args.confirm_buffer,
            args.max_entry_extension,
        )
        if planned:
            planned_by_entry.setdefault(planned.entry_date, []).append(planned)
    for date_value in planned_by_entry:
        planned_by_entry[date_value].sort(key=lambda item: selection_key(item, args.selection_mode), reverse=True)
    return planned_by_entry


def close_on_or_before(bar_map: dict[dt.date, PriceBar], date_value: dt.date) -> float | None:
    dates = [item for item in bar_map if item <= date_value]
    if not dates:
        return None
    return bar_map[max(dates)].close


def marked_equity(cash: float, open_positions: list[tuple[PlannedTrade, float, float]], bar_maps: dict[str, dict[dt.date, PriceBar]], date_value: dt.date) -> float:
    total = cash
    for planned, shares, _capital in open_positions:
        close = close_on_or_before(bar_maps.get(planned.ticker, {}), date_value) or planned.entry_price
        total += shares * close
    return total


def append_ledger_row(
    rows: list[dict[str, Any]],
    period: str,
    date_value: dt.date,
    action: str,
    planned: PlannedTrade,
    shares: float,
    price: float,
    cash: float,
    equity: float,
    note: str,
    realized_pnl: float = 0.0,
) -> None:
    rows.append(
        {
            "period": period,
            "date": date_value.isoformat(),
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


def simulate_period(
    period: str,
    planned_by_entry: dict[dt.date, list[PlannedTrade]],
    price_map: dict[str, list[PriceBar]],
    start_date: dt.date,
    end_date: dt.date,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    trading_dates = sorted(
        {
            bar.date
            for bars in price_map.values()
            for bar in bars
            if start_date <= bar.date <= end_date
        }
    )
    bar_maps = build_bar_maps(price_map)
    cash = args.initial_cash
    open_positions: list[tuple[PlannedTrade, float, float]] = []
    closed_returns: list[float] = []
    ledger: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    cooldown_until: dt.date | None = None
    last_regime_trade_count = 0
    closed_trade_count = 0
    hard_stop_count = 0
    fee_rate = args.fee_bps / 10000

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
            last_regime_trade_count,
        )

        candidates = planned_by_entry.get(date_value, [])
        for planned in candidates:
            if regime.action == "skip":
                break
            if len(open_positions) >= args.max_positions:
                break
            if any(position[0].ticker == planned.ticker for position in open_positions):
                continue
            if not planned_passes_dynamic_filters(planned, overrides):
                continue
            if cash <= 0:
                continue
            slots = max(1, args.max_positions - len(open_positions))
            capital = cash / slots
            if regime.action == "reduce":
                capital *= max(0.0, min(1.0, args.regime_risk_factor))
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
                closed_trade_count += 1
                closed_returns.append(planned.return_pct)
                if planned.exit_reason == "hard_stop":
                    hard_stop_count += 1
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
    summary = {
        "period": period,
        "start_date": trading_dates[0].isoformat() if trading_dates else start_date.isoformat(),
        "end_date": trading_dates[-1].isoformat() if trading_dates else end_date.isoformat(),
        "initial_cash": round(args.initial_cash, 2),
        "final_cash": round(cash, 2),
        "final_equity": round(final_equity, 2),
        "return_pct": round((final_equity / args.initial_cash - 1) * 100, 4) if args.initial_cash else 0.0,
        "max_drawdown_pct": round(max_drawdown(daily, args.initial_cash), 4),
        "closed_trades": closed_trade_count,
        "win_rate_pct": round(wins / closed_trade_count * 100, 4) if closed_trade_count else 0.0,
        "hard_stop_count": hard_stop_count,
        "open_positions_end": len(open_positions),
    }
    return ledger, daily, summary


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if rows:
            writer.writeheader()
            writer.writerows(rows)


def write_outputs(out_dir: Path, prefix: str, ledger: list[dict[str, Any]], daily: list[dict[str, Any]], summary: list[dict[str, Any]]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_csv = out_dir / f"{prefix}_ledger.csv"
    daily_csv = out_dir / f"{prefix}_daily_equity.csv"
    summary_csv = out_dir / f"{prefix}_summary.csv"
    write_csv(ledger_csv, ledger)
    write_csv(daily_csv, daily)
    write_csv(summary_csv, summary)
    outputs = {"ledger_csv": ledger_csv, "daily_csv": daily_csv, "summary_csv": summary_csv}
    try:
        import pandas as pd  # type: ignore

        xlsx = out_dir / f"{prefix}.xlsx"
        with pd.ExcelWriter(xlsx) as writer:
            pd.DataFrame(summary).to_excel(writer, sheet_name="summary", index=False)
            pd.DataFrame(ledger).to_excel(writer, sheet_name="ledger", index=False)
            pd.DataFrame(daily).to_excel(writer, sheet_name="daily_equity", index=False)
        outputs["xlsx"] = xlsx
    except Exception as exc:
        print(f"warning: xlsx export skipped: {exc}", flush=True)
    return outputs


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run current strategy backtest and export a cash/equity ledger.")
    parser.add_argument("--watchlist", default="config/watchlist.mainboard_liquid.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES))
    parser.add_argument("--end-date", default=default_end_date().isoformat())
    parser.add_argument("--period-months", default="3,6")
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
    parser.add_argument("--event-bonus", type=float, default=0.0)
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
    parser.add_argument("--market-ma-days", type=int, default=20)
    parser.add_argument("--market-lookback-days", type=int, default=5)
    parser.add_argument("--market-min-return", type=float, default=-0.04)
    parser.add_argument("--ma5-mode", choices=["ignore", "filter", "pullback"], default="ignore")
    parser.add_argument("--ma5-pullback-limit", type=float, default=0.025)
    parser.add_argument("--ma5-extension-limit", type=float, default=0.04)
    parser.add_argument("--sector-mode", choices=["ignore", "filter", "strong"], default="ignore")
    parser.add_argument("--min-sector-momentum-5d", type=float, default=-0.03)
    parser.add_argument("--min-sector-above-ma20-ratio", type=float, default=0.35)
    parser.add_argument("--max-gap-up", type=float, default=0.02)
    parser.add_argument("--max-gap-down", type=float, default=0.03)
    parser.add_argument("--confirm-buffer", type=float, default=0.0)
    parser.add_argument("--max-entry-extension", type=float, default=0.04)
    parser.add_argument("--dynamic-params", action="store_true", default=True)
    parser.add_argument("--hot-min-score", type=float, default=90.0)
    parser.add_argument("--hot-max-gap-up", type=float, default=0.02)
    parser.add_argument("--hot-gap-volume-min-ratio", type=float, default=1.3)
    parser.add_argument("--hot-max-5d-range-pct", type=float, default=32.0)
    parser.add_argument("--hot-max-momentum-10d-pct", type=float, default=26.0)
    parser.add_argument("--hot-max-close-position-20d-pct", type=float, default=85.0)
    parser.add_argument("--normal-min-score", type=float, default=90.0)
    parser.add_argument("--normal-max-gap-up", type=float, default=0.02)
    parser.add_argument("--normal-gap-volume-min-ratio", type=float, default=1.3)
    parser.add_argument("--normal-max-5d-range-pct", type=float, default=32.0)
    parser.add_argument("--normal-max-momentum-10d-pct", type=float, default=26.0)
    parser.add_argument("--normal-max-close-position-20d-pct", type=float, default=85.0)
    parser.add_argument("--narrow-rally-min-score", type=float, default=90.0)
    parser.add_argument("--narrow-rally-max-gap-up", type=float, default=0.01)
    parser.add_argument("--narrow-rally-gap-volume-min-ratio", type=float, default=1.35)
    parser.add_argument("--narrow-rally-max-5d-range-pct", type=float, default=25.0)
    parser.add_argument("--narrow-rally-max-momentum-10d-pct", type=float, default=20.0)
    parser.add_argument("--narrow-rally-max-close-position-20d-pct", type=float, default=80.0)
    parser.add_argument("--cold-min-score", type=float, default=90.0)
    parser.add_argument("--cold-max-gap-up", type=float, default=0.01)
    parser.add_argument("--cold-gap-volume-min-ratio", type=float, default=1.5)
    parser.add_argument("--cold-max-5d-range-pct", type=float, default=25.0)
    parser.add_argument("--cold-max-momentum-10d-pct", type=float, default=20.0)
    parser.add_argument("--cold-max-close-position-20d-pct", type=float, default=80.0)
    parser.add_argument("--history-timeout", type=float, default=5.0)
    parser.add_argument("--daily-cache-dir", default="output/backtest_daily_cache")
    parser.add_argument("--out-dir", default="output/backtest_existing_strategy")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    end_date = parse_date(args.end_date)
    if not end_date:
        raise SystemExit("end date must be YYYY-MM-DD")
    periods = sorted({int(item.strip()) for item in args.period_months.split(",") if item.strip()}, reverse=True)
    if not periods:
        raise SystemExit("no periods")
    earliest_start = subtract_months(end_date, max(periods))
    fetch_start = earliest_start - dt.timedelta(days=170)
    fetch_end = end_date + dt.timedelta(days=args.horizon * 3 + 10)

    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    price_map: dict[str, list[PriceBar]] = {}
    cache_dir = Path(args.daily_cache_dir)
    for index, symbol in enumerate(symbols, 1):
        try:
            price_map[symbol.ticker] = cached_history(
                session,
                symbol.yahoo_symbol or symbol.ticker,
                symbol.ticker,
                fetch_start,
                fetch_end,
                cache_dir,
                args.history_timeout,
            )
        except Exception as exc:
            print(f"warning: history failed {index}/{len(symbols)} {symbol.ticker} {symbol.name}: {type(exc).__name__}: {exc}", flush=True)
            price_map[symbol.ticker] = []
        if index == 1 or index % 25 == 0 or index == len(symbols):
            print(f"loaded daily history {index}/{len(symbols)}", flush=True)
        time.sleep(0.02)

    all_ledger: list[dict[str, Any]] = []
    all_daily: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for months in sorted(periods):
        start_date = subtract_months(end_date, months)
        label = f"{months}M"
        print(f"simulating {label}: {start_date} to {end_date}", flush=True)
        rows = build_signal_rows(
            symbols,
            price_map,
            start_date,
            end_date,
            args.horizon,
            {},
            args.min_traded_value,
            args.take_profit,
            args.hard_stop,
            args.trailing_stop,
        )
        planned = build_planned_by_entry(rows, price_map, end_date, args)
        ledger, daily, summary = simulate_period(label, planned, price_map, start_date, end_date, args)
        summary.update(
            {
                "event_file": "disabled",
                "event_weight": 0,
                "watchlist": args.watchlist,
                "max_positions": args.max_positions,
                "min_score": args.min_score,
                "fee_bps": args.fee_bps,
            }
        )
        all_ledger.extend(ledger)
        all_daily.extend(daily)
        summaries.append(summary)
        print(f"{label}: trades={summary['closed_trades']} final_equity={summary['final_equity']} return={summary['return_pct']}%", flush=True)

    prefix = f"existing_strategy_no_events_{min(periods)}M_{max(periods)}M_to_{end_date:%Y%m%d}"
    outputs = write_outputs(Path(args.out_dir), prefix, all_ledger, all_daily, summaries)
    for name, path in outputs.items():
        print(f"{name}={path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
