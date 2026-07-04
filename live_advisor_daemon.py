#!/usr/bin/env python3
"""Run the short-term live advisor every N seconds and optionally publish reports."""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
import time
from pathlib import Path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="5-minute A-share short-term advisor daemon.")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--out-dir", default="output/live_advice")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--monitor-script", default="short_term_live_monitor.py")
    parser.add_argument("--watchlist", default="config/watchlist.mainboard_liquid.csv")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--mode", choices=["intraday", "daily"], default="intraday")
    parser.add_argument("--today", default="")
    parser.add_argument("--github-mode", choices=["none", "commit", "issue"], default="none")
    parser.add_argument("--github-issue-title", default="A-share short-term live advice")
    parser.add_argument("--git-pull-before-scan", action="store_true")
    parser.add_argument("--git-branch", default="")
    parser.add_argument("--market-hours-only", action="store_true")
    parser.add_argument("--extra-monitor-arg", action="append", default=[], help="extra raw arg passed to the monitor, repeatable")
    return parser


def in_market_window(now: dt.datetime) -> bool:
    if now.weekday() >= 5:
        return False
    morning = dt.time(9, 25) <= now.time() <= dt.time(11, 35)
    afternoon = dt.time(12, 55) <= now.time() <= dt.time(15, 10)
    return morning or afternoon


def run_command(command: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=check)


def git_publish_commit(cwd: Path, files: list[Path], message: str, branch: str) -> None:
    if branch:
        run_command(["git", "checkout", branch], cwd)
    rel_files = [str(path.relative_to(cwd)) for path in files if path.exists()]
    if not rel_files:
        return
    run_command(["git", "add", *rel_files], cwd)
    status = run_command(["git", "status", "--porcelain", "--", *rel_files], cwd, check=False)
    if not status.stdout.strip():
        return
    run_command(["git", "commit", "-m", message], cwd)
    run_command(["git", "push"], cwd)


def github_publish_issue(cwd: Path, title: str, report_path: Path) -> None:
    body = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    if not body:
        return
    run_command(["gh", "issue", "create", "--title", title, "--body", body], cwd)


def run_scan(args: argparse.Namespace, cwd: Path) -> tuple[Path, Path]:
    out_dir = (cwd / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report = out_dir / f"live_advice_{stamp}.md"
    csv_path = out_dir / f"live_advice_{stamp}.csv"
    command = [
        args.python,
        args.monitor_script,
        "--watchlist",
        args.watchlist,
        "--mode",
        args.mode,
        "--top",
        str(args.top),
        "--dynamic-params",
        "--max-gap-up",
        "0.02",
        "--gap-volume-threshold",
        "0",
        "--gap-volume-min-ratio",
        "1.3",
        "--entry-end-time",
        "11:20",
        "--normal-entry-end-time",
        "10:40",
        "--max-5d-range-pct",
        "32",
        "--max-momentum-10d-pct",
        "26",
        "--max-close-position-20d-pct",
        "85",
        "--normal-min-atr-pct",
        "4.1",
        "--cold-max-gap-up",
        "0.01",
        "--cold-gap-volume-min-ratio",
        "1.5",
        "--cold-max-5d-range-pct",
        "25",
        "--cold-max-momentum-10d-pct",
        "20",
        "--cold-max-close-position-20d-pct",
        "80",
        "--out",
        str(report),
        "--csv-out",
        str(csv_path),
    ]
    if args.today:
        command.extend(["--today", args.today])
    command.extend(args.extra_monitor_arg)
    result = run_command(command, cwd)
    print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    latest_md = out_dir / "latest.md"
    latest_csv = out_dir / "latest.csv"
    if report.exists():
        shutil.copyfile(report, latest_md)
    if csv_path.exists():
        shutil.copyfile(csv_path, latest_csv)
    return report, csv_path


def main() -> int:
    args = build_arg_parser().parse_args()
    cwd = Path.cwd().resolve()
    while True:
        now = dt.datetime.now()
        try:
            if args.market_hours_only and not in_market_window(now):
                print(f"{now:%Y-%m-%d %H:%M:%S} outside market window; sleeping")
            else:
                if args.git_pull_before_scan:
                    run_command(["git", "pull", "--ff-only"], cwd, check=False)
                report, csv_path = run_scan(args, cwd)
                if args.github_mode == "commit":
                    git_publish_commit(cwd, [report, csv_path, report.parent / "latest.md", report.parent / "latest.csv"], f"live advice {now:%Y-%m-%d %H:%M}", args.git_branch)
                elif args.github_mode == "issue":
                    github_publish_issue(cwd, args.github_issue_title, report)
        except subprocess.CalledProcessError as exc:
            print(f"command failed: {' '.join(exc.cmd)}", file=sys.stderr)
            print(exc.stdout, file=sys.stderr)
            print(exc.stderr, file=sys.stderr)
        except Exception as exc:
            print(f"scan failed: {exc}", file=sys.stderr)
        if args.once:
            break
        time.sleep(max(30, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
