#!/usr/bin/env python3
"""Nightly GitHub publisher for local advice summaries."""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish latest local stock advice to GitHub after market close.")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="")
    parser.add_argument("--pull", action="store_true")
    parser.add_argument("--include-live-advice", action="store_true")
    return parser


def run(command: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=check)


def main() -> int:
    args = build_arg_parser().parse_args()
    cwd = Path(__file__).resolve().parent
    files = [
        cwd / "output" / "trading_assistant" / "latest_plan.md",
        cwd / "output" / "trading_assistant" / "latest_plan.csv",
        cwd / "output" / "trading_assistant" / "latest_plan.json",
    ]
    if args.include_live_advice:
        files.extend(
            [
                cwd / "output" / "live_advice" / "latest.md",
                cwd / "output" / "live_advice" / "latest.csv",
            ]
        )
    existing = [str(path.relative_to(cwd)) for path in files if path.exists()]
    if not existing:
        print("No latest advice files to publish.")
        return 0
    if args.branch:
        run(["git", "checkout", args.branch], cwd)
    if args.pull:
        run(["git", "pull", "--ff-only"], cwd, check=False)
    run(["git", "add", *existing], cwd)
    status = run(["git", "status", "--porcelain", "--", *existing], cwd, check=False)
    if not status.stdout.strip():
        print("No changes to publish.")
        return 0
    message = args.message or f"nightly advice {dt.datetime.now():%Y-%m-%d}"
    run(["git", "commit", "-m", message], cwd)
    run(["git", "push"], cwd)
    print(f"Published {len(existing)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
