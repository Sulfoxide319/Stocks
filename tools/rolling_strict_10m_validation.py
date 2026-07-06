#!/usr/bin/env python3
"""Rolling validation wrapper for strict 10-minute strategy backtests."""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import shlex
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


def parse_date(raw: str) -> dt.date:
    return dt.date.fromisoformat(raw)


def last_weekday_on_or_before(date_value: dt.date) -> dt.date:
    while date_value.weekday() >= 5:
        date_value -= dt.timedelta(days=1)
    return date_value


def monthly_end_dates(end_to: dt.date, count: int) -> list[str]:
    if count <= 0:
        return []
    dates: list[dt.date] = []
    year = end_to.year
    month = end_to.month
    for offset in range(count):
        current_month = month - offset
        current_year = year
        while current_month <= 0:
            current_month += 12
            current_year -= 1
        if current_year == end_to.year and current_month == end_to.month:
            candidate = end_to
        else:
            day = calendar.monthrange(current_year, current_month)[1]
            candidate = dt.date(current_year, current_month, day)
        dates.append(last_weekday_on_or_before(candidate))
    return [item.isoformat() for item in sorted(set(dates))]


def parse_scenario(raw: str) -> Scenario:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("scenario must be name=--arg value ...")
    name, args_text = raw.split("=", 1)
    scenario_name = name.strip()
    if not scenario_name:
        raise argparse.ArgumentTypeError(f"invalid scenario name: {raw!r}")
    return Scenario(scenario_name, shlex.split(args_text.strip(), posix=False))


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


def preset_scenarios(names: list[str]) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for name in names:
        if name == "quality_sizing":
            scenarios.extend(
                [
                    Scenario("equal_sizing", ["--position-sizing-mode", "equal"]),
                    Scenario("score_linear_sizing", ["--position-sizing-mode", "score_linear"]),
                    Scenario("edge_linear_sizing", ["--position-sizing-mode", "edge_linear"]),
                    Scenario("quality_max135", ["--position-sizing-mode", "quality", "--quality-capital-max-factor", "1.35"]),
                    Scenario(
                        "quality_no_dd_governor",
                        ["--position-sizing-mode", "quality", "--equity-drawdown-capital-threshold", "0"],
                    ),
                    Scenario(
                        "quality_dd04_f075",
                        [
                            "--position-sizing-mode",
                            "quality",
                            "--equity-drawdown-capital-threshold",
                            "0.04",
                            "--equity-drawdown-capital-factor",
                            "0.75",
                        ],
                    ),
                    Scenario(
                        "quality_dd045_f085",
                        [
                            "--position-sizing-mode",
                            "quality",
                            "--equity-drawdown-capital-threshold",
                            "0.045",
                            "--equity-drawdown-capital-factor",
                            "0.85",
                        ],
                    ),
                ]
            )
        elif name:
            raise argparse.ArgumentTypeError(f"unknown tuning preset: {name}")
    return scenarios


def unique_scenarios(scenarios: list[Scenario]) -> list[Scenario]:
    seen: set[str] = set()
    result: list[Scenario] = []
    for scenario in scenarios:
        if scenario.name in seen:
            continue
        seen.add(scenario.name)
        result.append(scenario)
    return result


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
    aggregate_rows = summarize_by_scenario_period(rows, baseline_name)
    lines = [
        "# Rolling Strict 10m Validation",
        "",
        f"Baseline scenario: `{baseline_name}`.",
        "",
        "## Scenario Aggregates",
        "",
        "| Scenario | Period | Windows | Avg Return% | Min Return% | Avg DD% | Max DD% | Avg Trades | Return Wins | DD Wins | Both Wins | Bad Rows |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in aggregate_rows:
        lines.append(
            f"| {row['scenario']} | {row['period']} | {row['windows']} | {row['avg_return_pct']:.4f} | {row['min_return_pct']:.4f} | {row['avg_drawdown_pct']:.4f} | {row['max_drawdown_pct']:.4f} | {row['avg_trades']:.2f} | {row['return_wins']} | {row['drawdown_wins']} | {row['both_wins']} | {row['bad_rows']} |"
        )
    lines.extend(
        [
            "",
            "## Detailed Rows",
            "",
        "| Scenario | End Date | Period | Return% | Max DD% | Trades | PF | Return Delta | DD Delta | Beats Baseline | Bad Checks |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
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


def summarize_by_scenario_period(rows: list[dict[str, Any]], baseline_name: str) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((str(row["scenario"]), str(row["period"])), []).append(row)
    result: list[dict[str, Any]] = []
    for (scenario, period), items in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0])):
        return_values = [float(item["return_pct"]) for item in items]
        drawdown_values = [float(item["max_drawdown_pct"]) for item in items]
        trades = [int(item["closed_trades"]) for item in items]
        compared = [item for item in items if item.get("return_delta_pct") != ""]
        return_wins = sum(1 for item in compared if float(item.get("return_delta_pct") or 0.0) >= 0)
        drawdown_wins = sum(1 for item in compared if float(item.get("drawdown_delta_pct") or 0.0) <= 0)
        both_wins = sum(1 for item in compared if item.get("beats_baseline") is True)
        if scenario == baseline_name:
            return_wins = drawdown_wins = both_wins = len(items)
        bad_rows = sum(1 for item in items if int(item["bad_300_301"]) or int(item["bad_lots"]) or int(item["bad_tick"]))
        result.append(
            {
                "scenario": scenario,
                "period": period,
                "windows": len(items),
                "avg_return_pct": sum(return_values) / len(return_values) if return_values else 0.0,
                "min_return_pct": min(return_values) if return_values else 0.0,
                "avg_drawdown_pct": sum(drawdown_values) / len(drawdown_values) if drawdown_values else 0.0,
                "max_drawdown_pct": max(drawdown_values) if drawdown_values else 0.0,
                "avg_trades": sum(trades) / len(trades) if trades else 0.0,
                "return_wins": return_wins,
                "drawdown_wins": drawdown_wins,
                "both_wins": both_wins,
                "bad_rows": bad_rows,
            }
        )
    return result


def write_aggregate_csv(rows: list[dict[str, Any]], path: Path, baseline_name: str) -> None:
    aggregate_rows = summarize_by_scenario_period(rows, baseline_name)
    if not aggregate_rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(aggregate_rows[0].keys()))
        writer.writeheader()
        writer.writerows(aggregate_rows)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run rolling strict 10m validations.")
    parser.add_argument("--end-dates", default="2026-05-29,2026-06-30,2026-07-03")
    parser.add_argument("--monthly-end-date-count", type=int, default=0, help="Generate recent month-end dates ending at --monthly-end-date-to; overrides --end-dates when positive.")
    parser.add_argument("--monthly-end-date-to", default="2026-07-03")
    parser.add_argument("--period-months", default="1,3,6")
    parser.add_argument("--out-dir", default="output/rolling_strict_10m_validation")
    parser.add_argument("--baseline-name", default="v0432_proxy")
    parser.add_argument("--no-v0432-proxy", action="store_true")
    parser.add_argument("--include-v0438-proxy", action="store_true")
    parser.add_argument("--tuning-preset", default="", help="Comma-separated scenario presets. Supported: quality_sizing.")
    parser.add_argument("--scenario", action="append", type=parse_scenario, default=[], help="Add a custom scenario as name=--arg value ...")
    parser.add_argument("--reuse-existing", action="store_true", help="Reuse matching summary/ledger files instead of rerunning backtests.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    out_root = Path(args.out_dir)
    scenarios = default_scenarios(not args.no_v0432_proxy, args.include_v0438_proxy)
    scenarios.extend(preset_scenarios(parse_csv_list(args.tuning_preset)))
    scenarios.extend(args.scenario)
    scenarios = unique_scenarios(scenarios)
    end_dates = (
        monthly_end_dates(parse_date(args.monthly_end_date_to), args.monthly_end_date_count)
        if args.monthly_end_date_count > 0
        else parse_csv_list(args.end_dates)
    )
    rows: list[dict[str, Any]] = []
    for end_date in end_dates:
        for scenario in scenarios:
            summary_path, ledger_path = run_backtest(scenario, end_date, args.period_months, out_root, args.reuse_existing)
            rows.extend(collect_summary(scenario, end_date, summary_path, ledger_path))
    add_baseline_deltas(rows, args.baseline_name)
    rows.sort(key=lambda row: (row["end_date"], row["period"], row["scenario"]))
    csv_path = out_root / "rolling_strict_10m_validation.csv"
    aggregate_csv_path = out_root / "rolling_strict_10m_aggregate.csv"
    md_path = out_root / "rolling_strict_10m_validation.md"
    write_csv(rows, csv_path)
    write_aggregate_csv(rows, aggregate_csv_path, args.baseline_name)
    write_markdown(rows, md_path, args.baseline_name)
    print(f"csv={csv_path}", flush=True)
    print(f"aggregate_csv={aggregate_csv_path}", flush=True)
    print(f"markdown={md_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
