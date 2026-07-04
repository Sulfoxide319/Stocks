#!/usr/bin/env python3
"""Rolling validation wrapper for strict 10-minute strategy backtests."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STRICT_SCRIPT = ROOT / "tools" / "backtest_strict_10m_ledger.py"


@dataclass(frozen=True)
class Scenario:
    name: str
    args: list[str]


def parse_csv_list(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def default_scenarios(include_v0432_proxy: bool, include_v0438_proxy: bool) -> list[Scenario]:
    scenarios = [Scenario("current_default", [])]
    if include_v0438_proxy:
        scenarios.append(
            Scenario(
                "v0438_proxy",
                [
                    "--profit-cushion-aggressive-threshold",
                    "0",
                ],
            )
        )
    if include_v0432_proxy:
        scenarios.append(
            Scenario(
                "v0432_proxy",
                [
                    "--cold-min-atr-pct",
                    "0",
                    "--cold-capital-factor",
                    "0.9",
                ],
            )
        )
    return scenarios


def find_existing_outputs(out_dir: Path, end_date: str) -> tuple[Path, Path] | None:
    suffix = end_date.replace("-", "")
    summary_matches = sorted(out_dir.glob(f"*to_{suffix}_summary.csv"))
    ledger_matches = sorted(out_dir.glob(f"*to_{suffix}_ledger.csv"))
    if not summary_matches or not ledger_matches:
        return None
    return summary_matches[-1], ledger_matches[-1]


def run_backtest(scenario: Scenario, end_date: str, period_months: str, out_root: Path, reuse_existing: bool) -> tuple[Path, Path]:
    out_dir = out_root / scenario.name / end_date.replace("-", "")
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = find_existing_outputs(out_dir, end_date)
    if reuse_existing and existing:
        print(f"reusing {scenario.name} end_date={end_date}", flush=True)
        return existing
    command = [
        sys.executable,
        str(STRICT_SCRIPT),
        "--end-date",
        end_date,
        "--period-months",
        period_months,
        "--out-dir",
        str(out_dir),
        *scenario.args,
    ]
    print(f"running {scenario.name} end_date={end_date} periods={period_months}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)
    existing = find_existing_outputs(out_dir, end_date)
    if not existing:
        raise FileNotFoundError(f"missing strict outputs for {scenario.name} {end_date} in {out_dir}")
    return existing


def ledger_quality(ledger_path: Path) -> dict[str, int]:
    with ledger_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    bad_300_301 = [row for row in rows if str(row.get("ticker", "")).startswith(("300", "301"))]
    bad_lots = [
        row
        for row in rows
        if row.get("action") == "BUY" and int(float(row.get("shares") or 0)) % 100 != 0
    ]
    bad_tick = []
    for row in rows:
        if row.get("action") not in {"BUY", "SELL", "PARTIAL_SELL"}:
            continue
        price = float(row.get("price") or 0)
        if abs(price * 100 - round(price * 100)) > 1e-6:
            bad_tick.append(row)
    return {
        "bad_300_301": len(bad_300_301),
        "bad_lots": len(bad_lots),
        "bad_tick": len(bad_tick),
    }


def collect_summary(scenario: Scenario, end_date: str, summary_path: Path, ledger_path: Path) -> list[dict[str, Any]]:
    quality = ledger_quality(ledger_path)
    rows: list[dict[str, Any]] = []
    with summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                {
                    "scenario": scenario.name,
                    "end_date": end_date,
                    "period": row["period"],
                    "return_pct": float(row["return_pct"]),
                    "max_drawdown_pct": float(row["max_drawdown_pct"]),
                    "closed_trades": int(row["closed_trades"]),
                    "win_rate_pct": float(row["win_rate_pct"]),
                    "profit_factor": float(row["profit_factor"]),
                    "cold_min_atr_pct": row.get("cold_min_atr_pct", ""),
                    "cold_min_momentum_10d_pct": row.get("cold_min_momentum_10d_pct", ""),
                    "cold_capital_factor": row.get("cold_capital_factor", ""),
                    **quality,
                    "summary_path": str(summary_path.resolve().relative_to(ROOT)),
                    "ledger_path": str(ledger_path.resolve().relative_to(ROOT)),
                }
            )
    return rows


def add_baseline_deltas(rows: list[dict[str, Any]], baseline_name: str) -> None:
    baselines = {
        (row["end_date"], row["period"]): row
        for row in rows
        if row["scenario"] == baseline_name
    }
    for row in rows:
        base = baselines.get((row["end_date"], row["period"]))
        if not base or row["scenario"] == baseline_name:
            row["return_delta_pct"] = ""
            row["drawdown_delta_pct"] = ""
            row["trade_delta"] = ""
            row["beats_baseline"] = ""
            continue
        return_delta = row["return_pct"] - base["return_pct"]
        drawdown_delta = row["max_drawdown_pct"] - base["max_drawdown_pct"]
        trade_delta = row["closed_trades"] - base["closed_trades"]
        row["return_delta_pct"] = round(return_delta, 4)
        row["drawdown_delta_pct"] = round(drawdown_delta, 4)
        row["trade_delta"] = trade_delta
        row["beats_baseline"] = bool(return_delta >= -1e-9 and drawdown_delta <= 1e-9)


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "scenario",
        "end_date",
        "period",
        "return_pct",
        "max_drawdown_pct",
        "closed_trades",
        "win_rate_pct",
        "profit_factor",
        "return_delta_pct",
        "drawdown_delta_pct",
        "trade_delta",
        "beats_baseline",
        "bad_300_301",
        "bad_lots",
        "bad_tick",
        "cold_min_atr_pct",
        "cold_min_momentum_10d_pct",
        "cold_capital_factor",
        "summary_path",
        "ledger_path",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows: list[dict[str, Any]], path: Path, baseline_name: str) -> None:
    lines = [
        "# Rolling Strict 10m Validation",
        "",
        f"Baseline scenario: `{baseline_name}`.",
        "",
        "| Scenario | End Date | Period | Return% | Max DD% | Trades | PF | Return Delta | DD Delta | Beats Baseline | Bad Checks |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        bad_checks = f"{row['bad_300_301']}/{row['bad_lots']}/{row['bad_tick']}"
        lines.append(
            f"| {row['scenario']} | {row['end_date']} | {row['period']} | {row['return_pct']:.4f} | {row['max_drawdown_pct']:.4f} | {row['closed_trades']} | {row['profit_factor']:.4f} | {row['return_delta_pct']} | {row['drawdown_delta_pct']} | {row['beats_baseline']} | {bad_checks} |"
        )
    compared = [row for row in rows if row.get("beats_baseline") != ""]
    return_wins = sum(1 for row in compared if float(row.get("return_delta_pct") or 0.0) >= 0)
    drawdown_wins = sum(1 for row in compared if float(row.get("drawdown_delta_pct") or 0.0) <= 0)
    beats_both = sum(1 for row in compared if row.get("beats_baseline") is True)
    bad_rows = sum(
        1
        for row in rows
        if int(row["bad_300_301"]) or int(row["bad_lots"]) or int(row["bad_tick"])
    )
    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Compared rows: `{len(compared)}`.",
            f"- Rows with return >= baseline: `{return_wins}`.",
            f"- Rows with drawdown <= baseline: `{drawdown_wins}`.",
            f"- Rows beating baseline on both return and drawdown: `{beats_both}`.",
            f"- Rows with any bad check: `{bad_rows}`.",
            "- Bad checks are `bad_300_301/bad_lots/bad_tick`.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run rolling strict 10m validations.")
    parser.add_argument("--end-dates", default="2026-05-29,2026-06-30,2026-07-03")
    parser.add_argument("--period-months", default="1,3,6")
    parser.add_argument("--out-dir", default="output/rolling_strict_10m_validation")
    parser.add_argument("--baseline-name", default="v0432_proxy")
    parser.add_argument("--no-v0432-proxy", action="store_true")
    parser.add_argument("--include-v0438-proxy", action="store_true")
    parser.add_argument("--reuse-existing", action="store_true", help="Reuse matching summary/ledger files instead of rerunning backtests.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    out_root = Path(args.out_dir)
    scenarios = default_scenarios(not args.no_v0432_proxy, args.include_v0438_proxy)
    rows: list[dict[str, Any]] = []
    for end_date in parse_csv_list(args.end_dates):
        for scenario in scenarios:
            summary_path, ledger_path = run_backtest(scenario, end_date, args.period_months, out_root, args.reuse_existing)
            rows.extend(collect_summary(scenario, end_date, summary_path, ledger_path))
    add_baseline_deltas(rows, args.baseline_name)
    rows.sort(key=lambda row: (row["end_date"], row["period"], row["scenario"]))
    csv_path = out_root / "rolling_strict_10m_validation.csv"
    md_path = out_root / "rolling_strict_10m_validation.md"
    write_csv(rows, csv_path)
    write_markdown(rows, md_path, args.baseline_name)
    print(f"csv={csv_path}", flush=True)
    print(f"markdown={md_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
