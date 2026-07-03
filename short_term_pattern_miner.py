#!/usr/bin/env python3
"""Mine patterns from short-term technology stock winners.

For each date and symbol, this script records features visible at the close of
that date, then checks whether the next N trading days reached +10%. It also
simulates practical intraday-style exits using daily OHLC: take profit,
trailing stop from the best high, hard stop, or time exit.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import textwrap
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from tech_event_backtest import PriceBar, fetch_yahoo_history
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date


DEFAULT_SIGNAL_WEIGHTS: dict[str, float] = {
    "liquidity": 14,
    "value": 18,
    "volatility": 22,
    "range": 24,
    "momentum": 10,
    "trend": 12,
    "event": 0,
    "overheat_penalty": 16,
}


@dataclass
class PatternRow:
    ticker: str
    name: str
    market: str
    date: str
    close: float
    score: float
    setup_type: str
    traded_value: float
    traded_value_ratio: float
    atr_pct: float
    max_5d_range_pct: float
    change_1d_pct: float
    momentum_3d_pct: float
    momentum_10d_pct: float
    value_ratio_3d: float
    distance_to_ma5_pct: float
    distance_to_20d_high_pct: float
    close_position_20d_pct: float
    above_ma5: bool
    above_ma20: bool
    future_max_return_pct: float
    hit_10pct: bool
    simulated_return_pct: float
    exit_reason: str
    sector_group: str = ""
    sector_momentum_5d_pct: float = 0.0
    sector_above_ma20_ratio: float = 0.0


def moving_average(values: list[float], end_index: int, window: int) -> float | None:
    if end_index + 1 < window:
        return None
    return sum(values[end_index - window + 1 : end_index + 1]) / window


def atr_pct_at(bars: list[PriceBar], end_index: int, window: int = 14) -> float:
    if end_index < window:
        return 0.0
    ranges = []
    for index in range(end_index - window + 1, end_index + 1):
        bar = bars[index]
        previous = bars[index - 1]
        true_range = max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
        if previous.close > 0:
            ranges.append(true_range / previous.close)
    return sum(ranges) / len(ranges) * 100 if ranges else 0.0


def max_range_pct_at(bars: list[PriceBar], end_index: int, window: int = 5, lookback: int = 20) -> float:
    start = max(0, end_index - lookback + 1)
    best = 0.0
    for index in range(start, end_index - window + 2):
        subset = bars[index : index + window]
        low = min(bar.low for bar in subset)
        high = max(bar.high for bar in subset)
        if low > 0:
            best = max(best, (high / low - 1) * 100)
    return best


def component_value(value: float, high: float, mid: float, mid_score: float = 0.55) -> float:
    if value >= high:
        return 1.0
    if value >= mid:
        return mid_score
    return 0.0


def setup_type(event_score: int, value_ratio: float, max_5d: float, atr_pct: float, event_weight: float | None = None) -> str:
    if event_weight is None:
        event_weight = DEFAULT_SIGNAL_WEIGHTS["event"]
    if event_weight > 0 and event_score >= 60:
        return "EVENT_PLUS_VOLATILITY"
    if value_ratio >= 1.5 and max_5d >= 10:
        return "VOLUME_BREAKOUT"
    if max_5d >= 12 and atr_pct >= 3.5:
        return "HIGH_VOLATILITY"
    return "BACKGROUND_WATCH"


def event_score_by_symbol(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        events = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    scores: dict[str, int] = {}
    for event in events:
        ticker = str(event.get("ticker", ""))
        scores[ticker] = max(scores.get(ticker, 0), int(event.get("raw_score") or 0))
    return scores


def simulate_exit(
    entry_price: float,
    future_bars: list[PriceBar],
    take_profit: float,
    hard_stop: float,
    trailing_stop: float,
) -> tuple[float, str]:
    best_high = entry_price
    for bar in future_bars:
        if bar.low <= entry_price * (1 - hard_stop):
            return ((entry_price * (1 - hard_stop)) / entry_price - 1) * 100, "hard_stop"
        if bar.high > best_high:
            best_high = bar.high
        if bar.high >= entry_price * (1 + take_profit):
            return (take_profit * 100), "take_profit"
        if best_high >= entry_price * 1.04 and bar.low <= best_high * (1 - trailing_stop):
            return ((best_high * (1 - trailing_stop)) / entry_price - 1) * 100, "trailing_stop"
    if not future_bars:
        return 0.0, "no_future_bars"
    return ((future_bars[-1].close / entry_price) - 1) * 100, "time_exit"


def feature_score(
    traded_value: float,
    value_ratio: float,
    atr_pct: float,
    max_5d: float,
    change_1d: float,
    momentum_3d: float,
    momentum_10d: float,
    value_ratio_3d: float,
    distance_high: float,
    close_position_20d: float,
    distance_ma5: float,
    above_ma5: bool,
    above_ma20: bool,
    event_score: int,
    min_traded_value: float,
    weights: dict[str, float] | None = None,
) -> float:
    weights = weights or DEFAULT_SIGNAL_WEIGHTS
    score = 0.0
    score += weights["liquidity"] * component_value(traded_value, min_traded_value, min_traded_value * 0.4, 0.45)
    score += weights["value"] * component_value(value_ratio, 1.8, 1.2)
    score += weights["volatility"] * component_value(atr_pct, 6.0, 3.5)
    score += weights["range"] * component_value(max_5d, 12.0, 8.0, 0.65)

    momentum_score = 0.0
    if 2 <= momentum_3d <= 14:
        momentum_score += 0.5
    elif momentum_3d > 20:
        momentum_score -= 0.5
    if 4 <= momentum_10d <= 28:
        momentum_score += 0.5
    elif momentum_10d > 38:
        momentum_score -= 0.6
    if value_ratio_3d >= 1.25 and value_ratio >= 1.0:
        momentum_score += 0.25
    score += weights["momentum"] * momentum_score

    trend_score = 0.0
    if above_ma5:
        trend_score += 0.28
    if above_ma20:
        trend_score += 0.36
    if -8 <= distance_high <= 0:
        trend_score += 0.24
    if 55 <= close_position_20d <= 85:
        trend_score += 0.12
    score += weights["trend"] * trend_score

    event_component = 1.0 if event_score >= 80 else 0.55 if event_score >= 60 else 0.0
    score += weights["event"] * event_component

    penalty = 0.0
    if close_position_20d >= 82 and distance_ma5 > 6:
        penalty += 0.6
    if change_1d <= -6:
        penalty += 0.5
    if momentum_10d > 38:
        penalty += 0.4
    score -= weights["overheat_penalty"] * penalty
    return round(score, 2)


def build_rows_for_symbol(
    symbol: Any,
    bars: list[PriceBar],
    start_date: dt.date,
    end_date: dt.date,
    horizon: int,
    event_scores: dict[str, int],
    min_traded_value: float,
    take_profit: float,
    hard_stop: float,
    trailing_stop: float,
) -> list[PatternRow]:
    rows: list[PatternRow] = []
    closes = [bar.close for bar in bars]
    event_score = event_scores.get(symbol.ticker, 0)
    for index, bar in enumerate(bars):
        if bar.date < start_date or bar.date > end_date:
            continue
        if index < 25 or index + 1 >= len(bars):
            continue
        ma5 = moving_average(closes, index, 5) or bar.close
        ma20 = moving_average(closes, index, 20) or bar.close
        traded_value = bar.close * bar.volume
        previous_values = [item.close * item.volume for item in bars[max(0, index - 20) : index] if item.volume > 0]
        avg_value = sum(previous_values) / len(previous_values) if previous_values else traded_value
        value_ratio = traded_value / avg_value if avg_value else 0.0
        recent_values = [item.close * item.volume for item in bars[max(0, index - 2) : index + 1] if item.volume > 0]
        recent_avg_value = sum(recent_values) / len(recent_values) if recent_values else traded_value
        value_ratio_3d = recent_avg_value / avg_value if avg_value else 0.0
        atr = atr_pct_at(bars, index)
        max_5d = max_range_pct_at(bars, index)
        distance_ma5 = (bar.close / ma5 - 1) * 100 if ma5 else 0.0
        high_20 = max(item.high for item in bars[index - 19 : index + 1])
        low_20 = min(item.low for item in bars[index - 19 : index + 1])
        distance_high = (bar.close / high_20 - 1) * 100 if high_20 else 0.0
        close_position_20d = (bar.close - low_20) / (high_20 - low_20) * 100 if high_20 > low_20 else 50.0
        previous_close = bars[index - 1].close if index >= 1 else bar.close
        change_1d = (bar.close / previous_close - 1) * 100 if previous_close else 0.0
        close_3d_ago = bars[index - 3].close if index >= 3 else bar.close
        close_10d_ago = bars[index - 10].close if index >= 10 else bar.close
        momentum_3d = (bar.close / close_3d_ago - 1) * 100 if close_3d_ago else 0.0
        momentum_10d = (bar.close / close_10d_ago - 1) * 100 if close_10d_ago else 0.0
        future = bars[index + 1 : index + 1 + horizon]
        future_high = max((item.high for item in future), default=bar.close)
        future_max_return = (future_high / bar.close - 1) * 100 if bar.close else 0.0
        sim_return, exit_reason = simulate_exit(bar.close, future, take_profit, hard_stop, trailing_stop)
        above_ma5 = bar.close > ma5
        above_ma20 = bar.close > ma20
        score = feature_score(
            traded_value,
            value_ratio,
            atr,
            max_5d,
            change_1d,
            momentum_3d,
            momentum_10d,
            value_ratio_3d,
            distance_high,
            close_position_20d,
            distance_ma5,
            above_ma5,
            above_ma20,
            event_score,
            min_traded_value,
        )
        rows.append(
            PatternRow(
                ticker=symbol.ticker,
                name=symbol.name,
                market=symbol.market,
                date=bar.date.isoformat(),
                close=round(bar.close, 4),
                score=round(score, 2),
                setup_type=setup_type(event_score, value_ratio, max_5d, atr),
                traded_value=round(traded_value, 2),
                traded_value_ratio=round(value_ratio, 2),
                atr_pct=round(atr, 2),
                max_5d_range_pct=round(max_5d, 2),
                change_1d_pct=round(change_1d, 2),
                momentum_3d_pct=round(momentum_3d, 2),
                momentum_10d_pct=round(momentum_10d, 2),
                value_ratio_3d=round(value_ratio_3d, 2),
                distance_to_ma5_pct=round(distance_ma5, 2),
                distance_to_20d_high_pct=round(distance_high, 2),
                close_position_20d_pct=round(close_position_20d, 2),
                above_ma5=above_ma5,
                above_ma20=above_ma20,
                future_max_return_pct=round(future_max_return, 2),
                hit_10pct=future_max_return >= 10,
                simulated_return_pct=round(sim_return, 2),
                exit_reason=exit_reason,
            )
        )
    return rows


def summarize(rows: list[PatternRow]) -> dict[str, Any]:
    winners = [row for row in rows if row.hit_10pct]
    non_winners = [row for row in rows if not row.hit_10pct]

    def avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    by_setup = {}
    for setup in sorted({row.setup_type for row in rows}):
        group = [row for row in rows if row.setup_type == setup]
        by_setup[setup] = {
            "count": len(group),
            "hit_rate_pct": round(sum(row.hit_10pct for row in group) / len(group) * 100, 2) if group else 0,
            "avg_sim_return_pct": round(avg([row.simulated_return_pct for row in group]), 2),
        }
    return {
        "rows": len(rows),
        "winners": len(winners),
        "hit_rate_pct": round(len(winners) / len(rows) * 100, 2) if rows else 0,
        "winner_avg_score": round(avg([row.score for row in winners]), 2),
        "non_winner_avg_score": round(avg([row.score for row in non_winners]), 2),
        "winner_avg_value_ratio": round(avg([row.traded_value_ratio for row in winners]), 2),
        "non_winner_avg_value_ratio": round(avg([row.traded_value_ratio for row in non_winners]), 2),
        "winner_avg_atr_pct": round(avg([row.atr_pct for row in winners]), 2),
        "non_winner_avg_atr_pct": round(avg([row.atr_pct for row in non_winners]), 2),
        "winner_avg_5d_range_pct": round(avg([row.max_5d_range_pct for row in winners]), 2),
        "non_winner_avg_5d_range_pct": round(avg([row.max_5d_range_pct for row in non_winners]), 2),
        "setup_summary": by_setup,
        "avg_simulated_return_pct": round(avg([row.simulated_return_pct for row in rows]), 2),
    }


def write_outputs(rows: list[PatternRow], summary: dict[str, Any], out_path: Path, csv_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()) if rows else [])
        if rows:
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row))

    winners = sorted([row for row in rows if row.hit_10pct], key=lambda item: item.future_max_return_pct, reverse=True)[:20]
    lines = [
        "# Short-Term Winner Pattern Mining",
        "",
        "This report labels each historical close by whether the next window reached +10%, then compares common features. It uses daily OHLC, so intraday exits are approximations.",
        "",
        f"- Samples: `{summary['rows']}`",
        f"- +10% hit count: `{summary['winners']}`",
        f"- +10% hit rate: `{summary['hit_rate_pct']}%`",
        f"- Avg simulated return with active exits: `{summary['avg_simulated_return_pct']}%`",
        "",
        "## Winner Vs Non-Winner",
        "",
        f"- Avg score: `{summary['winner_avg_score']}` vs `{summary['non_winner_avg_score']}`",
        f"- Avg traded value ratio: `{summary['winner_avg_value_ratio']}` vs `{summary['non_winner_avg_value_ratio']}`",
        f"- Avg ATR: `{summary['winner_avg_atr_pct']}%` vs `{summary['non_winner_avg_atr_pct']}%`",
        f"- Avg 5D range: `{summary['winner_avg_5d_range_pct']}%` vs `{summary['non_winner_avg_5d_range_pct']}%`",
        "",
        "## Setup Summary",
        "",
        "| Setup | Count | +10% Hit Rate | Avg Sim Return |",
        "|---|---:|---:|---:|",
    ]
    for setup, stats in summary["setup_summary"].items():
        lines.append(
            f"| {setup} | {stats['count']} | {stats['hit_rate_pct']}% | {stats['avg_sim_return_pct']}% |"
        )
    lines.extend(["", "## Top +10% Winners", "", "| Ticker | Date | Setup | Score | Future Max | Sim Return | Exit | Key Features |", "|---|---|---|---:|---:|---:|---|---|"])
    for row in winners:
        lines.append(
            f"| {row.ticker} | {row.date} | {row.setup_type} | {row.score:.1f} | {row.future_max_return_pct:.2f}% | {row.simulated_return_pct:.2f}% | {row.exit_reason} | valueX={row.traded_value_ratio}, ATR={row.atr_pct}%, 5D={row.max_5d_range_pct}% |"
        )
    lines.extend(
        [
            "",
            "## Practical Rule",
            "",
            "- Prefer candidates with high traded value, recent 5D range above 12%, ATR above 3.5%, and price above MA20.",
            "- Enter only after intraday reclaim or breakout; do not buy just because the stock is volatile.",
            "- Use active exits: take profit near 8%-10%, hard stop near 3%-5%, and trailing stop after a 4%+ favorable move.",
        ]
    )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mine short-term winner patterns and active exit behavior.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Example:
              python short_term_pattern_miner.py --start-date 2026-06-02 --end-date 2026-07-02
            """
        ),
    )
    parser.add_argument("--watchlist", default="config/watchlist.example.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES), help="buyable ticker prefixes; use 'all' to disable")
    parser.add_argument("--events", default="output/tech_event_radar_20260702.json")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--horizon", type=int, default=3)
    parser.add_argument("--take-profit", type=float, default=0.10)
    parser.add_argument("--hard-stop", type=float, default=0.04)
    parser.add_argument("--trailing-stop", type=float, default=0.035)
    parser.add_argument("--min-traded-value", type=float, default=200_000_000)
    parser.add_argument("--out", default="")
    parser.add_argument("--csv-out", default="")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("start and end dates must be YYYY-MM-DD")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    event_scores = event_score_by_symbol(Path(args.events))
    all_rows: list[PatternRow] = []
    fetch_start = start_date - dt.timedelta(days=120)
    fetch_end = end_date + dt.timedelta(days=args.horizon * 3 + 10)
    for symbol in symbols:
        try:
            bars = fetch_yahoo_history(session, symbol.yahoo_symbol or symbol.ticker, fetch_start, fetch_end)
        except Exception:
            time.sleep(0.05)
            continue
        all_rows.extend(
            build_rows_for_symbol(
                symbol,
                bars,
                start_date,
                end_date,
                args.horizon,
                event_scores,
                args.min_traded_value,
                args.take_profit,
                args.hard_stop,
                args.trailing_stop,
            )
        )
        time.sleep(0.05)

    summary = summarize(all_rows)
    default_name = f"short_term_patterns_{start_date:%Y%m%d}_{end_date:%Y%m%d}"
    out_path = Path(args.out or f"output/{default_name}.md")
    csv_path = Path(args.csv_out or f"output/{default_name}.csv")
    write_outputs(all_rows, summary, out_path, csv_path)
    print(f"rows={summary['rows']} winners={summary['winners']} hit_rate={summary['hit_rate_pct']}%")
    print(f"markdown={out_path}")
    print(f"csv={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
