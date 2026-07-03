#!/usr/bin/env python3
"""Local A-share trading assistant with scheduled focus, buy, and sell advice."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from dependency_bootstrap import ensure_project_dependencies

ensure_project_dependencies()

from baostock_intraday import BaoStock5mClient
from trading_journal import archive_trading_day, record_assistant_run
from app_storage import connect as connect_app_storage
from app_storage import default_db_path, export_open_positions_csv, save_latest_snapshot, update_positions_from_csv


MONITOR_DEFAULT_ARGS = [
    "--dynamic-params",
    "--max-gap-up",
    "0.02",
    "--gap-volume-threshold",
    "0",
    "--gap-volume-min-ratio",
    "1.3",
    "--max-5d-range-pct",
    "32",
    "--max-momentum-10d-pct",
    "26",
    "--max-close-position-20d-pct",
    "85",
    "--cold-max-gap-up",
    "-1",
    "--cold-gap-volume-min-ratio",
    "99",
    "--cold-max-5d-range-pct",
    "1",
    "--cold-max-momentum-10d-pct",
    "1",
    "--cold-max-close-position-20d-pct",
    "1",
]


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
    hard_stop_price: float
    target_pct: float
    hard_stop_pct: float
    edge_score: float
    reason: str


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
    hard_stop_price: float
    highest_price: float
    pnl_pct: float
    reason: str


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local 2-minute A-share buy/sell assistant.")
    parser.add_argument("--watchlist", default="config/watchlist.buyable_600_300_301_liquid.csv")
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
        return float(value)
    except (TypeError, ValueError):
        return default


def phase_for_time(now: dt.datetime) -> str:
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
    timeout_seconds: int = 300,
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
    )
    while process.poll() is None:
        now = time.monotonic()
        elapsed = int(now - started)
        if elapsed >= timeout_seconds:
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout_seconds, output=stdout, stderr=stderr)
        if now - last_heartbeat >= heartbeat_seconds:
            percent = min(54, 30 + int(elapsed / max(timeout_seconds, 1) * 24))
            emit_progress(percent, f"{heartbeat_message}，已运行 {elapsed} 秒（最多等待 {timeout_seconds} 秒）")
            last_heartbeat = now
        time.sleep(0.2)
    stdout, stderr = process.communicate()
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
    mode = "daily" if phase == "opening" and dt.datetime.now().time() < dt.time(9, 30) else "intraday"
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
    result = run_command_with_heartbeat(command, cwd, "候选股扫描运行中")
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
        hard_stop_price = ref_price * (1 - stop_pct / 100) if ref_price else 0.0
        if action == "DATA_UNAVAILABLE":
            priority = 9
            final_action = "DATA_UNAVAILABLE"
            ref_price = 0.0
            trigger = 0.0
            vwap = 0.0
            target_price = 0.0
            hard_stop_price = 0.0
            reason = "盘中5分钟行情不可用，暂停买入判断"
        elif action == "QUOTE_ONLY":
            priority = 8
            final_action = "QUOTE_ONLY"
            trigger = 0.0
            vwap = 0.0
            target_price = 0.0
            hard_stop_price = 0.0
            reason = "仅有实时报价兜底，缺少5分钟线/VWAP确认，暂停买入判断"
        elif action == "BUY_TRIGGER":
            priority = 1
            final_action = "BUY_NOW"
            reason = "价格站上触发价和VWAP，且没有被高开/过热过滤拦截"
        elif action in {"WATCH", "WATCH_NEXT_SESSION"}:
            priority = 2 if phase in {"opening", "intraday"} else 4
            final_action = "WATCH_BUY"
            reason = "保留关注，等价格重新站上触发价/VWAP"
        elif action in {"WAIT_0945", "WAIT_SESSION"}:
            priority = 3
            final_action = "WAIT"
            reason = "还没到有效买入确认窗口或暂无日内数据"
        else:
            priority = 5
            final_action = "NO_BUY"
            reason = row.get("risks", "") or action or "未通过盘中执行过滤"
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
                hard_stop_price=round(hard_stop_price, 4),
                target_pct=target_pct,
                hard_stop_pct=stop_pct,
                edge_score=parse_float(row.get("edge_score")),
                reason=reason,
            )
        )
    return sorted(advices, key=lambda item: (item.priority, -item.edge_score))


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


def latest_vwap_for_position(client: BaoStock5mClient, ticker: str, today: dt.date) -> tuple[float, float, float, str]:
    bars = client.fetch_5m(ticker, today, today)
    bars = [bar for bar in bars if dt.time(9, 30) <= bar.time <= dt.time(15, 0)]
    if not bars:
        return 0.0, 0.0, 0.0, ""
    amount = sum(bar.amount for bar in bars)
    volume = sum(bar.volume for bar in bars)
    vwap = amount / volume if volume > 0 else bars[-1].close
    latest = bars[-1].close
    high = max(bar.high for bar in bars)
    return latest, vwap, high, bars[-1].time.isoformat(timespec="minutes")


def build_sell_advice(positions: list[dict[str, str]], today: dt.date, positions_path: Path) -> list[SellAdvice]:
    advices: list[SellAdvice] = []
    if not positions:
        return advices
    changed = False
    with BaoStock5mClient() as client:
        for row in positions:
            ticker = row.get("ticker", "")
            if not ticker:
                continue
            buy_date = row.get("buy_date", "")
            buy_price = parse_float(row.get("buy_price"))
            target_price = parse_float(row.get("target_price"))
            hard_stop_price = parse_float(row.get("hard_stop_price"))
            trailing_stop_pct = parse_float(row.get("trailing_stop_pct"), 3.0)
            previous_highest = parse_float(row.get("highest_price"), buy_price)
            latest, vwap, intraday_high, latest_time = latest_vwap_for_position(client, ticker, today)
            if latest <= 0:
                advices.append(
                    SellAdvice(ticker, row.get("name", ""), "HOLD_NO_INTRADAY", buy_date, buy_price, 0.0, 0.0, target_price, hard_stop_price, previous_highest, 0.0, "暂无5分钟线，不能确认卖点")
                )
                continue
            highest = max(previous_highest, intraday_high, latest)
            if highest != previous_highest:
                row["highest_price"] = f"{highest:.4f}"
                changed = True
            pnl_pct = (latest / buy_price - 1) * 100 if buy_price else 0.0
            same_day = buy_date == today.isoformat()
            if same_day:
                action = "HOLD_T1"
                reason = "A股T+1，今天买入的仓位今天不能卖"
            elif hard_stop_price > 0 and latest <= hard_stop_price:
                action = "SELL_NOW"
                reason = f"跌破硬止损 {hard_stop_price:.2f}"
            elif target_price > 0 and latest >= target_price:
                action = "TAKE_PROFIT"
                reason = f"达到目标价 {target_price:.2f}"
            elif highest > buy_price and latest <= highest * (1 - trailing_stop_pct / 100):
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
                    hard_stop_price=round(hard_stop_price, 4),
                    highest_price=round(highest, 4),
                    pnl_pct=round(pnl_pct, 2),
                    reason=reason,
                )
            )
    if changed:
        write_positions(positions_path, positions)
    return sorted(advices, key=lambda item: item.action not in {"SELL_NOW", "TAKE_PROFIT", "TRAIL_SELL", "VWAP_WEAK_SELL", "PRE_CLOSE_REDUCE"})


def write_reports(out_dir: Path, today: dt.date, phase: str, mode: str, buy_advices: list[BuyAdvice], sell_advices: list[SellAdvice], monitor_report: Path, monitor_csv: Path) -> tuple[Path, Path, Path]:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report = out_dir / f"trade_plan_{phase}_{stamp}.md"
    json_path = out_dir / f"trade_plan_{phase}_{stamp}.json"
    csv_path = out_dir / f"trade_plan_{phase}_{stamp}.csv"
    out_dir.mkdir(parents=True, exist_ok=True)

    urgent = [item for item in sell_advices if item.action in {"SELL_NOW", "TAKE_PROFIT", "TRAIL_SELL", "VWAP_WEAK_SELL", "PRE_CLOSE_REDUCE"}]
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
        "| 动作 | 代码 | 名称 | 成本 | 最新 | VWAP | 盈亏 | 目标 | 止损 | 理由 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    if sell_advices:
        for item in sell_advices:
            lines.append(f"| {item.action} | {item.ticker} | {item.name} | {item.buy_price:.2f} | {item.latest_price:.2f} | {item.vwap:.2f} | {item.pnl_pct:.2f}% | {item.target_price:.2f} | {item.hard_stop_price:.2f} | {item.reason} |")
    else:
        lines.append("| - | - | 当前没有登记持仓 | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 再看买入",
            "",
            "| 动作 | 代码 | 名称 | 最新/参考 | 触发价 | VWAP | 目标价 | 止损价 | Edge | 理由 |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in buy_advices[:20]:
        lines.append(f"| {item.action} | {item.ticker} | {item.name} | {item.latest_price:.2f} | {item.trigger_price:.2f} | {item.vwap:.2f} | {item.target_price:.2f} | {item.hard_stop_price:.2f} | {item.edge_score:.2f} | {item.reason} |")
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
        fieldnames = ["side", "action", "ticker", "name", "latest_price", "trigger_or_cost", "target_price", "hard_stop_price", "pnl_pct", "reason"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in sell_advices:
            writer.writerow({"side": "sell", "action": item.action, "ticker": item.ticker, "name": item.name, "latest_price": item.latest_price, "trigger_or_cost": item.buy_price, "target_price": item.target_price, "hard_stop_price": item.hard_stop_price, "pnl_pct": item.pnl_pct, "reason": item.reason})
        for item in buy_advices:
            writer.writerow({"side": "buy", "action": item.action, "ticker": item.ticker, "name": item.name, "latest_price": item.latest_price, "trigger_or_cost": item.trigger_price, "target_price": item.target_price, "hard_stop_price": item.hard_stop_price, "pnl_pct": "", "reason": item.reason})

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
    urgent_sell = any(item.action in {"SELL_NOW", "TAKE_PROFIT", "TRAIL_SELL", "VWAP_WEAK_SELL", "PRE_CLOSE_REDUCE"} for item in sell_advices)
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
    rel_files = [str(path.relative_to(cwd)) for path in files if path.exists()]
    if not rel_files:
        return
    run_command(["git", "add", *rel_files], cwd)
    status = run_command(["git", "status", "--porcelain", "--", *rel_files], cwd, check=False)
    if not status.stdout.strip():
        return
    run_command(["git", "commit", "-m", f"trading plan {dt.datetime.now():%Y-%m-%d %H:%M}"], cwd)
    run_command(["git", "push"], cwd)


def run_once(args: argparse.Namespace, cwd: Path) -> tuple[Path, Path, Path]:
    emit_progress(10, "开始准备扫描")
    now = dt.datetime.now()
    today = parse_date(args.today) if args.today else now.date()
    phase = phase_for_time(now) if args.phase == "auto" else args.phase
    if phase == "closed":
        phase = "postclose" if args.once else "closed"
    out_dir = (cwd / args.out_dir).resolve()
    emit_progress(11, "准备输出目录")
    out_dir.mkdir(parents=True, exist_ok=True)
    app_db_path = Path(args.app_db).resolve() if args.app_db else default_db_path()
    positions_path = cwd / args.positions
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
