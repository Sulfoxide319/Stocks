#!/usr/bin/env python3
"""Local A-share trading assistant with scheduled focus, buy, and sell advice."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

from dependency_bootstrap import ensure_project_dependencies

ensure_project_dependencies()

from baostock_intraday import BaoStock5mClient
from broker_position_sync import calculate_holding_management_lines, calculate_profit_protection_result
from short_term_live_monitor import fetch_sina_quote
from trading_journal import archive_trading_day, record_assistant_run
from app_storage import connect as connect_app_storage
from app_storage import default_db_path, export_open_positions_csv, save_latest_snapshot, update_positions_from_csv


MONITOR_DEFAULT_ARGS = [
    "--dynamic-params",
    "--history-timeout",
    "5",
    "--min-score",
    "83",
    "--buy-min-score",
    "90",
    "--skip-hot-entries",
    "--hot-min-score",
    "90",
    "--max-gap-up",
    "0.02",
    "--gap-volume-threshold",
    "0",
    "--gap-volume-min-ratio",
    "1.5",
    "--entry-end-time",
    "11:20",
    "--normal-entry-end-time",
    "10:40",
    "--vwap-buffer",
    "0.003",
    "--max-5d-range-pct",
    "32",
    "--max-momentum-10d-pct",
    "26",
    "--max-close-position-20d-pct",
    "85",
    "--normal-min-score",
    "87",
    "--normal-gap-volume-min-ratio",
    "1.5",
    "--normal-min-atr-pct",
    "4.1",
    "--narrow-rally-min-score",
    "83",
    "--narrow-rally-max-gap-up",
    "0.01",
    "--narrow-rally-gap-volume-min-ratio",
    "1.5",
    "--narrow-rally-max-5d-range-pct",
    "32",
    "--narrow-rally-max-momentum-10d-pct",
    "26",
    "--narrow-rally-max-close-position-20d-pct",
    "88",
    "--cold-min-score",
    "87",
    "--cold-max-gap-up",
    "0.01",
    "--cold-gap-volume-min-ratio",
    "1.5",
    "--cold-min-atr-pct",
    "4.1",
    "--cold-min-momentum-10d-pct",
    "7.5",
    "--cold-max-5d-range-pct",
    "25",
    "--cold-max-momentum-10d-pct",
    "20",
    "--cold-max-close-position-20d-pct",
    "80",
]

DEFAULT_MONITOR_TIMEOUT_SECONDS = 15 * 60


@dataclass
class BuyAdvice:
    ticker: str
    name: str
    action: str
    priority: int
    latest_price: float
    trigger_price: float
    vwap: float
    target_price: float
    first_manage_price: float
    hard_stop_price: float
    target_pct: float
    first_manage_pct: float
    hard_stop_pct: float
    target_upper_hit_rate_pct: float | None
    target_upper_touch_rate_pct: float | None
    first_manage_hit_rate_pct: float | None
    hit_rate_sample_size: int
    hit_rate_source: str
    hit_rate_bucket: str
    hit_rate_warning: str
    position_quality_score: float
    position_quality_grade: str
    capital_factor: float
    suggested_capital_pct: float
    capital_reason: str
    score: float
    edge_score: float
    reason: str
    buy_enabled: bool = True


@dataclass
class SellAdvice:
    ticker: str
    name: str
    action: str
    buy_date: str
    buy_price: float
    latest_price: float
    vwap: float
    target_price: float
    first_manage_price: float
    trailing_stop_price: float
    hard_stop_price: float
    vwap_fail_price: float
    highest_price: float
    pnl_pct: float
    management_state: str
    previous_management_state: str
    signal_points: str
    reason: str


MANAGEMENT_STATES = {"OPEN", "FIRST_MANAGE_HIT", "PROFIT_PROTECTED", "REDUCED", "EXITED"}
URGENT_SELL_ACTIONS = {"SELL_NOW", "TAKE_PROFIT", "REDUCE_PROFIT", "MANAGE_PROFIT", "TRAIL_SELL", "VWAP_WEAK_SELL", "PRE_CLOSE_REDUCE"}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local 2-minute A-share buy/sell assistant.")
    parser.add_argument("--watchlist", default="config/watchlist.mainboard_liquid.csv")
    parser.add_argument("--positions", default="config/live_positions.csv")
    parser.add_argument("--out-dir", default="output/trading_assistant")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--monitor-script", default="short_term_live_monitor.py")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--today", default="")
    parser.add_argument("--phase", choices=["auto", "opening", "intraday", "preclose", "postclose"], default="auto")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--intraday-interval-seconds", type=int, default=120)
    parser.add_argument("--focus-interval-seconds", type=int, default=300)
    parser.add_argument("--postclose-interval-seconds", type=int, default=900)
    parser.add_argument("--monitor-timeout-seconds", type=int, default=DEFAULT_MONITOR_TIMEOUT_SECONDS)
    parser.add_argument("--db", default="output/trading_assistant/trading_journal.sqlite")
    parser.add_argument("--app-db", default="")
    parser.add_argument("--use-app-db", action="store_true")
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--beep", action="store_true")
    parser.add_argument("--github-mode", choices=["none", "commit"], default="none")
    parser.add_argument("--git-pull-before-scan", action="store_true")
    parser.add_argument("--git-branch", default="")
    parser.add_argument("--extra-monitor-arg", action="append", default=[])
    return parser


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def parse_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        text = str(value).replace(",", "").replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def parse_optional_float(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        text = str(value).replace(",", "").replace("%", "").strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def format_rate_pct(value: float | None) -> str:
    return "样本不足" if value is None else f"{value:.1f}%"


def clean_text(value: object) -> str:
    return str(value or "").strip()


def first_manage_pct_from_target(target_pct: float) -> float:
    return max(4.0, target_pct * 0.4)


def first_manage_price_from_position(buy_price: float, target_price: float) -> float:
    if buy_price <= 0 or target_price <= buy_price:
        return 0.0
    target_pct = (target_price / buy_price - 1) * 100
    return buy_price * (1 + first_manage_pct_from_target(target_pct) / 100)


def sell_signal_points(
    target_price: float,
    first_manage_price: float,
    trailing_stop_price: float,
    hard_stop_price: float,
    vwap: float,
    vwap_fail_price: float,
) -> str:
    parts: list[str] = []
    if target_price > 0:
        parts.append(f"目标上沿{target_price:.2f}")
    if first_manage_price > 0:
        parts.append(f"第一管理线{first_manage_price:.2f}")
    if trailing_stop_price > 0:
        parts.append(f"移动止盈{trailing_stop_price:.2f}")
    if hard_stop_price > 0:
        parts.append(f"硬止损{hard_stop_price:.2f}")
    if vwap_fail_price > 0:
        parts.append(f"VWAP弱势<{vwap_fail_price:.2f}")
    if vwap > 0:
        parts.append(f"尾盘弱势14:45后<VWAP{vwap:.2f}且盈利<1.5%")
    return "；".join(parts)


def normalize_management_state(value: object, status: str = "open") -> str:
    if str(status or "").strip().lower() == "closed":
        return "EXITED"
    state = clean_text(value).upper() or "OPEN"
    return state if state in MANAGEMENT_STATES else "OPEN"


def signal_timestamp(today: dt.date, latest_time: str) -> str:
    return f"{today.isoformat()} {latest_time or dt.datetime.now().time().isoformat(timespec='minutes')}"


def transition_management_state(
    row: dict[str, str],
    action: str,
    first_manage_hit: bool,
    today: dt.date,
    latest_time: str,
) -> tuple[str, str]:
    previous = normalize_management_state(row.get("management_state"), row.get("status", "open"))
    state = previous
    timestamp = signal_timestamp(today, latest_time)
    if row.get("status", "open").lower() == "closed":
        state = "EXITED"
    elif previous == "REDUCED":
        state = "REDUCED"
    elif action in {"REDUCE_PROFIT", "TRAIL_SELL", "PRE_CLOSE_REDUCE", "VWAP_WEAK_SELL"} and first_manage_hit:
        state = "PROFIT_PROTECTED"
        if not row.get("profit_protected_at"):
            row["profit_protected_at"] = timestamp
    elif first_manage_hit and previous == "OPEN":
        state = "FIRST_MANAGE_HIT"
        if not row.get("first_manage_hit_at"):
            row["first_manage_hit_at"] = timestamp
    row["management_state"] = state
    row["last_signal_action"] = action
    row["last_signal_at"] = timestamp
    return previous, state


def target_context_note(
    first_manage_price: float,
    target_hit_rate: float | None,
    first_manage_hit_rate: float | None,
    buy_enabled: bool,
    target_touch_rate: float | None = None,
    sample_size: int = 0,
    source: str = "",
    bucket: str = "",
    warning: str = "",
) -> str:
    if first_manage_price <= 0:
        return ""
    if not buy_enabled:
        return "；观察池不按目标价交易"
    if sample_size <= 0 or target_hit_rate is None or target_touch_rate is None or first_manage_hit_rate is None:
        suffix = f"，原因={warning}" if warning else ""
        return f"；目标价是上沿，不是承诺价，先看{first_manage_price:.2f}管理线；历史相似样本不足，暂不展示命中率{suffix}"
    bucket_note = bucket or source or "overall"
    return f"；目标价是上沿，不是承诺价，先看{first_manage_price:.2f}管理线；12M真实样本(N={sample_size}, {bucket_note}) 可卖上沿{target_hit_rate:.1f}%/触及上沿{target_touch_rate:.1f}%/管理线{first_manage_hit_rate:.1f}%"


def phase_for_time(now: dt.datetime) -> str:
    if now.weekday() >= 5:
        return "closed"
    current = now.time()
    if dt.time(9, 20) <= current < dt.time(9, 45):
        return "opening"
    if dt.time(14, 45) <= current <= dt.time(15, 5):
        return "preclose"
    if dt.time(15, 5) < current <= dt.time(15, 30):
        return "postclose"
    if dt.time(9, 45) <= current <= dt.time(11, 30):
        return "intraday"
    if dt.time(13, 0) <= current < dt.time(14, 45):
        return "intraday"
    return "closed"


def next_sleep_seconds(phase: str, args: argparse.Namespace) -> int:
    if phase in {"opening", "preclose"}:
        return max(60, args.focus_interval_seconds)
    if phase == "intraday":
        return max(30, args.intraday_interval_seconds)
    if phase == "postclose":
        return max(300, args.postclose_interval_seconds)
    return 60


def run_command(command: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=check)


def run_command_with_heartbeat(
    command: list[str],
    cwd: Path,
    heartbeat_message: str,
    timeout_seconds: int = DEFAULT_MONITOR_TIMEOUT_SECONDS,
    heartbeat_seconds: int = 5,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    last_heartbeat = started
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def read_stream(name: str, stream: object) -> None:
        if stream is None:
            return
        for raw_line in stream:  # type: ignore[union-attr]
            output_queue.put((name, str(raw_line).rstrip()))

    stdout_thread = threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True)
    stderr_thread = threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def drain_output() -> None:
        while True:
            try:
                name, line = output_queue.get_nowait()
            except queue.Empty:
                return
            if name == "stdout":
                stdout_lines.append(line)
            else:
                stderr_lines.append(line)
            if line.startswith("MONITOR_PROGRESS|"):
                emit_progress(30, line.split("|", 1)[1])
            elif name == "stderr" and line.strip():
                emit_progress(30, f"候选股扫描错误输出：{line[:180]}")

    while process.poll() is None:
        drain_output()
        now = time.monotonic()
        elapsed = int(now - started)
        if elapsed >= timeout_seconds:
            process.kill()
            stdout, stderr = process.communicate()
            if stdout:
                stdout_lines.append(stdout)
            if stderr:
                stderr_lines.append(stderr)
            raise subprocess.TimeoutExpired(command, timeout_seconds, output=stdout, stderr=stderr)
        if now - last_heartbeat >= heartbeat_seconds:
            percent = min(54, 30 + int(elapsed / max(timeout_seconds, 1) * 24))
            emit_progress(percent, f"{heartbeat_message}，已运行 {elapsed} 秒（最多等待 {timeout_seconds} 秒）")
            last_heartbeat = now
        time.sleep(0.2)
    stdout, stderr = process.communicate()
    if stdout:
        stdout_lines.append(stdout)
    if stderr:
        stderr_lines.append(stderr)
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    drain_output()
    stdout = "\n".join(line for line in stdout_lines if line)
    stderr = "\n".join(line for line in stderr_lines if line)
    result = subprocess.CompletedProcess(command, int(process.returncode or 0), stdout, stderr)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, command, output=stdout, stderr=stderr)
    return result


def emit_progress(percent: int, message: str) -> None:
    print(f"SCAN_PROGRESS|{percent}|{message}", flush=True)


def run_monitor(args: argparse.Namespace, cwd: Path, today: dt.date, phase: str, out_dir: Path) -> tuple[Path, Path, str]:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report = out_dir / f"monitor_{phase}_{stamp}.md"
    csv_path = out_dir / f"monitor_{phase}_{stamp}.csv"
    mode = "daily" if phase in {"postclose", "closed"} or (phase == "opening" and dt.datetime.now().time() < dt.time(9, 30)) else "intraday"
    command = [
        args.python,
        args.monitor_script,
        "--watchlist",
        args.watchlist,
        "--mode",
        mode,
        "--top",
        str(args.top),
        "--today",
        today.isoformat(),
        "--out",
        str(report),
        "--csv-out",
        str(csv_path),
        *MONITOR_DEFAULT_ARGS,
        *args.extra_monitor_arg,
    ]
    emit_progress(25, "运行候选股扫描")
    result = run_command_with_heartbeat(command, cwd, "候选股扫描运行中", timeout_seconds=args.monitor_timeout_seconds)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    emit_progress(55, "候选股扫描完成")
    return report, csv_path, mode


def read_candidates(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_buy_advice(rows: list[dict[str, str]], phase: str) -> list[BuyAdvice]:
    advices: list[BuyAdvice] = []
    for row in rows:
        action = row.get("action", "")
        latest = parse_float(row.get("latest_price"))
        trigger = parse_float(row.get("entry_trigger"))
        vwap = parse_float(row.get("intraday_vwap"))
        close = parse_float(row.get("close"))
        target_pct = parse_float(row.get("target_pct"))
        stop_pct = parse_float(row.get("hard_stop_pct"))
        ref_price = latest if latest > 0 else close
        target_price = ref_price * (1 + target_pct / 100) if ref_price else 0.0
        first_manage_pct = parse_float(row.get("first_manage_pct"), first_manage_pct_from_target(target_pct))
        first_manage_price = ref_price * (1 + first_manage_pct / 100) if ref_price else 0.0
        target_upper_hit_rate = parse_optional_float(row.get("target_upper_hit_rate_pct"))
        target_upper_touch_rate = parse_optional_float(row.get("target_upper_touch_rate_pct"))
        first_manage_hit_rate = parse_optional_float(row.get("first_manage_hit_rate_pct"))
        hit_rate_sample_size = int(parse_float(row.get("hit_rate_sample_size"), 0.0))
        hit_rate_source = row.get("hit_rate_source", "") or "fallback"
        hit_rate_bucket = row.get("hit_rate_bucket", "") or ""
        hit_rate_warning = row.get("hit_rate_warning", "") or ""
        position_quality_score = parse_float(row.get("position_quality_score"), 0.0)
        position_quality_grade = row.get("position_quality_grade", "") or ""
        capital_factor = parse_float(row.get("capital_factor"), 1.0)
        suggested_capital_pct = min(100.0, max(0.0, parse_float(row.get("suggested_capital_pct"), 0.0)))
        capital_reason = row.get("capital_reason", "") or ""
        score = parse_float(row.get("score"), 0.0)
        hard_stop_price = ref_price * (1 - stop_pct / 100) if ref_price else 0.0
        if action == "DATA_UNAVAILABLE":
            priority = 9
            final_action = "DATA_UNAVAILABLE"
            buy_enabled = False
            ref_price = 0.0
            trigger = 0.0
            vwap = 0.0
            target_price = 0.0
            first_manage_price = 0.0
            hard_stop_price = 0.0
            reason = "盘中5分钟行情不可用，暂停买入判断"
        elif action == "QUOTE_ONLY":
            priority = 8
            final_action = "QUOTE_ONLY"
            buy_enabled = False
            vwap = 0.0
            reason = "仅有实时报价兜底，缺少5分钟线/VWAP确认；价位仅作参考，暂停买入判断"
        elif action == "BUY_TRIGGER":
            priority = 1
            final_action = "BUY_NOW"
            buy_enabled = True
            reason = "价格站上触发价和VWAP，且没有被高开/过热过滤拦截"
        elif action == "WATCH_SCORE_ONLY":
            priority = 4
            final_action = "WATCH_BUY"
            buy_enabled = False
            reason = "进入观察池，但低于买入分数线；仅跟踪，不触发买入"
        elif action == "OBSERVE_ONLY":
            priority = 4
            final_action = "WATCH_ONLY"
            buy_enabled = False
            reason = row.get("risks", "") or "扩展观察池，仅跟踪，不触发买入"
        elif action in {"WATCH", "WATCH_NEXT_SESSION"}:
            priority = 2 if phase in {"opening", "intraday"} else 4
            final_action = "WATCH_BUY"
            buy_enabled = True
            reason = "保留关注，等价格重新站上触发价/VWAP"
        elif action in {"WAIT_0945", "WAIT_SESSION"}:
            priority = 3
            final_action = "WAIT"
            buy_enabled = True
            reason = "还没到有效买入确认窗口或暂无日内数据"
        else:
            priority = 5
            final_action = "NO_BUY"
            buy_enabled = False
            reason = row.get("risks", "") or action or "未通过盘中执行过滤"
        if buy_enabled and suggested_capital_pct <= 0 and ("暂停新开仓" in capital_reason or row.get("market_state", "") == "hot"):
            buy_enabled = False
            reason = f"{reason}；资金风控暂停买入"
        if not buy_enabled:
            suggested_capital_pct = 0.0
        capital_note = (
            f"；建议资金占比{suggested_capital_pct:.1f}%，质量{position_quality_grade or '-'}({position_quality_score:.2f})，仓位因子{capital_factor:.2f}"
            if buy_enabled and suggested_capital_pct > 0
            else "；观察/数据不足/分数不足，不提示投入资金"
        )
        if capital_reason:
            capital_note += f"；仓位依据：{capital_reason}"
        reason = f"{reason}{target_context_note(first_manage_price, target_upper_hit_rate, first_manage_hit_rate, buy_enabled, target_upper_touch_rate, hit_rate_sample_size, hit_rate_source, hit_rate_bucket, hit_rate_warning)}{capital_note}"
        advices.append(
            BuyAdvice(
                ticker=row.get("ticker", ""),
                name=row.get("name", ""),
                action=final_action,
                priority=priority,
                latest_price=round(ref_price, 4),
                trigger_price=round(trigger, 4),
                vwap=round(vwap, 4),
                target_price=round(target_price, 4),
                first_manage_price=round(first_manage_price, 4),
                hard_stop_price=round(hard_stop_price, 4),
                target_pct=target_pct,
                first_manage_pct=first_manage_pct,
                hard_stop_pct=stop_pct,
                target_upper_hit_rate_pct=target_upper_hit_rate,
                target_upper_touch_rate_pct=target_upper_touch_rate,
                first_manage_hit_rate_pct=first_manage_hit_rate,
                hit_rate_sample_size=hit_rate_sample_size,
                hit_rate_source=hit_rate_source,
                hit_rate_bucket=hit_rate_bucket,
                hit_rate_warning=hit_rate_warning,
                position_quality_score=position_quality_score,
                position_quality_grade=position_quality_grade,
                capital_factor=capital_factor,
                suggested_capital_pct=suggested_capital_pct,
                capital_reason=capital_reason,
                score=score,
                edge_score=parse_float(row.get("edge_score")),
                reason=reason,
                buy_enabled=buy_enabled,
            )
        )
    normalize_buy_advice_capital(advices)
    return sorted(
        advices,
        key=lambda item: (
            item.priority,
            -item.position_quality_score,
            -item.score,
            -item.suggested_capital_pct,
            -item.edge_score,
        ),
    )


def normalize_buy_advice_capital(advices: list[BuyAdvice]) -> None:
    eligible = [
        item
        for item in advices
        if item.buy_enabled and item.suggested_capital_pct > 0 and item.action not in {"DATA_UNAVAILABLE", "QUOTE_ONLY"}
    ]
    for item in eligible:
        item.suggested_capital_pct = min(100.0, max(0.0, item.suggested_capital_pct))
    total = sum(item.suggested_capital_pct for item in eligible)
    if total <= 100.0:
        return
    scale = 100.0 / total
    for item in eligible:
        before = item.suggested_capital_pct
        item.suggested_capital_pct = round(before * scale, 2)
        item.reason = f"{item.reason}；组合预算归一化：{before:.1f}%→{item.suggested_capital_pct:.1f}%"


def load_positions(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("status", "open").lower() == "open"]


def write_positions(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def quote_time_text(timestamp: str) -> str:
    parts = str(timestamp or "").split()
    if len(parts) >= 2:
        return parts[-1][:5]
    return str(timestamp or "")[:5]


def latest_vwap_for_position(
    client: BaoStock5mClient,
    ticker: str,
    today: dt.date,
    quote_session: requests.Session | None = None,
) -> tuple[float, float, float, str]:
    bars = client.fetch_5m(ticker, today, today)
    bars = [bar for bar in bars if dt.time(9, 30) <= bar.time <= dt.time(15, 0)]
    if not bars:
        if quote_session is not None and today == dt.date.today():
            quote = fetch_sina_quote(quote_session, ticker)
            if quote:
                return quote.price, 0.0, quote.price, quote_time_text(quote.timestamp)
        return 0.0, 0.0, 0.0, ""
    amount = sum(bar.amount for bar in bars)
    volume = sum(bar.volume for bar in bars)
    vwap = amount / volume if volume > 0 else bars[-1].close
    latest = bars[-1].close
    high = max(bar.high for bar in bars)
    return latest, vwap, high, bars[-1].time.isoformat(timespec="minutes")


def build_sell_advice(
    positions: list[dict[str, str]],
    today: dt.date,
    positions_path: Path,
    *,
    write_back: bool = True,
) -> list[SellAdvice]:
    advices: list[SellAdvice] = []
    if not positions:
        return advices
    changed = False
    with requests.Session() as quote_session, BaoStock5mClient() as client:
        for row in positions:
            ticker = row.get("ticker", "")
            if not ticker:
                continue
            buy_date = row.get("buy_date", "")
            buy_price = parse_float(row.get("buy_price"))
            target_price = parse_float(row.get("target_price"))
            first_manage_price = parse_float(row.get("first_manage_price"))
            hard_stop_price = parse_float(row.get("hard_stop_price"))
            trailing_stop_pct = parse_float(row.get("trailing_stop_pct"), 3.0)
            previous_highest = parse_float(row.get("highest_price"), buy_price)
            current_state = normalize_management_state(row.get("management_state"), row.get("status", "open"))
            if first_manage_price <= 0:
                first_manage_price = first_manage_price_from_position(buy_price, target_price)
            latest, vwap, intraday_high, latest_time = latest_vwap_for_position(client, ticker, today, quote_session)
            quote_only = latest > 0 and vwap <= 0
            vwap_fail_price = min(vwap, buy_price) if vwap > 0 and buy_price > 0 else 0.0
            if latest <= 0:
                trailing_stop_price = previous_highest * (1 - trailing_stop_pct / 100) if previous_highest > buy_price else 0.0
                points = sell_signal_points(target_price, first_manage_price, trailing_stop_price, hard_stop_price, vwap, vwap_fail_price)
                old_state = row.get("management_state", "")
                old_signal_action = row.get("last_signal_action", "")
                old_signal_at = row.get("last_signal_at", "")
                previous_state, new_state = transition_management_state(row, "HOLD_NO_INTRADAY", False, today, latest_time)
                if (
                    row.get("management_state", "") != old_state
                    or row.get("last_signal_action", "") != old_signal_action
                    or row.get("last_signal_at", "") != old_signal_at
                ):
                    changed = True
                advices.append(
                    SellAdvice(ticker, row.get("name", ""), "HOLD_NO_INTRADAY", buy_date, buy_price, 0.0, 0.0, target_price, first_manage_price, trailing_stop_price, hard_stop_price, vwap_fail_price, previous_highest, 0.0, new_state, previous_state, points, "暂无5分钟线，不能确认卖点")
                )
                continue
            highest = max(previous_highest, intraday_high, latest)
            if highest != previous_highest:
                row["highest_price"] = f"{highest:.4f}"
                changed = True
            lines = calculate_holding_management_lines(
                cost_price=buy_price,
                latest_price=latest,
                previous_target_price=target_price,
                previous_hard_stop_price=hard_stop_price,
                previous_highest_price=highest,
                trailing_stop_pct=trailing_stop_pct,
                previous_management_state=current_state,
                first_manage_hit_at=row.get("first_manage_hit_at", ""),
                profit_protected_at=row.get("profit_protected_at", ""),
                source_notes=row.get("notes", ""),
                timestamp=f"{today.isoformat()}T{latest_time or '00:00:00'}",
            )
            if target_price != lines.target_price:
                row["target_price"] = f"{lines.target_price:.4f}"
                target_price = lines.target_price
                changed = True
            if hard_stop_price != lines.hard_stop_price:
                row["hard_stop_price"] = f"{lines.hard_stop_price:.4f}"
                hard_stop_price = lines.hard_stop_price
                changed = True
            if trailing_stop_pct != lines.trailing_stop_pct:
                row["trailing_stop_pct"] = f"{lines.trailing_stop_pct:.4f}"
                trailing_stop_pct = lines.trailing_stop_pct
                changed = True
            if highest != lines.highest_price:
                row["highest_price"] = f"{lines.highest_price:.4f}"
                highest = lines.highest_price
                changed = True
            if row.get("management_state", "") != lines.management_state:
                row["management_state"] = lines.management_state
                current_state = lines.management_state
                changed = True
            if row.get("first_manage_hit_at", "") != lines.first_manage_hit_at:
                row["first_manage_hit_at"] = lines.first_manage_hit_at
                changed = True
            if row.get("profit_protected_at", "") != lines.profit_protected_at:
                row["profit_protected_at"] = lines.profit_protected_at
                changed = True
            first_manage_price = first_manage_price_from_position(buy_price, target_price)
            protection = calculate_profit_protection_result(
                cost_price=buy_price,
                latest_price=latest,
                target_price=target_price,
                hard_stop_price=hard_stop_price,
                trailing_stop_pct=trailing_stop_pct,
                highest_price=highest,
                management_state=current_state,
            )
            trailing_stop_price = protection.trailing_stop_price
            pnl_pct = (latest / buy_price - 1) * 100 if buy_price else 0.0
            same_day = buy_date == today.isoformat()
            first_manage_hit = first_manage_price > 0 and highest >= first_manage_price
            points = sell_signal_points(target_price, first_manage_price, trailing_stop_price, hard_stop_price, vwap, vwap_fail_price)
            if same_day:
                action = "HOLD_T1"
                reason = "A股T+1，今天买入的仓位今天不能卖"
                if first_manage_hit:
                    reason += f"；已触及第一管理线 {first_manage_price:.2f}，明日优先保护利润"
            elif hard_stop_price > 0 and latest <= hard_stop_price:
                if first_manage_hit or current_state in {"FIRST_MANAGE_HIT", "PROFIT_PROTECTED", "REDUCED"}:
                    action = "REDUCE_PROFIT"
                    reason = protection.summary
                else:
                    action = "SELL_NOW"
                    reason = f"跌破硬止损 {hard_stop_price:.2f}"
            elif target_price > 0 and latest >= target_price:
                if first_manage_hit or current_state in {"FIRST_MANAGE_HIT", "PROFIT_PROTECTED", "REDUCED"}:
                    action = "MANAGE_PROFIT"
                    reason = protection.summary
                else:
                    action = "TAKE_PROFIT"
                    reason = f"达到目标上沿 {target_price:.2f}"
            elif first_manage_hit and vwap > 0 and latest < vwap and latest_time >= "09:45":
                if clean_text(row.get("last_signal_action")) == "VWAP_WEAK_CONFIRM":
                    action = "REDUCE_PROFIT"
                    reason = f"已触及第一管理线 {first_manage_price:.2f}，连续两次弱于VWAP {vwap:.2f}，提示保护利润/减仓"
                else:
                    action = "VWAP_WEAK_CONFIRM"
                    reason = f"已触及第一管理线 {first_manage_price:.2f}，首次跌回VWAP {vwap:.2f} 下方，先确认一轮；若下一次扫描仍弱再减仓"
            elif first_manage_hit and trailing_stop_price > 0 and latest <= trailing_stop_price:
                action = "REDUCE_PROFIT"
                reason = protection.summary
            elif first_manage_price > 0 and latest >= first_manage_price:
                if current_state == "OPEN":
                    action = "MANAGE_PROFIT"
                    reason = f"达到第一管理线 {first_manage_price:.2f}，不强制卖出；建议上移止损、盯VWAP，允许手动减仓"
                else:
                    action = "HOLD_MANAGED"
                    reason = protection.summary
            elif trailing_stop_price > 0 and latest <= trailing_stop_price:
                action = "TRAIL_SELL"
                reason = f"从日内/持仓高点 {highest:.2f} 回落超过 {trailing_stop_pct:.1f}%"
            elif latest_time >= "14:45" and latest < vwap and pnl_pct < 1.5:
                action = "PRE_CLOSE_REDUCE"
                reason = "收盘前低于VWAP且利润不足，降低隔夜风险"
            elif latest < vwap and latest < buy_price and latest_time >= "09:45":
                action = "VWAP_WEAK_SELL"
                reason = "可卖日跌破VWAP且低于成本"
            else:
                action = "HOLD"
                reason = "未触发止盈、止损、VWAP弱势或收盘前减仓条件"
            if quote_only:
                reason += "；实时价兜底，暂无5分钟VWAP，VWAP弱势条件本轮不判定"
            old_state = row.get("management_state", "")
            old_signal_action = row.get("last_signal_action", "")
            old_signal_at = row.get("last_signal_at", "")
            previous_state, new_state = transition_management_state(row, action, first_manage_hit, today, latest_time)
            if (
                row.get("management_state", "") != old_state
                or row.get("last_signal_action", "") != old_signal_action
                or row.get("last_signal_at", "") != old_signal_at
            ):
                changed = True
            advices.append(
                SellAdvice(
                    ticker=ticker,
                    name=row.get("name", ""),
                    action=action,
                    buy_date=buy_date,
                    buy_price=round(buy_price, 4),
                    latest_price=round(latest, 4),
                    vwap=round(vwap, 4),
                    target_price=round(target_price, 4),
                    first_manage_price=round(first_manage_price, 4),
                    trailing_stop_price=round(trailing_stop_price, 4),
                    hard_stop_price=round(hard_stop_price, 4),
                    vwap_fail_price=round(vwap_fail_price, 4),
                    highest_price=round(highest, 4),
                    pnl_pct=round(pnl_pct, 2),
                    management_state=new_state,
                    previous_management_state=previous_state,
                    signal_points=points,
                    reason=reason,
                )
            )
    if changed and write_back:
        write_positions(positions_path, positions)
    return sorted(advices, key=lambda item: item.action not in URGENT_SELL_ACTIONS)


def write_reports(out_dir: Path, today: dt.date, phase: str, mode: str, buy_advices: list[BuyAdvice], sell_advices: list[SellAdvice], monitor_report: Path, monitor_csv: Path) -> tuple[Path, Path, Path]:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report = out_dir / f"trade_plan_{phase}_{stamp}.md"
    json_path = out_dir / f"trade_plan_{phase}_{stamp}.json"
    csv_path = out_dir / f"trade_plan_{phase}_{stamp}.csv"
    out_dir.mkdir(parents=True, exist_ok=True)

    urgent = [item for item in sell_advices if item.action in URGENT_SELL_ACTIONS]
    buys = [item for item in buy_advices if item.action == "BUY_NOW"]
    lines = [
        f"# 本地短线交易建议 - {today.isoformat()} {dt.datetime.now():%H:%M:%S}",
        "",
        f"- 阶段: `{phase}`",
        f"- 选股模式: `{mode}`",
        f"- 急需处理卖出: `{len(urgent)}`",
        f"- 当前买入触发: `{len(buys)}`",
        f"- 原始选股报告: `{monitor_report}`",
        "",
        "## 先看卖出/持仓",
        "",
        "| 动作 | 状态 | 代码 | 名称 | 成本 | 最新 | VWAP | 盈亏 | 目标上沿 | 第一管理线 | 移动止盈 | 硬止损 | VWAP弱势 | 理由 |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    if sell_advices:
        for item in sell_advices:
            lines.append(f"| {item.action} | {item.management_state} | {item.ticker} | {item.name} | {item.buy_price:.2f} | {item.latest_price:.2f} | {item.vwap:.2f} | {item.pnl_pct:.2f}% | {item.target_price:.2f} | {item.first_manage_price:.2f} | {item.trailing_stop_price:.2f} | {item.hard_stop_price:.2f} | {item.vwap_fail_price:.2f} | {item.reason} |")
    else:
        lines.append("| - | - | 当前没有登记持仓 | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 再看买入",
            "",
            "| 动作 | 代码 | 名称 | 最新/参考 | 触发价 | VWAP | 建议资金 | 质量 | 分数 | 目标上沿 | 第一管理线 | 止损价 | 可卖上沿 | 触及上沿 | 管理线 | N | 样本桶 | Edge | 理由 |",
            "|---|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|",
        ]
    )
    for item in buy_advices[:20]:
        quality_text = f"{item.position_quality_grade or '-'}/{item.position_quality_score:.2f}"
        lines.append(f"| {item.action} | {item.ticker} | {item.name} | {item.latest_price:.2f} | {item.trigger_price:.2f} | {item.vwap:.2f} | {item.suggested_capital_pct:.1f}% | {quality_text} | {item.score:.1f} | {item.target_price:.2f} | {item.first_manage_price:.2f} | {item.hard_stop_price:.2f} | {format_rate_pct(item.target_upper_hit_rate_pct)} | {format_rate_pct(item.target_upper_touch_rate_pct)} | {format_rate_pct(item.first_manage_hit_rate_pct)} | {item.hit_rate_sample_size} | {item.hit_rate_bucket or '-'} | {item.edge_score:.2f} | {item.reason} |")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "date": today.isoformat(),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "phase": phase,
        "mode": mode,
        "monitor_report": str(monitor_report),
        "monitor_csv": str(monitor_csv),
        "buy": [asdict(item) for item in buy_advices],
        "sell": [asdict(item) for item in sell_advices],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = ["side", "action", "ticker", "name", "latest_price", "trigger_or_cost", "suggested_capital_pct", "position_quality_score", "position_quality_grade", "score", "edge_score", "capital_factor", "capital_reason", "target_price", "first_manage_price", "trailing_stop_price", "hard_stop_price", "vwap_fail_price", "management_state", "previous_management_state", "target_upper_hit_rate_pct", "target_upper_touch_rate_pct", "first_manage_hit_rate_pct", "hit_rate_sample_size", "hit_rate_source", "hit_rate_bucket", "hit_rate_warning", "pnl_pct", "signal_points", "reason"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in sell_advices:
            writer.writerow({"side": "sell", "action": item.action, "ticker": item.ticker, "name": item.name, "latest_price": item.latest_price, "trigger_or_cost": item.buy_price, "suggested_capital_pct": "", "position_quality_score": "", "position_quality_grade": "", "score": "", "edge_score": "", "capital_factor": "", "capital_reason": "", "target_price": item.target_price, "first_manage_price": item.first_manage_price, "trailing_stop_price": item.trailing_stop_price, "hard_stop_price": item.hard_stop_price, "vwap_fail_price": item.vwap_fail_price, "management_state": item.management_state, "previous_management_state": item.previous_management_state, "target_upper_hit_rate_pct": "", "target_upper_touch_rate_pct": "", "first_manage_hit_rate_pct": "", "hit_rate_sample_size": "", "hit_rate_source": "", "hit_rate_bucket": "", "hit_rate_warning": "", "pnl_pct": item.pnl_pct, "signal_points": item.signal_points, "reason": item.reason})
        for item in buy_advices:
            writer.writerow({"side": "buy", "action": item.action, "ticker": item.ticker, "name": item.name, "latest_price": item.latest_price, "trigger_or_cost": item.trigger_price, "suggested_capital_pct": item.suggested_capital_pct, "position_quality_score": item.position_quality_score, "position_quality_grade": item.position_quality_grade, "score": item.score, "edge_score": item.edge_score, "capital_factor": item.capital_factor, "capital_reason": item.capital_reason, "target_price": item.target_price, "first_manage_price": item.first_manage_price, "trailing_stop_price": "", "hard_stop_price": item.hard_stop_price, "vwap_fail_price": "", "management_state": "", "previous_management_state": "", "target_upper_hit_rate_pct": item.target_upper_hit_rate_pct, "target_upper_touch_rate_pct": item.target_upper_touch_rate_pct, "first_manage_hit_rate_pct": item.first_manage_hit_rate_pct, "hit_rate_sample_size": item.hit_rate_sample_size, "hit_rate_source": item.hit_rate_source, "hit_rate_bucket": item.hit_rate_bucket, "hit_rate_warning": item.hit_rate_warning, "pnl_pct": "", "signal_points": "", "reason": item.reason})

    shutil.copyfile(report, out_dir / "latest_plan.md")
    shutil.copyfile(json_path, out_dir / "latest_plan.json")
    shutil.copyfile(csv_path, out_dir / "latest_plan.csv")
    return report, json_path, csv_path


def payload_from_plan(json_path: Path) -> dict[str, object]:
    if not json_path.exists():
        return {}
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def beep_if_needed(args: argparse.Namespace, buy_advices: list[BuyAdvice], sell_advices: list[SellAdvice]) -> None:
    if not args.beep:
        return
    urgent_sell = any(item.action in URGENT_SELL_ACTIONS for item in sell_advices)
    urgent_buy = any(item.action == "BUY_NOW" for item in buy_advices)
    if not urgent_sell and not urgent_buy:
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        print("\a", end="")


def git_publish(cwd: Path, args: argparse.Namespace, files: list[Path]) -> None:
    if args.github_mode != "commit":
        return
    if args.git_branch:
        run_command(["git", "checkout", args.git_branch], cwd)
    rel_files: list[str] = []
    for path in files:
        if not path.exists():
            continue
        try:
            rel_files.append(str(path.resolve().relative_to(cwd.resolve())))
        except ValueError:
            continue
    if not rel_files:
        return
    run_command(["git", "add", *rel_files], cwd)
    status = run_command(["git", "status", "--porcelain", "--", *rel_files], cwd, check=False)
    if not status.stdout.strip():
        return
    run_command(["git", "commit", "-m", f"trading plan {dt.datetime.now():%Y-%m-%d %H:%M}"], cwd)
    run_command(["git", "push"], cwd)


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def ensure_writable_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    probe = Path(tempfile.mktemp(prefix=".write_probe_", dir=str(path)))
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
    return path


def fallback_output_dir() -> Path:
    return (default_db_path().parent / "output" / "trading_assistant").resolve()


def prepare_output_dir(preferred: Path) -> Path:
    try:
        return ensure_writable_dir(preferred)
    except OSError as exc:
        fallback = fallback_output_dir()
        emit_progress(11, f"输出目录不可写，切换到本地数据目录：{fallback}；原错误：{exc}")
        return ensure_writable_dir(fallback)


def run_once(args: argparse.Namespace, cwd: Path) -> tuple[Path, Path, Path]:
    emit_progress(10, "开始准备扫描")
    now = dt.datetime.now()
    today = parse_date(args.today) if args.today else now.date()
    phase = phase_for_time(now) if args.phase == "auto" else args.phase
    if phase == "closed":
        phase = "postclose" if args.once else "closed"
    out_dir = resolve_path(cwd, args.out_dir)
    emit_progress(11, "准备输出目录")
    out_dir = prepare_output_dir(out_dir)
    app_db_path = resolve_path(cwd, args.app_db) if args.app_db else default_db_path()
    positions_path = resolve_path(cwd, args.positions)
    if args.use_app_db:
        emit_progress(12, "读取本地持仓数据")
        positions_path = out_dir / "runtime_positions.csv"
        with connect_app_storage(app_db_path) as conn:
            export_open_positions_csv(conn, positions_path)
        emit_progress(13, "本地持仓数据已准备")
    emit_progress(20, f"准备 {phase} 扫描")
    if args.git_pull_before_scan:
        emit_progress(21, "同步 Git 最新结果")
        run_command(["git", "pull", "--ff-only"], cwd, check=False)
    monitor_report, monitor_csv, mode = run_monitor(args, cwd, today, phase, out_dir)
    emit_progress(62, "读取候选股结果")
    candidates = read_candidates(monitor_csv)
    emit_progress(70, "生成买入建议")
    buy_advices = build_buy_advice(candidates, phase)
    emit_progress(78, "读取持仓并生成卖出建议")
    positions = load_positions(positions_path)
    sell_advices = build_sell_advice(positions, today, positions_path)
    if args.use_app_db:
        with connect_app_storage(app_db_path) as conn:
            update_positions_from_csv(conn, positions_path)
    emit_progress(88, "写入扫描报告")
    report, json_path, csv_path = write_reports(out_dir, today, phase, mode, buy_advices, sell_advices, monitor_report, monitor_csv)
    db_path = (cwd / args.db).resolve()
    if not args.no_db:
        emit_progress(93, "记录交易日志")
        payload = payload_from_plan(json_path)
        if payload:
            if args.use_app_db:
                with connect_app_storage(app_db_path) as conn:
                    save_latest_snapshot(conn, payload)
            run_id = record_assistant_run(db_path, payload, report, json_path, csv_path)
            if phase == "postclose":
                archive_trading_day(db_path, today, out_dir, notes="postclose archive")
            print(f"journal_db={db_path} run_id={run_id}")
    beep_if_needed(args, buy_advices, sell_advices)
    emit_progress(96, "发布或保存最新结果")
    git_publish(cwd, args, [report, json_path, csv_path, out_dir / "latest_plan.md", out_dir / "latest_plan.json", out_dir / "latest_plan.csv"])
    print(f"phase={phase} buy_now={sum(1 for item in buy_advices if item.action == 'BUY_NOW')} sell_actions={sum(1 for item in sell_advices if item.action != 'HOLD')} report={report}")
    emit_progress(100, "扫描完成")
    return report, json_path, csv_path


def main() -> int:
    args = build_arg_parser().parse_args()
    cwd = Path.cwd().resolve()
    while True:
        now = dt.datetime.now()
        phase = phase_for_time(now) if args.phase == "auto" else args.phase
        if now.weekday() >= 5 and not args.once:
            print(f"{now:%Y-%m-%d %H:%M:%S} weekend; sleeping")
        elif phase == "closed" and not args.once:
            print(f"{now:%Y-%m-%d %H:%M:%S} outside trading assistant windows; sleeping")
        else:
            try:
                run_once(args, cwd)
            except subprocess.CalledProcessError as exc:
                print(f"command failed: {' '.join(exc.cmd)}", file=sys.stderr)
                print(exc.stdout, file=sys.stderr)
                print(exc.stderr, file=sys.stderr)
            except Exception as exc:
                print(f"assistant scan failed: {exc}", file=sys.stderr)
        if args.once:
            break
        time.sleep(next_sleep_seconds(phase, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
