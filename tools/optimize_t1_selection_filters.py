#!/usr/bin/env python3
"""Optimize T+1 intraday selection filters for recent A-share short-term tests."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intraday_vwap_backtest import (
    build_arg_parser as build_intraday_arg_parser,
    build_planned_by_entry,
    build_signal_rows,
    load_daily_context,
    prefetch_intraday,
    result_for_preplanned,
    write_report,
    write_trades,
)
from optimize_short_term_strategy import parse_float_list, parse_int_list, parse_str_list
from optimize_time_weighted_strategy import month_windows, recency_weights
from tech_event_radar import parse_date


def build_arg_parser() -> argparse.ArgumentParser:
    parser = build_intraday_arg_parser()
    parser.description = "Optimize T+1 selection filters for the 5m VWAP A-share model."
    parser.add_argument("--selection-modes", default="score,score_low_heat")
    parser.add_argument("--max-atr-pcts", default="0,7.5,8.5")
    parser.add_argument("--max-5d-range-pcts", default="0,24,28")
    parser.add_argument("--max-momentum-10d-pcts", default="999,12,18")
    parser.add_argument("--max-close-position-20d-pcts", default="80,85,90,100")
    parser.add_argument("--max-distance-to-20d-high-pcts", default="999,-2,-4")
    parser.add_argument("--symbol-cooldown-days-list", default="0,3,5")
    parser.add_argument("--gap-volume-thresholds", default="0,0.02")
    parser.add_argument("--gap-volume-min-ratios", default="0,1.5")
    parser.add_argument("--min-full-trades", type=int, default=5)
    parser.add_argument("--robust-drawdown-penalty", type=float, default=0.8)
    parser.add_argument("--trade-penalty-under-min", type=float, default=3.0)
    parser.add_argument("--quiet", action="store_true")
    return parser


def clone_args(args: argparse.Namespace, **updates: Any) -> argparse.Namespace:
    values = vars(args).copy()
    values.update(updates)
    return argparse.Namespace(**values)


def build_planned_sets(
    rows_by_label: dict[str, list[Any]],
    price_map: dict[str, list[Any]],
    args: argparse.Namespace,
    windows: list[tuple[dt.date, dt.date, str]],
    end_date: dt.date,
) -> tuple[dict[str, dict[dt.date, list[Any]]], dict[dt.date, list[Any]]]:
    monthly_planned = {
        label: build_planned_by_entry(rows_by_label[label], price_map, window_end, args)
        for _, window_end, label in windows
    }
    full_planned = build_planned_by_entry(rows_by_label["full"], price_map, end_date, args)
    return monthly_planned, full_planned


def robust_score(result: dict[str, Any], args: argparse.Namespace) -> float:
    shortfall = max(0, args.min_full_trades - int(result["full_trades"]))
    return (
        float(result["weighted_return_pct"])
        - args.robust_drawdown_penalty * float(result["weighted_drawdown_pct"])
        + 0.8 * float(result["worst_month_pct"])
        - 5.0 * int(result["negative_months"])
        - args.trade_penalty_under_min * shortfall
    )


def main() -> int:
    args = build_arg_parser().parse_args()
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("dates must be YYYY-MM-DD")

    symbols, price_map, event_scores = load_daily_context(args)
    windows = month_windows(start_date, end_date)
    weights = recency_weights(len(windows), args.decay)

    rows_by_label: dict[str, list[Any]] = {}
    for window_start, window_end, label in windows:
        rows_by_label[label] = build_signal_rows(
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
    rows_by_label["full"] = build_signal_rows(
        symbols,
        price_map,
        start_date,
        end_date,
        args.horizon,
        event_scores,
        args.min_traded_value,
        args.take_profit,
        args.hard_stop,
        args.trailing_stop,
    )

    prefetch_args = clone_args(
        args,
        max_atr_pct=0.0,
        max_5d_range_pct=0.0,
        max_momentum_10d_pct=999.0,
        max_close_position_20d_pct=100.0,
        max_distance_to_20d_high_pct=999.0,
        symbol_cooldown_days=0,
        selection_mode="score",
    )
    _, broad_full_planned = build_planned_sets(rows_by_label, price_map, prefetch_args, windows, end_date)
    intraday_map = prefetch_intraday(broad_full_planned, args)

    combos = itertools.product(
        parse_str_list(args.selection_modes),
        parse_float_list(args.max_atr_pcts),
        parse_float_list(args.max_5d_range_pcts),
        parse_float_list(args.max_momentum_10d_pcts),
        parse_float_list(args.max_close_position_20d_pcts),
        parse_float_list(args.max_distance_to_20d_high_pcts),
        parse_int_list(args.symbol_cooldown_days_list),
        parse_float_list(args.gap_volume_thresholds),
        parse_float_list(args.gap_volume_min_ratios),
        parse_str_list(args.entry_end_times),
        parse_float_list(args.max_gap_ups),
        parse_float_list(args.confirm_buffers),
        parse_float_list(args.vwap_buffers),
        parse_float_list(args.max_entry_extensions),
        parse_int_list(args.vwap_fail_bars_list),
    )

    results: list[dict[str, Any]] = []
    best_payload: tuple[dict[str, Any], list[dict[str, Any]], list[Any], argparse.Namespace] | None = None
    for combo in combos:
        (
            selection_mode,
            max_atr_pct,
            max_5d_range_pct,
            max_momentum_10d_pct,
            max_close_position_20d_pct,
            max_distance_to_20d_high_pct,
            symbol_cooldown_days,
            gap_volume_threshold,
            gap_volume_min_ratio,
            entry_end_time,
            max_gap_up,
            confirm_buffer,
            vwap_buffer,
            max_entry_extension,
            vwap_fail_bars,
        ) = combo
        combo_args = clone_args(
            args,
            selection_mode=selection_mode,
            max_atr_pct=max_atr_pct,
            max_5d_range_pct=max_5d_range_pct,
            max_momentum_10d_pct=max_momentum_10d_pct,
            max_close_position_20d_pct=max_close_position_20d_pct,
            max_distance_to_20d_high_pct=max_distance_to_20d_high_pct,
            symbol_cooldown_days=symbol_cooldown_days,
            gap_volume_threshold=gap_volume_threshold,
            gap_volume_min_ratio=gap_volume_min_ratio,
            entry_end_time=entry_end_time,
            max_gap_up=max_gap_up,
            confirm_buffer=confirm_buffer,
            vwap_buffer=vwap_buffer,
            max_entry_extension=max_entry_extension,
            vwap_fail_bars=vwap_fail_bars,
        )
        monthly_planned, full_planned = build_planned_sets(rows_by_label, price_map, combo_args, windows, end_date)
        payload = result_for_preplanned(windows, weights, monthly_planned, full_planned, price_map, intraday_map, combo_args)
        result = payload[0]
        result.update(
            {
                "robust_score": round(robust_score(result, args), 4),
                "selection_mode": selection_mode,
                "max_atr_pct": max_atr_pct,
                "max_5d_range_pct": max_5d_range_pct,
                "max_momentum_10d_pct": max_momentum_10d_pct,
                "max_close_position_20d_pct": max_close_position_20d_pct,
                "max_distance_to_20d_high_pct": max_distance_to_20d_high_pct,
                "symbol_cooldown_days": symbol_cooldown_days,
                "gap_volume_threshold": gap_volume_threshold,
                "gap_volume_min_ratio": gap_volume_min_ratio,
            }
        )
        results.append(result)
        if best_payload is None or (result["robust_score"], result["weighted_return_pct"]) > (
            best_payload[0]["robust_score"],
            best_payload[0]["weighted_return_pct"],
        ):
            best_payload = (result, payload[1], payload[2], combo_args)
        if not args.quiet:
            print(result)

    results.sort(key=lambda item: (item["robust_score"], item["weighted_return_pct"]), reverse=True)
    opt_path = Path(args.opt_out)
    opt_path.parent.mkdir(parents=True, exist_ok=True)
    with opt_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()) if results else [])
        if results:
            writer.writeheader()
            writer.writerows(results)

    if best_payload is None:
        raise SystemExit("no result")
    best, monthly_rows, trades, best_args = best_payload
    trades_csv = Path(args.csv_out)
    write_trades(trades_csv, trades)
    write_report(Path(args.out), trades_csv, best, monthly_rows, trades, best_args)
    print(f"best={best}")
    print(f"report={args.out}")
    print(f"trades_csv={trades_csv}")
    print(f"grid_csv={opt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
