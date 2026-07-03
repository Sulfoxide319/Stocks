#!/usr/bin/env python3
"""Optimize short-term strategy parameters with recency-weighted monthly results."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import math
from pathlib import Path
from typing import Any

import requests

from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from optimize_short_term_strategy import parse_float_list, parse_int_list, parse_str_list
from short_term_pattern_miner import event_score_by_symbol
from short_term_strategy_backtest import build_signal_rows, max_drawdown, simulate_portfolio
from tech_event_backtest import fetch_yahoo_history
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date


def month_windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date, str]]:
    windows: list[tuple[dt.date, dt.date, str]] = []
    cursor = dt.date(start.year, start.month, 1)
    while cursor < end:
        next_month = dt.date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
        window_start = max(start, cursor)
        window_end = min(end, next_month)
        if window_start < window_end:
            windows.append((window_start, window_end, f"{cursor:%Y%m}"))
        cursor = next_month
    return windows


def recency_weights(count: int, decay: float) -> list[float]:
    raw = [decay ** (count - index - 1) for index in range(count)]
    total = sum(raw) or 1.0
    return [value / total * count for value in raw]


def main() -> int:
    parser = argparse.ArgumentParser(description="Time-weighted monthly optimizer.")
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
    parser.add_argument("--max-positions", default="2")
    parser.add_argument("--min-scores", default="75,80,85")
    parser.add_argument("--target-atr-mults", default="0.9")
    parser.add_argument("--target-range-mults", default="0.35")
    parser.add_argument("--stop-atr-mults", default="0.5,0.55,0.6")
    parser.add_argument("--trail-atr-mults", default="0.25,0.35")
    parser.add_argument("--regime-modes", default="skip")
    parser.add_argument("--regime-lookback-trades", default="8,12")
    parser.add_argument("--regime-min-win-rates", default="0.3,0.35")
    parser.add_argument("--regime-max-hard-stop-rates", default="0.45,0.5")
    parser.add_argument("--regime-max-drawdowns", default="0.06,0.08")
    parser.add_argument("--regime-cooldown-days", default="5,8")
    parser.add_argument("--ma5-modes", default="ignore")
    parser.add_argument("--ma5-extension-limits", default="0.04,0.05,0.06")
    parser.add_argument("--sector-modes", default="filter,strong")
    parser.add_argument("--min-sector-momentum-5ds", default="-0.03,0,0.03")
    parser.add_argument("--min-sector-above-ma20-ratios", default="0.5,0.65,0.75")
    parser.add_argument("--execution-models", default="open,confirm")
    parser.add_argument("--max-gap-ups", default="0.03,0.04")
    parser.add_argument("--max-gap-downs", default="0.03")
    parser.add_argument("--confirm-buffers", default="0.0,0.003,0.005")
    parser.add_argument("--max-entry-extensions", default="0.04,0.05")
    parser.add_argument("--intraday-fail-exits", default="false,true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("dates must be YYYY-MM-DD")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    event_scores = event_score_by_symbol(Path(args.events))
    fetch_start = start_date - dt.timedelta(days=120)
    fetch_end = end_date + dt.timedelta(days=20)
    price_map = {}
    for symbol in symbols:
        try:
            price_map[symbol.ticker] = fetch_yahoo_history(session, symbol.yahoo_symbol or symbol.ticker, fetch_start, fetch_end)
        except Exception:
            price_map[symbol.ticker] = []

    windows = month_windows(start_date, end_date)
    weights = recency_weights(len(windows), args.decay)
    monthly_rows = [
        (
            window_start,
            window_end,
            label,
            weight,
            build_signal_rows(symbols, price_map, window_start, window_end, 3, event_scores, 200_000_000, 0.10, 0.04, 0.035),
        )
        for (window_start, window_end, label), weight in zip(windows, weights)
    ]
    full_rows = build_signal_rows(symbols, price_map, start_date, end_date, 3, event_scores, 200_000_000, 0.10, 0.04, 0.035)
    setup_allow = {"EVENT_PLUS_VOLATILITY", "VOLUME_BREAKOUT", "HIGH_VOLATILITY"}

    regime_combos: list[tuple[str, int, float, float, float, int]] = []
    for mode in parse_str_list(args.regime_modes):
        for combo in itertools.product(
            parse_int_list(args.regime_lookback_trades),
            parse_float_list(args.regime_min_win_rates),
            parse_float_list(args.regime_max_hard_stop_rates),
            parse_float_list(args.regime_max_drawdowns),
            parse_int_list(args.regime_cooldown_days),
        ):
            regime_combos.append((mode, *combo))

    ma5_combos = []
    for mode in parse_str_list(args.ma5_modes):
        for extension_limit in parse_float_list(args.ma5_extension_limits):
            ma5_combos.append((mode, 0.025, extension_limit))

    sector_combos = []
    for mode in parse_str_list(args.sector_modes):
        for momentum, above_ratio in itertools.product(
            parse_float_list(args.min_sector_momentum_5ds),
            parse_float_list(args.min_sector_above_ma20_ratios),
        ):
            sector_combos.append((mode, momentum, above_ratio))

    execution_combos = []
    for model in parse_str_list(args.execution_models):
        if model not in {"open", "confirm"}:
            raise SystemExit("--execution-models items must be open or confirm")
        if model == "open":
            execution_combos.append(("open", 0.04, 0.03, 0.003, 0.05, False))
            continue
        for gap_up, gap_down, buffer, extension, fail_exit in itertools.product(
            parse_float_list(args.max_gap_ups),
            parse_float_list(args.max_gap_downs),
            parse_float_list(args.confirm_buffers),
            parse_float_list(args.max_entry_extensions),
            parse_str_list(args.intraday_fail_exits),
        ):
            execution_combos.append(("confirm", gap_up, gap_down, buffer, extension, fail_exit.lower() in {"1", "true", "yes"}))

    results: list[dict[str, Any]] = []
    combos = itertools.product(
        parse_int_list(args.max_positions),
        parse_float_list(args.min_scores),
        parse_float_list(args.target_atr_mults),
        parse_float_list(args.target_range_mults),
        parse_float_list(args.stop_atr_mults),
        parse_float_list(args.trail_atr_mults),
        regime_combos,
        ma5_combos,
        sector_combos,
        execution_combos,
    )
    for max_positions, min_score, target_atr, target_range, stop_atr, trail_atr, regime, ma5, sector, execution in combos:
        regime_mode, regime_lookback, regime_min_win, regime_max_hard_stop, regime_max_dd, regime_cooldown = regime
        ma5_mode, ma5_pullback, ma5_extension = ma5
        sector_mode, sector_momentum, sector_above = sector
        execution_model, max_gap_up, max_gap_down, confirm_buffer, max_entry_extension, intraday_fail_exit = execution
        monthly_returns: list[float] = []
        monthly_drawdowns: list[float] = []
        weighted_return = 0.0
        weighted_drawdown = 0.0
        monthly_trades = 0
        monthly_wins = 0
        for window_start, window_end, label, weight, rows in monthly_rows:
            trades, curve, final_equity = simulate_portfolio(
                rows,
                price_map,
                window_start,
                window_end,
                args.initial_cash,
                max_positions,
                min_score,
                setup_allow,
                3,
                0.10,
                0.04,
                0.035,
                True,
                target_atr,
                target_range,
                0.02,
                0.05,
                0.18,
                stop_atr,
                0.025,
                0.07,
                trail_atr,
                0.025,
                0.06,
                5.0,
                False,
                1,
                0.04,
                0.5,
                True,
                regime_mode,
                regime_lookback,
                8,
                regime_min_win,
                regime_max_hard_stop,
                regime_max_dd,
                regime_cooldown,
                0.35,
                [],
                20,
                5,
                -0.04,
                ma5_mode,
                ma5_pullback,
                ma5_extension,
                sector_mode,
                sector_momentum,
                sector_above,
                execution_model,
                max_gap_up,
                max_gap_down,
                confirm_buffer,
                max_entry_extension,
                intraday_fail_exit,
            )
            ret = (final_equity / args.initial_cash - 1) * 100
            dd = max_drawdown(curve, args.initial_cash)
            monthly_returns.append(ret)
            monthly_drawdowns.append(dd)
            weighted_return += ret * weight
            weighted_drawdown += dd * weight
            monthly_trades += len(trades)
            monthly_wins += sum(1 for trade in trades if trade.return_pct > 0)

        negative_months = sum(1 for ret in monthly_returns if ret < 0)
        worst_month = min(monthly_returns) if monthly_returns else 0.0
        monthly_compound = (math.prod(1 + ret / 100 for ret in monthly_returns) - 1) * 100 if monthly_returns else 0.0
        objective = (
            weighted_return
            - args.drawdown_penalty * weighted_drawdown
            - args.negative_month_penalty * negative_months
            + args.worst_month_penalty * worst_month
        )
        trades, curve, final_equity = simulate_portfolio(
            full_rows,
            price_map,
            start_date,
            end_date,
            args.initial_cash,
            max_positions,
            min_score,
            setup_allow,
            3,
            0.10,
            0.04,
            0.035,
            True,
            target_atr,
            target_range,
            0.02,
            0.05,
            0.18,
            stop_atr,
            0.025,
            0.07,
            trail_atr,
            0.025,
            0.06,
            5.0,
            False,
            1,
            0.04,
            0.5,
            True,
            regime_mode,
            regime_lookback,
            8,
            regime_min_win,
            regime_max_hard_stop,
            regime_max_dd,
            regime_cooldown,
            0.35,
            [],
            20,
            5,
            -0.04,
            ma5_mode,
            ma5_pullback,
            ma5_extension,
            sector_mode,
            sector_momentum,
            sector_above,
            execution_model,
            max_gap_up,
            max_gap_down,
            confirm_buffer,
            max_entry_extension,
            intraday_fail_exit,
        )
        full_return = (final_equity / args.initial_cash - 1) * 100
        full_dd = max_drawdown(curve, args.initial_cash)
        result = {
            "objective": round(objective, 4),
            "weighted_return_pct": round(weighted_return, 4),
            "weighted_drawdown_pct": round(weighted_drawdown, 4),
            "monthly_compound_pct": round(monthly_compound, 4),
            "worst_month_pct": round(worst_month, 4),
            "negative_months": negative_months,
            "monthly_trades": monthly_trades,
            "monthly_win_rate_pct": round(monthly_wins / monthly_trades * 100, 2) if monthly_trades else 0.0,
            "full_return_pct": round(full_return, 4),
            "full_drawdown_pct": round(full_dd, 4),
            "full_trades": len(trades),
            "full_win_rate_pct": round(sum(1 for trade in trades if trade.return_pct > 0) / len(trades) * 100, 2) if trades else 0.0,
            "max_positions": max_positions,
            "min_score": min_score,
            "target_atr_mult": target_atr,
            "target_range_mult": target_range,
            "stop_atr_mult": stop_atr,
            "trail_atr_mult": trail_atr,
            "regime_mode": regime_mode,
            "regime_lookback_trades": regime_lookback,
            "regime_min_win_rate": regime_min_win,
            "regime_max_hard_stop_rate": regime_max_hard_stop,
            "regime_max_drawdown": regime_max_dd,
            "regime_cooldown_days": regime_cooldown,
            "ma5_mode": ma5_mode,
            "ma5_extension_limit": ma5_extension,
            "sector_mode": sector_mode,
            "min_sector_momentum_5d": sector_momentum,
            "min_sector_above_ma20_ratio": sector_above,
            "execution_model": execution_model,
            "max_gap_up": max_gap_up,
            "max_gap_down": max_gap_down,
            "confirm_buffer": confirm_buffer,
            "max_entry_extension": max_entry_extension,
            "intraday_fail_exit": intraday_fail_exit,
        }
        for (_, _, label), ret, dd, weight in zip(windows, monthly_returns, monthly_drawdowns, weights):
            result[f"{label}_return_pct"] = round(ret, 4)
            result[f"{label}_drawdown_pct"] = round(dd, 4)
            result[f"{label}_weight"] = round(weight, 4)
        results.append(result)

    results.sort(key=lambda item: (item["objective"], item["weighted_return_pct"]), reverse=True)
    out_path = Path(args.out or f"output/time_weighted_strategy_{start_date:%Y%m%d}_{end_date:%Y%m%d}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()) if results else [])
        if results:
            writer.writeheader()
            writer.writerows(results)
    print(f"tested={len(results)} out={out_path}")
    for row in results[:10]:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
