#!/usr/bin/env python3
"""Validate rolling profit protection against historical strategy ledgers."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import random
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from broker_position_sync import calculate_holding_management_lines


@dataclass(frozen=True)
class Episode:
    end_date: str
    ticker: str
    name: str
    entry_date: str
    entry_time: str
    entry_price: float
    target_price: float
    hard_stop_price: float
    target_touch_date: str
    old_exit_date: str
    old_return_pct: float


@dataclass(frozen=True)
class SimulationResult:
    end_date: str
    ticker: str
    name: str
    entry_date: str
    entry_time: str
    entry_price: float
    target_price: float
    hard_stop_price: float
    target_touch_date: str
    horizon_days: int
    old_return_pct: float
    new_exit_date: str
    new_exit_price: float
    new_return_pct: float
    improvement_pct: float
    exit_reason: str
    max_high_after_target: float
    final_target_price: float
    final_protection_price: float


@dataclass(frozen=True)
class TrendWinnerResult:
    ticker: str
    anchor_date: str
    manage_start_date: str
    horizon_days: int
    threshold_gain_pct: float
    cost_price: float
    baseline_exit_price: float
    baseline_return_pct: float
    new_exit_date: str
    new_exit_price: float
    new_return_pct: float
    improvement_pct: float
    exit_reason: str
    max_high_after_start: float
    final_target_price: float
    final_protection_price: float


def as_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_date(value: str) -> dt.date:
    text = str(value).strip()
    if len(text) >= 8 and text[:8].isdigit() and "-" not in text[:10]:
        return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return dt.date.fromisoformat(text[:10])


def truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def collect_target_episodes(ledger_root: Path) -> list[Episode]:
    episodes: list[Episode] = []
    ledger_paths = sorted(ledger_root.glob("*/strict_*_ledger.csv"))
    for ledger_path in ledger_paths:
        end_date = ledger_path.parent.name
        open_by_ticker: dict[str, dict[str, str]] = {}
        for row in load_rows(ledger_path):
            if row.get("period") != "12M":
                continue
            ticker = row.get("ticker", "")
            if row.get("action") == "BUY":
                open_by_ticker[ticker] = row
                continue
            if row.get("action") != "SELL":
                continue
            buy = open_by_ticker.pop(ticker, None)
            if not buy:
                continue
            target_hit = truthy(row.get("target_upper_touch")) or truthy(row.get("target_upper_sellable_hit"))
            if not target_hit:
                continue
            target_price = as_float(row.get("target_upper_price"), as_float(buy.get("target_upper_price")))
            entry_price = as_float(buy.get("price"))
            if entry_price <= 0 or target_price <= 0:
                continue
            episodes.append(
                Episode(
                    end_date=end_date,
                    ticker=ticker,
                    name=row.get("name", "") or buy.get("name", ""),
                    entry_date=buy.get("date", ""),
                    entry_time=buy.get("time", ""),
                    entry_price=entry_price,
                    target_price=target_price,
                    hard_stop_price=as_float(row.get("hard_stop_price"), as_float(buy.get("hard_stop_price"))),
                    target_touch_date=row.get("target_upper_sellable_date") or row.get("target_upper_touch_date") or row.get("date", ""),
                    old_exit_date=row.get("date", ""),
                    old_return_pct=(target_price / entry_price - 1.0) * 100.0,
                )
            )
    return episodes


def dedupe_episodes(episodes: list[Episode]) -> list[Episode]:
    best: dict[tuple[str, str, str, float], Episode] = {}
    for item in episodes:
        key = (item.ticker, item.entry_date, item.entry_time, round(item.entry_price, 4))
        current = best.get(key)
        if current is None or item.end_date > current.end_date:
            best[key] = item
    return sorted(best.values(), key=lambda item: (item.entry_date, item.ticker, item.end_date))


def load_daily_bars(cache_dir: Path, ticker: str, end_date: str) -> list[dict[str, Any]]:
    bars_by_date: dict[str, dict[str, Any]] = {}
    end = parse_date(end_date)
    for path in cache_dir.glob(f"{ticker}_*.json"):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            date_text = str(row.get("date", ""))
            if not date_text:
                continue
            try:
                date_value = parse_date(date_text)
            except ValueError:
                continue
            if date_value <= end:
                bars_by_date[date_text] = row
    return [bars_by_date[key] for key in sorted(bars_by_date)]


def load_all_daily_bars(cache_dir: Path) -> dict[str, list[dict[str, Any]]]:
    by_ticker: dict[str, dict[str, dict[str, Any]]] = {}
    for path in cache_dir.glob("*.json"):
        ticker = path.name.split("_", 1)[0]
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        bucket = by_ticker.setdefault(ticker, {})
        for row in rows:
            date_text = str(row.get("date", ""))
            if date_text:
                bucket[date_text] = row
    return {ticker: [rows[key] for key in sorted(rows)] for ticker, rows in by_ticker.items() if len(rows) >= 80}


def exit_at_stop(bar: dict[str, Any], stop: float) -> float:
    open_price = as_float(bar.get("open"))
    low = as_float(bar.get("low"))
    if open_price > 0 and open_price < stop:
        return open_price
    if low > 0 and low <= stop:
        return stop
    return 0.0


def simulate_episode(item: Episode, bars: list[dict[str, Any]], horizon_days: int) -> SimulationResult | None:
    if not bars:
        return None
    target_date = parse_date(item.target_touch_date)
    selected = [row for row in bars if parse_date(str(row.get("date"))) >= target_date]
    if not selected:
        return None

    highest = max(item.entry_price, item.target_price)
    target = item.target_price
    stop = max(0.0, item.hard_stop_price)
    exit_bar = selected[min(len(selected), horizon_days) - 1]
    exit_price = as_float(exit_bar.get("close"))
    exit_reason = "horizon_close"
    final_protection = stop
    max_high_after_target = highest

    for index, bar in enumerate(selected[:horizon_days]):
        high = as_float(bar.get("high"))
        close = as_float(bar.get("close"))
        highest = max(highest, high, close)
        max_high_after_target = max(max_high_after_target, highest)
        lines = calculate_holding_management_lines(
            cost_price=item.entry_price,
            latest_price=close,
            previous_target_price=target,
            previous_hard_stop_price=stop,
            previous_highest_price=highest,
            trailing_stop_pct=3.0,
            previous_management_state="PROFIT_PROTECTED",
            source_notes="国盛睿持仓同步",
            timestamp=f"{bar.get('date')}T00:00:00",
        )
        target = lines.target_price
        stop = lines.hard_stop_price
        final_protection = stop
        stop_exit = exit_at_stop(bar, stop)
        if stop_exit > 0:
            exit_bar = bar
            exit_price = stop_exit
            exit_reason = "dynamic_protection"
            break
        if index == horizon_days - 1:
            exit_bar = bar
            exit_price = close
            exit_reason = "horizon_close"

    new_return = (exit_price / item.entry_price - 1.0) * 100.0 if item.entry_price else 0.0
    return SimulationResult(
        end_date=item.end_date,
        ticker=item.ticker,
        name=item.name,
        entry_date=item.entry_date,
        entry_time=item.entry_time,
        entry_price=round(item.entry_price, 4),
        target_price=round(item.target_price, 4),
        hard_stop_price=round(item.hard_stop_price, 4),
        target_touch_date=item.target_touch_date,
        horizon_days=horizon_days,
        old_return_pct=round(item.old_return_pct, 4),
        new_exit_date=str(exit_bar.get("date", "")),
        new_exit_price=round(exit_price, 4),
        new_return_pct=round(new_return, 4),
        improvement_pct=round(new_return - item.old_return_pct, 4),
        exit_reason=exit_reason,
        max_high_after_target=round(max_high_after_target, 4),
        final_target_price=round(target, 4),
        final_protection_price=round(final_protection, 4),
    )


def simulate_trend_winner(
    ticker: str,
    bars: list[dict[str, Any]],
    anchor_index: int,
    start_index: int,
    threshold_gain_pct: float,
    horizon_days: int,
) -> TrendWinnerResult | None:
    anchor = bars[anchor_index]
    start = bars[start_index]
    cost = as_float(anchor.get("close"))
    start_close = as_float(start.get("close"))
    if cost <= 0 or start_close <= 0:
        return None
    selected = bars[start_index : start_index + horizon_days]
    if not selected:
        return None
    highest = max(cost, as_float(start.get("high")), start_close)
    lines = calculate_holding_management_lines(
        cost_price=cost,
        latest_price=start_close,
        previous_target_price=0.0,
        previous_hard_stop_price=0.0,
        previous_highest_price=highest,
        trailing_stop_pct=3.0,
        previous_management_state="PROFIT_PROTECTED",
        source_notes="国盛睿持仓同步",
        timestamp=f"{start.get('date')}T00:00:00",
    )
    target = lines.target_price
    stop = lines.hard_stop_price
    exit_bar = selected[-1]
    exit_price = as_float(exit_bar.get("close"))
    exit_reason = "horizon_close"
    final_protection = stop
    max_high_after_start = highest

    for index, bar in enumerate(selected):
        high = as_float(bar.get("high"))
        close = as_float(bar.get("close"))
        highest = max(highest, high, close)
        max_high_after_start = max(max_high_after_start, highest)
        lines = calculate_holding_management_lines(
            cost_price=cost,
            latest_price=close,
            previous_target_price=target,
            previous_hard_stop_price=stop,
            previous_highest_price=highest,
            trailing_stop_pct=3.0,
            previous_management_state="PROFIT_PROTECTED",
            source_notes="国盛睿持仓同步",
            timestamp=f"{bar.get('date')}T00:00:00",
        )
        target = lines.target_price
        stop = lines.hard_stop_price
        final_protection = stop
        stop_exit = exit_at_stop(bar, stop)
        if stop_exit > 0 and index > 0:
            exit_bar = bar
            exit_price = stop_exit
            exit_reason = "dynamic_protection"
            break
        if index == len(selected) - 1:
            exit_bar = bar
            exit_price = close
            exit_reason = "horizon_close"

    baseline_return = (start_close / cost - 1.0) * 100.0
    new_return = (exit_price / cost - 1.0) * 100.0
    return TrendWinnerResult(
        ticker=ticker,
        anchor_date=str(anchor.get("date", "")),
        manage_start_date=str(start.get("date", "")),
        horizon_days=horizon_days,
        threshold_gain_pct=threshold_gain_pct,
        cost_price=round(cost, 4),
        baseline_exit_price=round(start_close, 4),
        baseline_return_pct=round(baseline_return, 4),
        new_exit_date=str(exit_bar.get("date", "")),
        new_exit_price=round(exit_price, 4),
        new_return_pct=round(new_return, 4),
        improvement_pct=round(new_return - baseline_return, 4),
        exit_reason=exit_reason,
        max_high_after_start=round(max_high_after_start, 4),
        final_target_price=round(target, 4),
        final_protection_price=round(final_protection, 4),
    )


def collect_trend_winner_results(
    cache_dir: Path,
    thresholds: list[float],
    horizons: list[int],
    *,
    min_days_before_manage: int = 20,
    lookahead_days: int = 250,
    anchor_step_days: int = 20,
) -> list[TrendWinnerResult]:
    results: list[TrendWinnerResult] = []
    all_bars = load_all_daily_bars(cache_dir)
    for ticker, bars in all_bars.items():
        max_horizon = max(horizons)
        if len(bars) < min_days_before_manage + max_horizon + 1:
            continue
        for threshold in thresholds:
            index = 0
            while index < len(bars) - min_days_before_manage - max_horizon:
                cost = as_float(bars[index].get("close"))
                if cost <= 0:
                    index += anchor_step_days
                    continue
                start_index = None
                end = min(len(bars), index + lookahead_days)
                trigger = cost * (1.0 + threshold / 100.0)
                for probe in range(index + min_days_before_manage, end):
                    if as_float(bars[probe].get("close")) >= trigger:
                        start_index = probe
                        break
                if start_index is None:
                    index += anchor_step_days
                    continue
                for horizon in horizons:
                    result = simulate_trend_winner(ticker, bars, index, start_index, threshold, horizon)
                    if result:
                        results.append(result)
                index = start_index + max_horizon
    return results


def bootstrap_mean_ci(values: list[float], trials: int = 5000, seed: int = 20260705) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    boot = []
    for _ in range(trials):
        sample = [values[rng.randrange(len(values))] for _ in values]
        boot.append(mean(sample))
    boot.sort()
    lower = boot[int(0.025 * (len(boot) - 1))]
    upper = boot[int(0.975 * (len(boot) - 1))]
    positive_prob = sum(1 for value in boot if value > 0) / len(boot)
    return lower, upper, positive_prob


def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return max(0.0, center - half), min(1.0, center + half)


def summarize(results: list[SimulationResult], label: str) -> dict[str, Any]:
    improvements = [row.improvement_pct for row in results]
    positive = sum(1 for value in improvements if value > 0)
    non_negative = sum(1 for value in improvements if value >= 0)
    lower, upper, positive_prob = bootstrap_mean_ci(improvements)
    win_lower, win_upper = wilson_ci(positive, len(improvements))
    avg_improvement = mean(improvements) if improvements else 0.0
    med_improvement = median(improvements) if improvements else 0.0
    return {
        "sample": label,
        "n": len(results),
        "positive_improvement": positive,
        "non_negative_improvement": non_negative,
        "positive_rate_pct": round(positive / len(results) * 100, 4) if results else 0.0,
        "positive_rate_wilson95_low_pct": round(win_lower * 100, 4),
        "positive_rate_wilson95_high_pct": round(win_upper * 100, 4),
        "avg_improvement_pct": round(avg_improvement, 4),
        "median_improvement_pct": round(med_improvement, 4),
        "mean_improvement_bootstrap95_low_pct": round(lower, 4),
        "mean_improvement_bootstrap95_high_pct": round(upper, 4),
        "bootstrap_prob_mean_gt_0_pct": round(positive_prob * 100, 2),
        "avg_old_return_pct": round(mean([row.old_return_pct for row in results]), 4) if results else 0.0,
        "avg_new_return_pct": round(mean([row.new_return_pct for row in results]), 4) if results else 0.0,
        "protection_exit_rate_pct": round(
            sum(1 for row in results if row.exit_reason == "dynamic_protection") / len(results) * 100,
            4,
        )
        if results
        else 0.0,
    }


def summarize_trend(results: list[TrendWinnerResult], label: str) -> dict[str, Any]:
    improvements = [row.improvement_pct for row in results]
    positive = sum(1 for value in improvements if value > 0)
    non_negative = sum(1 for value in improvements if value >= 0)
    lower, upper, positive_prob = bootstrap_mean_ci(improvements)
    win_lower, win_upper = wilson_ci(positive, len(improvements))
    return {
        "sample": label,
        "n": len(results),
        "positive_improvement": positive,
        "non_negative_improvement": non_negative,
        "positive_rate_pct": round(positive / len(results) * 100, 4) if results else 0.0,
        "positive_rate_wilson95_low_pct": round(win_lower * 100, 4),
        "positive_rate_wilson95_high_pct": round(win_upper * 100, 4),
        "avg_improvement_pct": round(mean(improvements), 4) if improvements else 0.0,
        "median_improvement_pct": round(median(improvements), 4) if improvements else 0.0,
        "mean_improvement_bootstrap95_low_pct": round(lower, 4),
        "mean_improvement_bootstrap95_high_pct": round(upper, 4),
        "bootstrap_prob_mean_gt_0_pct": round(positive_prob * 100, 2),
        "avg_old_return_pct": round(mean([row.baseline_return_pct for row in results]), 4) if results else 0.0,
        "avg_new_return_pct": round(mean([row.new_return_pct for row in results]), 4) if results else 0.0,
        "protection_exit_rate_pct": round(
            sum(1 for row in results if row.exit_reason == "dynamic_protection") / len(results) * 100,
            4,
        )
        if results
        else 0.0,
    }


def confidence_label(summary: dict[str, Any]) -> str:
    n = int(summary["n"])
    prob = float(summary["bootstrap_prob_mean_gt_0_pct"])
    low = float(summary["mean_improvement_bootstrap95_low_pct"])
    if n >= 30 and low > 0 and prob >= 95:
        return "高"
    if n >= 8 and prob >= 80:
        return "中"
    return "低"


def write_outputs(
    out_dir: Path,
    unique_results: list[SimulationResult],
    rolling_results: list[SimulationResult],
    summaries: list[dict[str, Any]],
    trend_results: list[TrendWinnerResult],
    trend_summaries: list[dict[str, Any]],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    detail_path = out_dir / "profit_protection_validation_detail.csv"
    with detail_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = ["sample_type", *asdict((unique_results or rolling_results)[0]).keys()] if (unique_results or rolling_results) else ["sample_type"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for sample_type, rows in (("unique", unique_results), ("rolling", rolling_results)):
            for row in rows:
                writer.writerow({"sample_type": sample_type, **asdict(row)})

    summary_path = out_dir / "profit_protection_validation_summary.csv"
    with summary_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0].keys()) + ["confidence"] if summaries else ["sample"])
        writer.writeheader()
        for row in summaries:
            writer.writerow({**row, "confidence": confidence_label(row)})

    trend_detail_path = out_dir / "profit_protection_trend_winners_detail.csv"
    with trend_detail_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(asdict(trend_results[0]).keys()) if trend_results else ["ticker"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in trend_results:
            writer.writerow(asdict(row))

    trend_summary_path = out_dir / "profit_protection_trend_winners_summary.csv"
    with trend_summary_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(trend_summaries[0].keys()) + ["confidence"] if trend_summaries else ["sample"])
        writer.writeheader()
        for row in trend_summaries:
            writer.writerow({**row, "confidence": confidence_label(row)})

    md_path = out_dir / "profit_protection_validation.md"
    lines = [
        "# Profit Protection Validation",
        "",
        "## Method",
        "",
        "- Data: current-default strict 10-minute rolling 12M ledgers plus local daily-cache bars.",
        "- Sample: trades that reached target upper in the historical ledgers.",
        "- Baseline: sell at historical target upper.",
        "- Candidate: after target touch, keep the position and roll target/protection using the v0.4.52 dynamic profit-protection rule.",
        "- Horizon: results below are grouped by holding horizon in trading days.",
        "- Caveat: rolling windows overlap, so `unique` sample is the stricter confidence source; `rolling` is sensitivity evidence.",
        "",
        "## Summary",
        "",
        "| Sample | N | Avg Old | Avg New | Avg Lift | 95% Mean Lift CI | Positive Rate | 95% Rate CI | P(mean>0) | Confidence |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in summaries:
        lines.append(
            "| {sample} | {n} | {avg_old_return_pct:.2f}% | {avg_new_return_pct:.2f}% | {avg_improvement_pct:.2f}% | "
            "{mean_improvement_bootstrap95_low_pct:.2f}% ~ {mean_improvement_bootstrap95_high_pct:.2f}% | "
            "{positive_rate_pct:.1f}% | {positive_rate_wilson95_low_pct:.1f}% ~ {positive_rate_wilson95_high_pct:.1f}% | "
            "{bootstrap_prob_mean_gt_0_pct:.1f}% | {confidence} |".format(**row, confidence=confidence_label(row))
        )
    lines.extend(
        [
            "",
            "## Trend-Winner Scenario",
            "",
            "This second test targets the intended old-position use case: a position is already up by a large amount from its historical cost. Baseline exits immediately at the management-start close; candidate keeps holding with dynamic protection.",
            "",
            "| Sample | N | Avg Sell-Now | Avg Protected | Avg Lift | 95% Mean Lift CI | Positive Rate | 95% Rate CI | P(mean>0) | Confidence |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in trend_summaries:
        lines.append(
            "| {sample} | {n} | {avg_old_return_pct:.2f}% | {avg_new_return_pct:.2f}% | {avg_improvement_pct:.2f}% | "
            "{mean_improvement_bootstrap95_low_pct:.2f}% ~ {mean_improvement_bootstrap95_high_pct:.2f}% | "
            "{positive_rate_pct:.1f}% | {positive_rate_wilson95_low_pct:.1f}% ~ {positive_rate_wilson95_high_pct:.1f}% | "
            "{bootstrap_prob_mean_gt_0_pct:.1f}% | {confidence} |".format(**row, confidence=confidence_label(row))
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- If `P(mean>0)` is high but the 95% CI crosses zero, the direction is promising but sample size is too small for high confidence.",
            "- Use the `unique` rows as the conservative confidence call; use `rolling` rows to check whether the conclusion is stable under overlapping validation windows.",
            "",
            f"- Detail CSV: `{detail_path}`",
            f"- Summary CSV: `{summary_path}`",
            f"- Trend detail CSV: `{trend_detail_path}`",
            f"- Trend summary CSV: `{trend_summary_path}`",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    ledger_root = Path(args.ledger_root)
    cache_dir = Path(args.daily_cache_dir)
    out_dir = Path(args.out_dir)
    episodes = collect_target_episodes(ledger_root)
    unique_episodes = dedupe_episodes(episodes)
    all_results: list[SimulationResult] = []
    unique_results_all: list[SimulationResult] = []
    for horizon in args.horizons:
        for source, container in ((episodes, all_results), (unique_episodes, unique_results_all)):
            for episode in source:
                bars = load_daily_bars(cache_dir, episode.ticker, episode.end_date)
                result = simulate_episode(episode, bars, horizon)
                if result:
                    container.append(result)
    summaries = []
    for horizon in args.horizons:
        summaries.append(summarize([row for row in unique_results_all if row.horizon_days == horizon], f"unique_{horizon}d"))
        summaries.append(summarize([row for row in all_results if row.horizon_days == horizon], f"rolling_{horizon}d"))
    trend_results = collect_trend_winner_results(cache_dir, args.trend_thresholds, args.horizons)
    trend_summaries: list[dict[str, Any]] = []
    for threshold in args.trend_thresholds:
        for horizon in args.horizons:
            trend_summaries.append(
                summarize_trend(
                    [
                        row
                        for row in trend_results
                        if row.threshold_gain_pct == threshold and row.horizon_days == horizon
                    ],
                    f"trend_{int(threshold)}pct_{horizon}d",
                )
            )
    write_outputs(out_dir, unique_results_all, all_results, summaries, trend_results, trend_summaries)
    print(out_dir / "profit_protection_validation.md")
    for row in summaries:
        print(row["sample"], "n=", row["n"], "avg_lift=", row["avg_improvement_pct"], "p_mean_gt_0=", row["bootstrap_prob_mean_gt_0_pct"], "confidence=", confidence_label(row))
    for row in trend_summaries:
        print(row["sample"], "n=", row["n"], "avg_lift=", row["avg_improvement_pct"], "p_mean_gt_0=", row["bootstrap_prob_mean_gt_0_pct"], "confidence=", confidence_label(row))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate rolling profit-protection rule.")
    parser.add_argument("--ledger-root", default="output/rolling_v041_current_default_monthly_6x_12m/current_default")
    parser.add_argument("--daily-cache-dir", default="output/backtest_daily_cache")
    parser.add_argument("--out-dir", default="output/profit_protection_validation_20260705")
    parser.add_argument("--horizons", type=int, nargs="+", default=[20, 40, 60])
    parser.add_argument("--trend-thresholds", type=float, nargs="+", default=[50.0, 100.0, 120.0])
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
