#!/usr/bin/env python3
"""Optimize short-term signal weights against the existing 5m VWAP backtest."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dependency_bootstrap import ensure_project_dependencies  # noqa: E402

ensure_project_dependencies()

import requests

from intraday_vwap_backtest import (  # noqa: E402
    build_arg_parser as build_intraday_arg_parser,
    build_planned_by_entry,
    build_signal_rows,
    prefetch_intraday,
    result_for_preplanned,
    write_report,
    write_trades,
)
from market_universe import filter_symbols  # noqa: E402
from optimize_time_weighted_strategy import month_windows, recency_weights  # noqa: E402
from short_term_pattern_miner import DEFAULT_SIGNAL_WEIGHTS, PatternRow, event_score_by_symbol  # noqa: E402
from tech_event_backtest import PriceBar, fetch_yahoo_history  # noqa: E402
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date  # noqa: E402


WEIGHT_PROFILES: dict[str, dict[str, float]] = {
    "balanced": {
        "liquidity": 16,
        "value": 18,
        "volatility": 14,
        "range": 16,
        "momentum": 12,
        "trend": 16,
        "event": 10,
        "overheat_penalty": 10,
    },
    "market_data_heavy": {
        "liquidity": 22,
        "value": 24,
        "volatility": 14,
        "range": 18,
        "momentum": 12,
        "trend": 18,
        "event": 4,
        "overheat_penalty": 12,
    },
    "event_light": {
        "liquidity": 20,
        "value": 22,
        "volatility": 15,
        "range": 18,
        "momentum": 14,
        "trend": 18,
        "event": 0,
        "overheat_penalty": 14,
    },
    "event_plus_market": {
        "liquidity": 16,
        "value": 18,
        "volatility": 14,
        "range": 16,
        "momentum": 10,
        "trend": 15,
        "event": 18,
        "overheat_penalty": 10,
    },
    "trend_quality": {
        "liquidity": 16,
        "value": 16,
        "volatility": 10,
        "range": 14,
        "momentum": 14,
        "trend": 28,
        "event": 6,
        "overheat_penalty": 18,
    },
    "volatility_breakout": {
        "liquidity": 14,
        "value": 18,
        "volatility": 22,
        "range": 24,
        "momentum": 10,
        "trend": 12,
        "event": 6,
        "overheat_penalty": 16,
    },
    "volatility_breakout_no_event": dict(DEFAULT_SIGNAL_WEIGHTS),
}


def component_value(value: float, high: float, mid: float, mid_score: float = 0.55) -> float:
    if value >= high:
        return 1.0
    if value >= mid:
        return mid_score
    return 0.0


def score_row(row: PatternRow, event_score: int, weights: dict[str, float], min_traded_value: float) -> float:
    score = 0.0
    score += weights["liquidity"] * component_value(row.traded_value, min_traded_value, min_traded_value * 0.4, 0.45)
    score += weights["value"] * component_value(row.traded_value_ratio, 1.8, 1.2)
    score += weights["volatility"] * component_value(row.atr_pct, 6.0, 3.5)
    score += weights["range"] * component_value(row.max_5d_range_pct, 12.0, 8.0, 0.65)

    momentum_score = 0.0
    if 2 <= row.momentum_3d_pct <= 14:
        momentum_score += 0.5
    elif row.momentum_3d_pct > 20:
        momentum_score -= 0.5
    if 4 <= row.momentum_10d_pct <= 28:
        momentum_score += 0.5
    elif row.momentum_10d_pct > 38:
        momentum_score -= 0.6
    if row.value_ratio_3d >= 1.25 and row.traded_value_ratio >= 1.0:
        momentum_score += 0.25
    score += weights["momentum"] * momentum_score

    trend_score = 0.0
    if row.above_ma5:
        trend_score += 0.28
    if row.above_ma20:
        trend_score += 0.36
    if -8 <= row.distance_to_20d_high_pct <= 0:
        trend_score += 0.24
    if 55 <= row.close_position_20d_pct <= 85:
        trend_score += 0.12
    score += weights["trend"] * trend_score

    event_component = 1.0 if event_score >= 80 else 0.55 if event_score >= 60 else 0.0
    score += weights["event"] * event_component

    penalty = 0.0
    if row.close_position_20d_pct >= 82 and row.distance_to_ma5_pct > 6:
        penalty += 0.6
    if row.change_1d_pct <= -6:
        penalty += 0.5
    if row.momentum_10d_pct > 38:
        penalty += 0.4
    score -= weights["overheat_penalty"] * penalty
    return round(score, 2)


def setup_for_weighted_row(row: PatternRow, event_score: int, event_weight: float) -> str:
    if event_weight > 0 and event_score >= 60:
        return "EVENT_PLUS_VOLATILITY"
    if row.traded_value_ratio >= 1.5 and row.max_5d_range_pct >= 10:
        return "VOLUME_BREAKOUT"
    if row.max_5d_range_pct >= 12 and row.atr_pct >= 3.5:
        return "HIGH_VOLATILITY"
    return "BACKGROUND_WATCH"


def reweight_rows(
    rows: list[PatternRow],
    event_scores: dict[str, int],
    weights: dict[str, float],
    min_traded_value: float,
) -> list[PatternRow]:
    weighted: list[PatternRow] = []
    for row in rows:
        event_score = event_scores.get(row.ticker, 0)
        weighted.append(
            replace(
                row,
                score=score_row(row, event_score, weights, min_traded_value),
                setup_type=setup_for_weighted_row(row, event_score, weights["event"]),
            )
        )
    return weighted


def clone_args(args: argparse.Namespace, **updates: Any) -> argparse.Namespace:
    values = vars(args).copy()
    values.update(updates)
    return argparse.Namespace(**values)


def load_daily_context_limited(args: argparse.Namespace) -> tuple[list[Any], dict[str, list[PriceBar]], dict[str, int]]:
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("dates must be YYYY-MM-DD")
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    if args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]
    event_scores = event_score_by_symbol(Path(args.events))
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    fetch_start = start_date - dt.timedelta(days=120)
    fetch_end = end_date + dt.timedelta(days=args.horizon * 3 + 15)
    price_map: dict[str, list[PriceBar]] = {}
    for index, symbol in enumerate(symbols, 1):
        try:
            price_map[symbol.ticker] = fetch_yahoo_history(session, symbol.yahoo_symbol or symbol.ticker, fetch_start, fetch_end)
        except Exception:
            price_map[symbol.ticker] = []
        if index % 20 == 0:
            print(f"loaded daily bars {index}/{len(symbols)} symbols")
        time.sleep(0.03)
    return symbols, price_map, event_scores


def robust_score(result: dict[str, Any], min_trades: int, trade_penalty: float, drawdown_penalty: float) -> float:
    shortfall = max(0, min_trades - int(result["full_trades"]))
    return round(
        float(result["weighted_return_pct"])
        - drawdown_penalty * float(result["weighted_drawdown_pct"])
        + 0.8 * float(result["worst_month_pct"])
        - 5.0 * int(result["negative_months"])
        - trade_penalty * shortfall,
        4,
    )


def write_weight_report(path: Path, grid_csv: Path, best: dict[str, Any], top_rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Signal Weight Optimization - {args.start_date} to {args.end_date}",
        "",
        "This test re-scores the same historical features, then runs the existing BaoStock 5m VWAP execution model.",
        "",
        f"- Grid CSV: `{grid_csv}`",
        f"- Best profile: `{best['weight_profile']}`",
        f"- Best min score: `{best['min_score']}`",
        f"- Robust score: `{best['robust_score']}`",
        f"- Weighted return: `{best['weighted_return_pct']}%`",
        f"- Weighted drawdown: `{best['weighted_drawdown_pct']}%`",
        f"- Worst month: `{best['worst_month_pct']}%`",
        f"- Negative months: `{best['negative_months']}`",
        f"- Full return: `{best['full_return_pct']}%`",
        f"- Full trades: `{best['full_trades']}`",
        "",
        "## Best Weights",
        "",
    ]
    for key in ("liquidity", "value", "volatility", "range", "momentum", "trend", "event", "overheat_penalty"):
        lines.append(f"- {key}: `{best[key]}`")
    lines.extend(
        [
            "",
            "## Top Results",
            "",
            "| Rank | Profile | Min Score | Robust | Weighted Return | Drawdown | Worst Month | Neg Months | Full Return | Trades |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for index, row in enumerate(top_rows, 1):
        lines.append(
            "| {rank} | {profile} | {min_score} | {robust} | {ret}% | {dd}% | {worst}% | {neg} | {full}% | {trades} |".format(
                rank=index,
                profile=row["weight_profile"],
                min_score=row["min_score"],
                robust=row["robust_score"],
                ret=row["weighted_return_pct"],
                dd=row["weighted_drawdown_pct"],
                worst=row["worst_month_pct"],
                neg=row["negative_months"],
                full=row["full_return_pct"],
                trades=row["full_trades"],
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- Prefer the robust-score winner over the raw-return winner unless trade count is too low.",
            "- If two profiles are close, choose the one with lower event weight and lower drawdown.",
            "- Re-run after refreshing the watchlist or event file; these weights are data-period dependent.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = build_intraday_arg_parser()
    parser.description = "Optimize signal data-source weights before the existing 5m VWAP execution model."
    parser.add_argument("--weight-profiles", default=",".join(WEIGHT_PROFILES), help="comma list of built-in profile names")
    parser.add_argument("--min-scores", default="70,80,90")
    parser.add_argument("--max-symbols", type=int, default=0, help="use only the first N rows of the watchlist; 0 means all")
    parser.add_argument("--min-full-trades", type=int, default=5)
    parser.add_argument("--robust-drawdown-penalty", type=float, default=0.8)
    parser.add_argument("--trade-penalty-under-min", type=float, default=3.0)
    parser.add_argument("--weights-out", default="output/signal_weight_optimization.md")
    parser.set_defaults(
        opt_out="output/signal_weight_optimization_grid.csv",
        out="output/signal_weight_optimization_best.md",
        csv_out="output/signal_weight_optimization_best_trades.csv",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("dates must be YYYY-MM-DD")

    profile_names = [item.strip() for item in args.weight_profiles.split(",") if item.strip()]
    unknown = [name for name in profile_names if name not in WEIGHT_PROFILES]
    if unknown:
        raise SystemExit(f"unknown weight profile(s): {', '.join(unknown)}")
    min_scores = [float(item.strip()) for item in args.min_scores.split(",") if item.strip()]

    symbols, price_map, event_scores = load_daily_context_limited(args)
    windows = month_windows(start_date, end_date)
    weights_by_month = recency_weights(len(windows), args.decay)

    base_rows_by_label: dict[str, list[PatternRow]] = {}
    for window_start, window_end, label in windows:
        base_rows_by_label[label] = build_signal_rows(
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
    base_rows_by_label["full"] = build_signal_rows(
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

    prefetch_args = clone_args(args, min_score=min(min_scores))
    broad_planned: dict[dt.date, list[Any]] = {}
    for profile_name in profile_names:
        broad_rows = reweight_rows(base_rows_by_label["full"], event_scores, WEIGHT_PROFILES[profile_name], args.min_traded_value)
        planned_for_profile = build_planned_by_entry(broad_rows, price_map, end_date, prefetch_args)
        for entry_date, plans in planned_for_profile.items():
            broad_planned.setdefault(entry_date, []).extend(plans)
    intraday_map = prefetch_intraday(broad_planned, args, price_map)

    results: list[dict[str, Any]] = []
    best_payload: tuple[dict[str, Any], list[dict[str, Any]], list[Any], argparse.Namespace] | None = None
    for profile_name in profile_names:
        profile = WEIGHT_PROFILES[profile_name]
        weighted_rows_by_label = {
            label: reweight_rows(rows, event_scores, profile, args.min_traded_value)
            for label, rows in base_rows_by_label.items()
        }
        for min_score in min_scores:
            combo_args = clone_args(args, min_score=min_score)
            monthly_planned = {
                label: build_planned_by_entry(weighted_rows_by_label[label], price_map, window_end, combo_args)
                for _, window_end, label in windows
            }
            full_planned = build_planned_by_entry(weighted_rows_by_label["full"], price_map, end_date, combo_args)
            payload = result_for_preplanned(windows, weights_by_month, monthly_planned, full_planned, price_map, intraday_map, combo_args)
            result = payload[0]
            result.update(
                {
                    "robust_score": robust_score(result, args.min_full_trades, args.trade_penalty_under_min, args.robust_drawdown_penalty),
                    "weight_profile": profile_name,
                    "min_score": min_score,
                    **profile,
                }
            )
            results.append(result)
            if best_payload is None or (result["robust_score"], result["weighted_return_pct"]) > (
                best_payload[0]["robust_score"],
                best_payload[0]["weighted_return_pct"],
            ):
                best_payload = (result, payload[1], payload[2], combo_args)
            print(result)

    results.sort(key=lambda item: (item["robust_score"], item["weighted_return_pct"]), reverse=True)
    grid_path = Path(args.opt_out)
    grid_path.parent.mkdir(parents=True, exist_ok=True)
    with grid_path.open("w", encoding="utf-8-sig", newline="") as handle:
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
    write_weight_report(Path(args.weights_out), grid_path, best, results[:10], args)
    print(f"best={best}")
    print(f"weights_report={args.weights_out}")
    print(f"backtest_report={args.out}")
    print(f"trades_csv={trades_csv}")
    print(f"grid_csv={grid_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
