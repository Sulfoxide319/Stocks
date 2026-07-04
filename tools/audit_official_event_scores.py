#!/usr/bin/env python3
"""Audit official event scores before using them in the short-term monitor."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from short_term_pattern_miner import (
    OFFICIAL_NEGATIVE_KEYWORDS,
    OFFICIAL_POSITIVE_KEYWORDS,
    _keyword_hit,
    _official_source,
    official_event_score_adjustment,
    official_event_score_by_symbol,
)
from tech_event_radar import load_watchlist


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def event_signed_score(event: dict[str, Any]) -> int:
    source = str(event.get("source", "")).strip()
    url = str(event.get("url", "")).strip()
    if not _official_source(source, url):
        return 0
    text = f"{event.get('title', '')} {event.get('snippet', '')}"
    risk_flags = [str(item) for item in (event.get("risk_flags") or [])]
    raw = max(0, min(100, int(event.get("raw_score") or 0)))
    has_negative = bool(risk_flags) or _keyword_hit(text, OFFICIAL_NEGATIVE_KEYWORDS)
    has_positive = _keyword_hit(text, OFFICIAL_POSITIVE_KEYWORDS)
    if has_negative:
        return -max(raw, 55)
    if has_positive:
        return max(raw, 60)
    return 0


def event_reason(event: dict[str, Any], signed_score: int) -> str:
    source = str(event.get("source", "")).strip()
    url = str(event.get("url", "")).strip()
    if not _official_source(source, url):
        return "ignored_non_official_source"
    text = f"{event.get('title', '')} {event.get('snippet', '')}"
    if signed_score < 0:
        return "negative_risk_or_keyword"
    if signed_score > 0:
        return "positive_keyword"
    if _keyword_hit(text, OFFICIAL_POSITIVE_KEYWORDS):
        return "positive_below_threshold"
    return "ignored_no_signed_keyword"


def ticker_prefix_ok(ticker: str, allowed_prefixes: str) -> bool:
    prefixes = tuple(item.strip() for item in allowed_prefixes.split(",") if item.strip())
    return bool(prefixes) and ticker.startswith(prefixes)


def audit_events(events: list[dict[str, Any]], watchlist_tickers: set[str], allowed_prefixes: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        ticker = str(event.get("ticker", "")).strip()
        source = str(event.get("source", "")).strip()
        url = str(event.get("url", "")).strip()
        signed_score = event_signed_score(event)
        adjustment = official_event_score_adjustment(signed_score)
        rows.append(
            {
                "ticker": ticker,
                "market": str(event.get("market", "")).strip(),
                "name": str(event.get("name", "")).strip(),
                "source": source,
                "raw_score": int(event.get("raw_score") or 0),
                "signed_score": signed_score,
                "score_adjustment": adjustment,
                "reason": event_reason(event, signed_score),
                "official_source": bool(_official_source(source, url)),
                "watchlist_overlap": ticker in watchlist_tickers,
                "allowed_prefix": ticker_prefix_ok(ticker, allowed_prefixes),
                "title": re.sub(r"\s+", " ", str(event.get("title", "")).strip()),
                "url": url,
            }
        )
    return rows


def aggregate_symbol_scores(rows: list[dict[str, Any]]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for row in rows:
        ticker = row["ticker"]
        signed = int(row["signed_score"])
        if not ticker or signed == 0:
            continue
        current = scores.get(ticker, 0)
        if signed < 0:
            if current >= 0 or abs(signed) > abs(current):
                scores[ticker] = signed
        elif current >= 0:
            scores[ticker] = max(current, signed)
    return scores


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ticker",
        "market",
        "name",
        "source",
        "raw_score",
        "signed_score",
        "score_adjustment",
        "reason",
        "official_source",
        "watchlist_overlap",
        "allowed_prefix",
        "title",
        "url",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows: list[dict[str, Any]], symbol_scores: dict[str, int], path: Path, events_path: Path) -> None:
    total = len(rows)
    official = sum(1 for row in rows if row["official_source"])
    signed = sum(1 for row in rows if int(row["signed_score"]) != 0)
    positive = sum(1 for row in rows if int(row["signed_score"]) > 0)
    negative = sum(1 for row in rows if int(row["signed_score"]) < 0)
    overlap = sum(1 for row in rows if row["watchlist_overlap"])
    signed_overlap_symbols = sorted(ticker for ticker in symbol_scores if any(row["ticker"] == ticker and row["watchlist_overlap"] for row in rows))
    source_counts = Counter(str(row["source"] or "-") for row in rows)
    reason_counts = Counter(str(row["reason"] or "-") for row in rows)
    lines = [
        "# Official Event Score Audit",
        "",
        f"- Event file: `{events_path}`",
        f"- Total events: `{total}`",
        f"- Official-source events: `{official}`",
        f"- Signed official events: `{signed}` positive=`{positive}` negative=`{negative}`",
        f"- Watchlist-overlap events: `{overlap}`",
        f"- Signed watchlist-overlap symbols: `{len(signed_overlap_symbols)}`",
        "",
        "## Source Counts",
        "",
        "| Source | Count |",
        "|---|---:|",
    ]
    for source, count in sorted(source_counts.items()):
        lines.append(f"| {source} | {count} |")
    lines.extend(["", "## Reason Counts", "", "| Reason | Count |", "|---|---:|"])
    for reason, count in sorted(reason_counts.items()):
        lines.append(f"| {reason} | {count} |")
    lines.extend(
        [
            "",
            "## Watchlist Impact",
            "",
            "Official events are score adjustments only. They do not bypass mainboard prefix, score, VWAP, T+1, lot, tick, or limit filters.",
        ]
    )
    if signed_overlap_symbols:
        lines.append(f"- Signed overlap symbols: `{', '.join(signed_overlap_symbols)}`")
    else:
        lines.append("- Signed overlap symbols: `0`; this event file has no direct scoring impact on the configured watchlist.")
    lines.extend(
        [
            "",
            "## Signed Events",
            "",
            "| Ticker | Source | Signed | Adjustment | Overlap | Reason | Title |",
            "|---|---|---:|---:|---|---|---|",
        ]
    )
    for row in rows:
        if int(row["signed_score"]) == 0:
            continue
        title = str(row["title"]).replace("|", "/")
        lines.append(
            f"| {row['ticker']} | {row['source']} | {row['signed_score']} | {row['score_adjustment']} | {row['watchlist_overlap']} | {row['reason']} | {title} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit official event scores and watchlist overlap.")
    parser.add_argument("--events", default="output/tech_event_radar_20260703.json")
    parser.add_argument("--watchlist", default="config/watchlist.mainboard_liquid.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES))
    parser.add_argument("--out-dir", default="output/event_score_audit")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    events_path = Path(args.events)
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    watchlist_tickers = {symbol.ticker for symbol in symbols}
    events = load_events(events_path)
    rows = audit_events(events, watchlist_tickers, args.allowed_prefixes)
    symbol_scores = aggregate_symbol_scores(rows)
    production_scores = official_event_score_by_symbol(events_path)
    if symbol_scores != production_scores:
        raise SystemExit("audit mismatch: aggregate signed scores differ from official_event_score_by_symbol")
    out_dir = Path(args.out_dir)
    stem = events_path.stem
    csv_path = out_dir / f"{stem}_official_event_audit.csv"
    md_path = out_dir / f"{stem}_official_event_audit.md"
    write_csv(rows, csv_path)
    write_markdown(rows, symbol_scores, md_path, events_path)
    print(f"events={len(events)} signed_symbols={len(symbol_scores)} watchlist_overlap={sum(1 for row in rows if row['watchlist_overlap'])}", flush=True)
    print(f"csv={csv_path}", flush=True)
    print(f"markdown={md_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
