#!/usr/bin/env python3
"""Optimize dynamic short-term exit parameters."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
from pathlib import Path
from typing import Any

from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from short_term_strategy_backtest import (
    build_signal_rows,
    max_drawdown,
    simulate_portfolio,
)
from tech_event_backtest import fetch_yahoo_history
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date
from short_term_pattern_miner import event_score_by_symbol

import requests


def parse_float_list(raw: str) -> list[float]:
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def parse_int_list(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def parse_str_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Grid optimize dynamic active exit parameters.")
    parser.add_argument("--watchlist", default="config/watchlist.example.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES), help="buyable ticker prefixes; use 'all' to disable")
    parser.add_argument("--events", default="output/tech_event_radar_20260702.json")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--max-positions", default="2,3")
    parser.add_argument("--min-scores", default="65,70,75")
    parser.add_argument("--target-atr-mults", default="0.6,0.9,1.2")
    parser.add_argument("--target-range-mults", default="0.15,0.25,0.35")
    parser.add_argument("--stop-atr-mults", default="0.4,0.55,0.7")
    parser.add_argument("--trail-atr-mults", default="0.35,0.45,0.6")
    parser.add_argument("--target-min", type=float, default=0.05)
    parser.add_argument("--target-max", type=float, default=0.18)
    parser.add_argument("--stop-min", type=float, default=0.025)
    parser.add_argument("--stop-max", type=float, default=0.07)
    parser.add_argument("--trail-min", type=float, default=0.025)
    parser.add_argument("--trail-max", type=float, default=0.06)
    parser.add_argument("--event-bonus", type=float, default=0.02)
    parser.add_argument("--drawdown-penalty", type=float, default=0.35)
    parser.add_argument("--regime-modes", default="off", help="comma list: off,skip,reduce")
    parser.add_argument("--regime-lookback-trades", default="12")
    parser.add_argument("--regime-min-trades", type=int, default=8)
    parser.add_argument("--regime-min-win-rates", default="0.35,0.40,0.45")
    parser.add_argument("--regime-max-hard-stop-rates", default="0.45,0.50,0.55")
    parser.add_argument("--regime-max-drawdowns", default="0.08,0.12")
    parser.add_argument("--regime-cooldown-days", default="3,5")
    parser.add_argument("--regime-risk-factors", default="0.25,0.35,0.50")
    parser.add_argument("--market-index-yahoo", default="")
    parser.add_argument("--market-ma-days", type=int, default=20)
    parser.add_argument("--market-lookback-days", type=int, default=5)
    parser.add_argument("--market-min-return", type=float, default=-0.04)
    parser.add_argument("--ma5-modes", default="ignore", help="comma list: ignore,filter,pullback")
    parser.add_argument("--ma5-pullback-limits", default="0.015,0.025,0.04")
    parser.add_argument("--ma5-extension-limits", default="0", help="comma list; 0 disables, e.g. 0,0.06,0.08")
    parser.add_argument("--sector-modes", default="ignore", help="comma list: ignore,filter,strong")
    parser.add_argument("--min-sector-momentum-5ds", default="-0.03,0,0.03")
    parser.add_argument("--min-sector-above-ma20-ratios", default="0.35,0.5,0.65")
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
    market_bars = []
    if args.market_index_yahoo:
        try:
            market_bars = fetch_yahoo_history(session, args.market_index_yahoo, fetch_start, fetch_end)
        except Exception:
            market_bars = []

    signal_rows = build_signal_rows(
        symbols,
        price_map,
        start_date,
        end_date,
        3,
        event_scores,
        200_000_000,
        0.10,
        0.04,
        0.035,
    )
    setup_allow = {"EVENT_PLUS_VOLATILITY", "VOLUME_BREAKOUT", "HIGH_VOLATILITY"}
    results: list[dict[str, Any]] = []
    base_combos = itertools.product(
        parse_int_list(args.max_positions),
        parse_float_list(args.min_scores),
        parse_float_list(args.target_atr_mults),
        parse_float_list(args.target_range_mults),
        parse_float_list(args.stop_atr_mults),
        parse_float_list(args.trail_atr_mults),
    )
    regime_combos: list[tuple[str, int, float, float, float, int, float]] = []
    for mode in parse_str_list(args.regime_modes):
        if mode == "off":
            regime_combos.append(("off", 12, 0.4, 0.5, 0.08, 5, 0.35))
            continue
        if mode not in {"skip", "reduce"}:
            raise SystemExit("--regime-modes items must be off, skip, or reduce")
        for combo in itertools.product(
            parse_int_list(args.regime_lookback_trades),
            parse_float_list(args.regime_min_win_rates),
            parse_float_list(args.regime_max_hard_stop_rates),
            parse_float_list(args.regime_max_drawdowns),
            parse_int_list(args.regime_cooldown_days),
            parse_float_list(args.regime_risk_factors),
        ):
            regime_combos.append((mode, *combo))

    ma5_combos = []
    for mode in parse_str_list(args.ma5_modes):
        if mode not in {"ignore", "filter", "pullback"}:
            raise SystemExit("--ma5-modes items must be ignore, filter, or pullback")
        limits = [0.025] if mode != "pullback" else parse_float_list(args.ma5_pullback_limits)
        for limit in limits:
            for extension_limit in parse_float_list(args.ma5_extension_limits):
                ma5_combos.append((mode, limit, extension_limit))

    sector_combos = []
    for mode in parse_str_list(args.sector_modes):
        if mode not in {"ignore", "filter", "strong"}:
            raise SystemExit("--sector-modes items must be ignore, filter, or strong")
        if mode == "ignore":
            sector_combos.append(("ignore", -0.03, 0.35))
            continue
        for momentum, above_ratio in itertools.product(
            parse_float_list(args.min_sector_momentum_5ds),
            parse_float_list(args.min_sector_above_ma20_ratios),
        ):
            sector_combos.append((mode, momentum, above_ratio))

    for (max_positions, min_score, target_atr, target_range, stop_atr, trail_atr), regime, ma5, sector in itertools.product(base_combos, regime_combos, ma5_combos, sector_combos):
        regime_mode, regime_lookback, regime_min_win, regime_max_hard_stop, regime_max_dd, regime_cooldown, regime_risk_factor = regime
        regime_enabled = regime_mode != "off"
        ma5_mode, ma5_pullback_limit, ma5_extension_limit = ma5
        sector_mode, min_sector_momentum, min_sector_above = sector
        trades, curve, final_equity = simulate_portfolio(
            signal_rows,
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
            args.event_bonus,
            args.target_min,
            args.target_max,
            stop_atr,
            args.stop_min,
            args.stop_max,
            trail_atr,
            args.trail_min,
            args.trail_max,
            5.0,
            False,
            1,
            0.04,
            0.5,
            regime_enabled,
            "skip" if regime_mode == "off" else regime_mode,
            regime_lookback,
            args.regime_min_trades,
            regime_min_win,
            regime_max_hard_stop,
            regime_max_dd,
            regime_cooldown,
            regime_risk_factor,
            market_bars if regime_enabled else [],
            args.market_ma_days,
            args.market_lookback_days,
            args.market_min_return,
            ma5_mode,
            ma5_pullback_limit,
            ma5_extension_limit,
            sector_mode,
            min_sector_momentum,
            min_sector_above,
        )
        total_return = (final_equity / args.initial_cash - 1) * 100
        dd = max_drawdown(curve, args.initial_cash)
        objective = total_return - args.drawdown_penalty * dd
        wins = sum(1 for trade in trades if trade.return_pct > 0)
        results.append(
            {
                "objective": round(objective, 4),
                "return_pct": round(total_return, 4),
                "max_drawdown_pct": round(dd, 4),
                "trades": len(trades),
                "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0,
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
                "regime_risk_factor": regime_risk_factor,
                "ma5_mode": ma5_mode,
                "ma5_pullback_limit": ma5_pullback_limit,
                "ma5_extension_limit": ma5_extension_limit,
                "sector_mode": sector_mode,
                "min_sector_momentum_5d": min_sector_momentum,
                "min_sector_above_ma20_ratio": min_sector_above,
            }
        )
    results.sort(key=lambda item: (item["objective"], item["return_pct"]), reverse=True)
    out_path = Path(args.out or f"output/short_term_strategy_optimized_{start_date:%Y%m%d}_{end_date:%Y%m%d}.csv")
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
