#!/usr/bin/env python3
"""Screen technology stocks for short-term 10% volatility opportunity.

This is a screening tool, not a return guarantee. It finds names whose recent
price action, liquidity, trend, and event score make a 1-5 day 10% move
plausible enough to watch.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import textwrap
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dependency_bootstrap import ensure_project_dependencies

ensure_project_dependencies()

import requests

from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from tech_event_backtest import PriceBar, fetch_yahoo_history
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date


@dataclass
class Candidate:
    ticker: str
    name: str
    market: str
    theme: str
    score: float
    action: str
    setup_type: str
    close: float
    change_pct: float
    traded_value: float
    traded_value_ratio: float
    atr_pct: float
    max_3d_range_pct: float
    max_5d_range_pct: float
    distance_to_ma5_pct: float
    distance_to_20d_high_pct: float
    target_pct: float
    hard_stop_pct: float
    upside_grade: str
    entry_trigger: str
    extension_risk: str
    trend_flags: list[str] = field(default_factory=list)
    event_score: int = 0
    event_title: str = ""
    event_url: str = ""
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def average_true_range_pct(bars: list[PriceBar], window: int = 14) -> float:
    if len(bars) < window + 1:
        return 0.0
    ranges = []
    for index in range(len(bars) - window, len(bars)):
        bar = bars[index]
        previous = bars[index - 1]
        true_range = max(
            bar.high - bar.low,
            abs(bar.high - previous.close),
            abs(bar.low - previous.close),
        )
        if previous.close > 0:
            ranges.append(true_range / previous.close)
    return sum(ranges) / len(ranges) * 100 if ranges else 0.0


def max_window_range_pct(bars: list[PriceBar], window: int, lookback: int = 20) -> float:
    if len(bars) < window:
        return 0.0
    start = max(0, len(bars) - lookback)
    best = 0.0
    for index in range(start, len(bars) - window + 1):
        subset = bars[index : index + window]
        low = min(bar.low for bar in subset)
        high = max(bar.high for bar in subset)
        if low > 0:
            best = max(best, (high / low - 1) * 100)
    return best


def load_event_scores(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        events = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    best: dict[str, dict[str, Any]] = {}
    for event in events:
        ticker = event.get("ticker")
        score = int(event.get("raw_score") or 0)
        if not ticker:
            continue
        if ticker not in best or score > int(best[ticker].get("raw_score") or 0):
            best[ticker] = event
    return best


def score_candidate(
    symbol: Any,
    bars: list[PriceBar],
    event: dict[str, Any] | None,
    min_traded_value: float,
) -> Candidate | None:
    if len(bars) < 25:
        return None
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    latest = bars[-1]
    previous = bars[-2]
    ma5 = moving_average(closes, 5) or latest.close
    ma10 = moving_average(closes, 10) or latest.close
    ma20 = moving_average(closes, 20) or latest.close
    traded_value = latest.close * latest.volume
    values_20 = [bar.close * bar.volume for bar in bars[-21:-1] if bar.volume > 0]
    avg_value = sum(values_20) / len(values_20) if values_20 else traded_value
    value_ratio = traded_value / avg_value if avg_value else 0.0
    atr_pct = average_true_range_pct(bars)
    max_3d = max_window_range_pct(bars, 3)
    max_5d = max_window_range_pct(bars, 5)
    distance_ma5 = (latest.close / ma5 - 1) * 100 if ma5 else 0.0
    high_20 = max(bar.high for bar in bars[-20:])
    distance_high = (latest.close / high_20 - 1) * 100 if high_20 else 0.0
    change_pct = (latest.close / previous.close - 1) * 100 if previous.close else 0.0
    target_pct = min(18.0, max(6.0, atr_pct * 1.2 + max_5d * 0.35))
    hard_stop_pct = min(6.5, max(2.5, atr_pct * 0.7))

    event_score = int(event.get("raw_score") or 0) if event else 0
    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    trend_flags: list[str] = []

    if traded_value >= min_traded_value:
        score += 18
        reasons.append("liquid_traded_value")
    elif traded_value >= min_traded_value * 0.4:
        score += 8
        reasons.append("acceptable_traded_value")
    else:
        risks.append("low_traded_value")

    if value_ratio >= 1.8:
        score += 18
        reasons.append("traded_value_expansion")
    elif value_ratio >= 1.2:
        score += 10
        reasons.append("traded_value_active")

    if atr_pct >= 6:
        score += 18
        reasons.append("high_daily_volatility")
    elif atr_pct >= 3.5:
        score += 10
        reasons.append("medium_daily_volatility")

    if max_5d >= 12:
        score += 18
        reasons.append("recent_10pct_plus_range")
    elif max_3d >= 8:
        score += 12
        reasons.append("recent_fast_range")

    if latest.close > ma5:
        score += 8
        trend_flags.append("close>MA5")
        if distance_ma5 >= 9:
            score -= 16
            risks.append("extended_far_above_MA5")
        elif distance_ma5 >= 6:
            score -= 8
            risks.append("extended_above_MA5")
    else:
        trend_flags.append("close<MA5")
        if distance_ma5 < -4:
            score -= 14
            risks.append("deep_below_MA5")
        elif distance_ma5 < -1.5:
            score -= 8
            risks.append("below_MA5")
        else:
            score -= 3
            risks.append("shallow_below_MA5")
            if latest.close > ma20 and value_ratio >= 1.2 and max_5d >= 12:
                reasons.append("MA5_pullback_watch")
    if latest.close > ma20:
        score += 10
        trend_flags.append("close>MA20")
    else:
        risks.append("below_MA20")

    if -8 <= distance_high <= 0:
        score += 10
        reasons.append("near_20d_high")
    elif distance_high < -15:
        risks.append("far_from_20d_high")

    if event_score >= 80:
        score += 18
        reasons.append("strong_event")
    elif event_score >= 60:
        score += 10
        reasons.append("event_watch")

    if change_pct < -5:
        risks.append("falling_today")
        score -= 8
    if traded_value < min_traded_value * 0.25:
        score -= 12
    if atr_pct < 2.5 and max_5d < 8:
        score -= 10
        risks.append("insufficient_10pct_elasticity")

    reward_risk = target_pct / hard_stop_pct if hard_stop_pct else 0.0
    upside_points = 0.0
    if score >= 85:
        upside_points += 2
    elif score >= 75:
        upside_points += 1
    if value_ratio >= 1.8:
        upside_points += 1
    if latest.close > ma5:
        upside_points += 1
    elif distance_ma5 >= -2.5 and latest.close > ma20 and max_5d >= 12:
        upside_points += 0.5
    if reward_risk >= 3:
        upside_points += 1
    if "falling_today" in risks:
        upside_points -= 1
    if "far_from_20d_high" in risks:
        upside_points -= 1
    if "below_MA20" in risks:
        upside_points -= 2
    if "extended_far_above_MA5" in risks:
        upside_points -= 2
    elif "extended_above_MA5" in risks:
        upside_points -= 1
    if upside_points >= 4:
        upside_grade = "HIGH"
    elif upside_points >= 2.5:
        upside_grade = "MEDIUM"
    else:
        upside_grade = "LOW"

    if distance_ma5 >= 9:
        extension_risk = "EXTREME"
    elif distance_ma5 >= 6:
        extension_risk = "HIGH"
    elif distance_ma5 >= 3:
        extension_risk = "MEDIUM"
    else:
        extension_risk = "LOW"

    if latest.close > ma5 and extension_risk in {"HIGH", "EXTREME"}:
        entry_trigger = "wait_pullback_or_consolidation"
    elif latest.close > ma5:
        entry_trigger = "hold_MA5_or_breakout"
    elif distance_ma5 >= -2.5 and latest.close > ma20:
        entry_trigger = "reclaim_MA5_only"
    else:
        entry_trigger = "no_buy_below_MA5"

    if score >= 78 and "below_MA20" not in risks and latest.close > ma5 and extension_risk not in {"HIGH", "EXTREME"}:
        action = "READY_WATCH"
    elif score >= 75 and "below_MA20" not in risks and latest.close > ma5 and extension_risk in {"HIGH", "EXTREME"}:
        action = "WAIT_PULLBACK"
    elif score >= 75 and "below_MA20" not in risks and distance_ma5 >= -2.5:
        action = "WATCH_FOR_MA5_RECLAIM"
    elif score >= 62:
        action = "WATCH_FOR_TRIGGER"
    elif score >= 50:
        action = "LOW_PRIORITY"
    else:
        action = "IGNORE"

    if event_score >= 60:
        setup_type = "EVENT_PLUS_VOLATILITY"
    elif value_ratio >= 1.5 and max_5d >= 10:
        setup_type = "VOLUME_BREAKOUT"
    elif max_5d >= 12 and atr_pct >= 3.5:
        setup_type = "HIGH_VOLATILITY"
    else:
        setup_type = "BACKGROUND_WATCH"

    theme = symbol.notes or ""
    return Candidate(
        ticker=symbol.ticker,
        name=symbol.name,
        market=symbol.market,
        theme=theme,
        score=round(score, 2),
        action=action,
        setup_type=setup_type,
        close=round(latest.close, 4),
        change_pct=round(change_pct, 2),
        traded_value=round(traded_value, 2),
        traded_value_ratio=round(value_ratio, 2),
        atr_pct=round(atr_pct, 2),
        max_3d_range_pct=round(max_3d, 2),
        max_5d_range_pct=round(max_5d, 2),
        distance_to_ma5_pct=round(distance_ma5, 2),
        distance_to_20d_high_pct=round(distance_high, 2),
        target_pct=round(target_pct, 2),
        hard_stop_pct=round(hard_stop_pct, 2),
        upside_grade=upside_grade,
        entry_trigger=entry_trigger,
        extension_risk=extension_risk,
        trend_flags=trend_flags,
        event_score=event_score,
        event_title=str(event.get("title", "")) if event else "",
        event_url=str(event.get("url", "")) if event else "",
        reasons=reasons,
        risks=risks,
    )


def markdown_report(candidates: list[Candidate], today: dt.date, min_score: float, top: int) -> str:
    lines = [
        f"# Short-Term 10% Elasticity Screen - {today.isoformat()}",
        "",
        "This screen looks for stocks with enough short-term volatility and liquidity to plausibly produce a 10% move. It is not a guarantee and not a buy recommendation.",
        "",
        "| Rank | Action | Setup | Ticker | Score | Upside | Extension | Trigger | Close | Chg | MA5 Dist | ValueX | ATR | 5D Range | Target | Stop | Event | Risks |",
        "|---:|---|---|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    shown = [item for item in candidates if item.score >= min_score and item.action != "IGNORE"][:top]
    for rank, item in enumerate(shown, start=1):
        lines.append(
            "| {rank} | {action} | {setup} | {ticker} | {score:.1f} | {upside} | {extension} | {trigger} | {close:.2f} | {chg:.2f}% | {ma5:.2f}% | {valuex:.2f} | {atr:.2f}% | {range5:.2f}% | {target:.2f}% | {stop:.2f}% | {event} | {risks} |".format(
                rank=rank,
                action=item.action,
                setup=item.setup_type,
                ticker=item.ticker,
                score=item.score,
                upside=item.upside_grade,
                extension=item.extension_risk,
                trigger=item.entry_trigger,
                close=item.close,
                chg=item.change_pct,
                ma5=item.distance_to_ma5_pct,
                valuex=item.traded_value_ratio,
                atr=item.atr_pct,
                range5=item.max_5d_range_pct,
                target=item.target_pct,
                stop=item.hard_stop_pct,
                event=item.event_score,
                risks=", ".join(item.risks[:4]).replace("|", "\\|"),
            )
        )
    if not shown:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - | - | No candidates | - |")

    lines.extend(
        [
            "",
            "## Trigger Rules",
            "",
            "- READY_WATCH: wait for intraday reclaim of VWAP/previous close or breakout above the prior day high.",
            "- WATCH_FOR_TRIGGER: only act if traded value expands again and price holds above MA5/MA20.",
            "- Stop loss: usually 3%-5%, or below the signal day's low.",
            "- Take profit: reduce above 8%-10%; do not assume continuation after a 10% move.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_csv(path: Path, candidates: list[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(asdict(candidates[0]).keys()) if candidates else [field.name for field in Candidate.__dataclass_fields__.values()]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in candidates:
            row = asdict(item)
            row["trend_flags"] = ",".join(item.trend_flags)
            row["reasons"] = ",".join(item.reasons)
            row["risks"] = ",".join(item.risks)
            writer.writerow(row)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Screen tech stocks for short-term 10% elasticity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python short_term_opportunity.py --today 2026-07-02
              python short_term_opportunity.py --market CN --min-score 55
            """
        ),
    )
    parser.add_argument("--watchlist", default="config/watchlist.example.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES), help="buyable ticker prefixes; use 'all' to disable")
    parser.add_argument("--events", default="output/tech_event_radar_20260702.json")
    parser.add_argument("--today", default="")
    parser.add_argument("--market", choices=("ALL", "US", "CN"), default="ALL")
    parser.add_argument("--min-score", type=float, default=50.0)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-traded-value", type=float, default=200_000_000)
    parser.add_argument("--out", default="")
    parser.add_argument("--csv-out", default="")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    today = parse_date(args.today) if args.today else dt.date.today()
    if not today:
        raise SystemExit("--today must be YYYY-MM-DD")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    if args.market == "US":
        symbols = [item for item in symbols if item.market == "US"]
    elif args.market == "CN":
        symbols = [item for item in symbols if item.market in {"CN", "SH", "SZ", "BJ"}]

    event_scores = load_event_scores(Path(args.events))
    candidates: list[Candidate] = []
    start = today - dt.timedelta(days=90)
    for symbol in symbols:
        yahoo_symbol = symbol.yahoo_symbol or symbol.ticker
        try:
            bars = fetch_yahoo_history(session, yahoo_symbol, start, today)
        except Exception:
            time.sleep(0.05)
            continue
        bars = [bar for bar in bars if bar.date <= today]
        candidate = score_candidate(symbol, bars, event_scores.get(symbol.ticker), args.min_traded_value)
        if candidate:
            candidates.append(candidate)
        time.sleep(0.05)

    candidates.sort(key=lambda item: (item.score, item.event_score, item.traded_value), reverse=True)
    default_name = f"short_term_opportunity_{today:%Y%m%d}"
    out_path = Path(args.out or f"output/{default_name}.md")
    csv_path = Path(args.csv_out or f"output/{default_name}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown_report(candidates, today, args.min_score, args.top), encoding="utf-8")
    write_csv(csv_path, candidates)

    shown = [item for item in candidates if item.score >= args.min_score and item.action != "IGNORE"][: args.top]
    print(f"symbols={len(symbols)} candidates={len(shown)}")
    print(f"markdown={out_path}")
    print(f"csv={csv_path}")
    for item in shown[:15]:
        print(
            f"{item.action} {item.setup_type} {item.ticker} score={item.score} value={item.traded_value:.0f} "
            f"atr={item.atr_pct:.2f}% range5={item.max_5d_range_pct:.2f}% event={item.event_score}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
