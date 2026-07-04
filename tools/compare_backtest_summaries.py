#!/usr/bin/env python3
"""Compare strict backtest summary directories against a baseline."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ScenarioInput:
    name: str
    directory: Path


@dataclass(frozen=True)
class ScenarioFiles:
    name: str
    directory: Path
    summary_path: Path
    ledger_path: Path


def parse_csv_list(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def parse_scenario(raw: str) -> ScenarioInput:
    if "=" in raw:
        name, directory = raw.split("=", 1)
        scenario_name = name.strip()
        scenario_dir = directory.strip()
    else:
        scenario_dir = raw.strip()
        scenario_name = Path(scenario_dir).name
    if not scenario_name or not scenario_dir:
        raise argparse.ArgumentTypeError(f"invalid scenario value: {raw!r}")
    return ScenarioInput(scenario_name, Path(scenario_dir))


def resolve_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return ROOT / path


def relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def find_single_file(directory: Path, suffix: str, excluded_tokens: tuple[str, ...] = ()) -> Path:
    matches = [
        path
        for path in sorted(directory.glob(f"*{suffix}"))
        if not any(token in path.name for token in excluded_tokens)
    ]
    if not matches:
        raise FileNotFoundError(f"missing *{suffix} in {directory}")
    if len(matches) > 1:
        newest = max(matches, key=lambda path: path.stat().st_mtime)
        return newest
    return matches[0]


def resolve_files(scenario: ScenarioInput) -> ScenarioFiles:
    directory = resolve_path(scenario.directory)
    if not directory.exists():
        raise FileNotFoundError(f"missing scenario directory: {directory}")
    summary_path = find_single_file(directory, "_summary.csv", ("sell_path", "condition"))
    ledger_path = find_single_file(directory, "_ledger.csv")
    return ScenarioFiles(scenario.name, directory, summary_path, ledger_path)


def read_summary(path: Path) -> dict[str, dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty summary: {path}")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        period = str(row.get("period") or "").strip()
        if not period:
            continue
        result[period] = row
    return result


def ledger_quality(path: Path) -> dict[str, int]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    bad_300_301 = 0
    bad_lots = 0
    bad_tick = 0
    for row in rows:
        ticker = str(row.get("ticker") or "")
        action = str(row.get("action") or "")
        if ticker.startswith(("300", "301")):
            bad_300_301 += 1
        if action == "BUY":
            shares = int(float(row.get("shares") or 0))
            if shares % 100 != 0:
                bad_lots += 1
        if action in {"BUY", "SELL", "PARTIAL_SELL"}:
            price = float(row.get("price") or 0)
            if abs(price * 100 - round(price * 100)) > 1e-6:
                bad_tick += 1
    return {
        "bad_300_301": bad_300_301,
        "bad_lots": bad_lots,
        "bad_tick": bad_tick,
    }


def as_float(row: dict[str, Any], field: str) -> float:
    return float(row.get(field) or 0.0)


def as_int(row: dict[str, Any], field: str) -> int:
    return int(float(row.get(field) or 0))


def compare_scenario(
    baseline_files: ScenarioFiles,
    candidate_files: ScenarioFiles,
    periods: list[str],
    dd_period: str,
    min_trade_ratio: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    baseline_summary = read_summary(baseline_files.summary_path)
    candidate_summary = read_summary(candidate_files.summary_path)
    candidate_quality = ledger_quality(candidate_files.ledger_path)

    rows: list[dict[str, Any]] = []
    missing_periods: list[str] = []
    return_gate = True
    dd_gate = True
    trade_gate = True
    for period in periods:
        baseline_row = baseline_summary.get(period)
        candidate_row = candidate_summary.get(period)
        if baseline_row is None or candidate_row is None:
            missing_periods.append(period)
            return_gate = False
            continue
        base_return = as_float(baseline_row, "return_pct")
        cand_return = as_float(candidate_row, "return_pct")
        base_dd = as_float(baseline_row, "max_drawdown_pct")
        cand_dd = as_float(candidate_row, "max_drawdown_pct")
        base_trades = as_int(baseline_row, "closed_trades")
        cand_trades = as_int(candidate_row, "closed_trades")
        return_delta = cand_return - base_return
        dd_delta = cand_dd - base_dd
        trade_ratio = cand_trades / base_trades if base_trades else 0.0
        period_return_gate = return_delta >= -1e-9
        period_dd_gate = period != dd_period or dd_delta <= 1e-9
        period_trade_gate = period != dd_period or trade_ratio + 1e-9 >= min_trade_ratio
        return_gate = return_gate and period_return_gate
        dd_gate = dd_gate and period_dd_gate
        trade_gate = trade_gate and period_trade_gate
        rows.append(
            {
                "scenario": candidate_files.name,
                "period": period,
                "baseline_return_pct": round(base_return, 4),
                "candidate_return_pct": round(cand_return, 4),
                "return_delta_pct": round(return_delta, 4),
                "baseline_max_drawdown_pct": round(base_dd, 4),
                "candidate_max_drawdown_pct": round(cand_dd, 4),
                "drawdown_delta_pct": round(dd_delta, 4),
                "baseline_trades": base_trades,
                "candidate_trades": cand_trades,
                "trade_ratio": round(trade_ratio, 4),
                "baseline_profit_factor": round(as_float(baseline_row, "profit_factor"), 4),
                "candidate_profit_factor": round(as_float(candidate_row, "profit_factor"), 4),
                "return_gate": period_return_gate,
                "drawdown_gate": period_dd_gate,
                "trade_gate": period_trade_gate,
                **candidate_quality,
                "baseline_summary_path": relative_path(baseline_files.summary_path),
                "candidate_summary_path": relative_path(candidate_files.summary_path),
                "baseline_ledger_path": relative_path(baseline_files.ledger_path),
                "candidate_ledger_path": relative_path(candidate_files.ledger_path),
            }
        )

    bad_gate = (
        candidate_quality["bad_300_301"] == 0
        and candidate_quality["bad_lots"] == 0
        and candidate_quality["bad_tick"] == 0
    )
    gates = {
        "scenario": candidate_files.name,
        "return_gate": return_gate,
        "drawdown_gate": dd_gate,
        "trade_gate": trade_gate,
        "bad_gate": bad_gate,
        "passes": return_gate and dd_gate and trade_gate and bad_gate and not missing_periods,
        "missing_periods": ",".join(missing_periods),
        "bad_300_301": candidate_quality["bad_300_301"],
        "bad_lots": candidate_quality["bad_lots"],
        "bad_tick": candidate_quality["bad_tick"],
        "summary_path": relative_path(candidate_files.summary_path),
        "ledger_path": relative_path(candidate_files.ledger_path),
    }
    return rows, gates


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows: list[dict[str, Any]], gates: list[dict[str, Any]], path: Path, dd_period: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Backtest Summary Comparison",
        "",
        "| Scenario | Period | Base Return | Cand Return | Delta | Base DD | Cand DD | DD Delta | Trades | Trade Ratio | PF | Gates |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        gates_text = f"R:{row['return_gate']} DD:{row['drawdown_gate']} T:{row['trade_gate']} Bad:{row['bad_300_301']}/{row['bad_lots']}/{row['bad_tick']}"
        lines.append(
            f"| {row['scenario']} | {row['period']} | {row['baseline_return_pct']:.4f}% | {row['candidate_return_pct']:.4f}% | {row['return_delta_pct']:.4f} | {row['baseline_max_drawdown_pct']:.4f}% | {row['candidate_max_drawdown_pct']:.4f}% | {row['drawdown_delta_pct']:.4f} | {row['candidate_trades']}/{row['baseline_trades']} | {row['trade_ratio']:.4f} | {row['candidate_profit_factor']:.4f} | {gates_text} |"
        )
    lines.extend(["", "## Acceptance", ""])
    lines.append(f"| Scenario | Returns | {dd_period} DD | {dd_period} Trades | Bad Checks | Passes | Missing Periods |")
    lines.append("|---|---|---|---|---|---|---|")
    for gate in gates:
        bad_checks = f"{gate['bad_300_301']}/{gate['bad_lots']}/{gate['bad_tick']}"
        lines.append(
            f"| {gate['scenario']} | {gate['return_gate']} | {gate['drawdown_gate']} | {gate['trade_gate']} | {bad_checks} | {gate['passes']} | {gate['missing_periods']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", required=True, type=Path)
    parser.add_argument("--baseline-name", default="baseline")
    parser.add_argument(
        "--candidate-dir",
        action="append",
        required=True,
        type=parse_scenario,
        help="Candidate directory, optionally named as NAME=DIR.",
    )
    parser.add_argument("--periods", default="1M,3M,6M,9M,12M")
    parser.add_argument("--drawdown-period", default="12M")
    parser.add_argument("--min-trade-ratio", type=float, default=0.8)
    parser.add_argument("--out-csv", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--strict-exit-code", action="store_true")
    args = parser.parse_args()

    periods = parse_csv_list(args.periods)
    if not periods:
        raise SystemExit("at least one period is required")
    if args.drawdown_period not in periods:
        raise SystemExit("--drawdown-period must be included in --periods")

    baseline_files = resolve_files(ScenarioInput(args.baseline_name, args.baseline_dir))
    all_rows: list[dict[str, Any]] = []
    all_gates: list[dict[str, Any]] = []
    for candidate in args.candidate_dir:
        candidate_files = resolve_files(candidate)
        rows, gates = compare_scenario(
            baseline_files,
            candidate_files,
            periods,
            args.drawdown_period,
            args.min_trade_ratio,
        )
        all_rows.extend(rows)
        all_gates.append(gates)

    if args.out_csv:
        write_csv(all_rows, resolve_path(args.out_csv))
    if args.out_md:
        write_markdown(all_rows, all_gates, resolve_path(args.out_md), args.drawdown_period)

    for gate in all_gates:
        status = "PASS" if gate["passes"] else "FAIL"
        print(
            f"{status} {gate['scenario']}: returns={gate['return_gate']} "
            f"{args.drawdown_period}_dd={gate['drawdown_gate']} "
            f"{args.drawdown_period}_trades={gate['trade_gate']} "
            f"bad={gate['bad_300_301']}/{gate['bad_lots']}/{gate['bad_tick']}"
        )
    if args.strict_exit_code and any(not gate["passes"] for gate in all_gates):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
