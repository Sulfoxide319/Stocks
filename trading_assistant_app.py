#!/usr/bin/env python3
"""Tkinter desktop app for local 2-minute trading alerts."""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Button, Label, StringVar, Tk, Toplevel, messagebox
from tkinter import ttk

from local_trading_assistant import phase_for_time, next_sleep_seconds


URGENT_SELL_ACTIONS = {"SELL_NOW", "TAKE_PROFIT", "TRAIL_SELL", "VWAP_WEAK_SELL", "PRE_CLOSE_REDUCE"}
TRADE_ACTIONS = URGENT_SELL_ACTIONS | {"BUY_NOW"}


class TradingAssistantApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.cwd = Path(__file__).resolve().parent
        self.out_dir = self.cwd / "output" / "trading_assistant"
        self.latest_json = self.out_dir / "latest_plan.json"
        self.latest_md = self.out_dir / "latest_plan.md"
        self.running = False
        self.scan_in_progress = False
        self.alerted_keys: set[str] = set()
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self.status = StringVar(value="未启动")
        self.next_scan = StringVar(value="-")
        self.last_scan = StringVar(value="-")

        self.root.title("A股短线本地交易助手")
        self.root.geometry("980x640")
        self.root.minsize(820, 520)

        self.build_ui()
        self.root.after(300, self.process_queue)

    def build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill=X)
        Button(toolbar, text="启动", command=self.start).pack(side=LEFT, padx=(0, 6))
        Button(toolbar, text="停止", command=self.stop).pack(side=LEFT, padx=(0, 6))
        Button(toolbar, text="立即扫描", command=self.run_now).pack(side=LEFT, padx=(0, 6))
        Button(toolbar, text="打开最新计划", command=self.open_latest_plan).pack(side=LEFT, padx=(0, 6))
        Button(toolbar, text="编辑持仓", command=self.open_positions).pack(side=LEFT, padx=(0, 6))
        Button(toolbar, text="测试弹窗", command=self.test_alert).pack(side=LEFT)

        status_frame = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        status_frame.pack(fill=X)
        Label(status_frame, textvariable=self.status, anchor="w").pack(fill=X)
        Label(status_frame, textvariable=self.last_scan, anchor="w").pack(fill=X)
        Label(status_frame, textvariable=self.next_scan, anchor="w").pack(fill=X)

        columns = ("side", "action", "ticker", "name", "latest", "trigger", "target", "stop", "pnl", "reason")
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings")
        headings = {
            "side": "方向",
            "action": "动作",
            "ticker": "代码",
            "name": "名称",
            "latest": "最新",
            "trigger": "触发/成本",
            "target": "目标",
            "stop": "止损",
            "pnl": "盈亏",
            "reason": "理由",
        }
        widths = {
            "side": 58,
            "action": 130,
            "ticker": 82,
            "name": 96,
            "latest": 76,
            "trigger": 82,
            "target": 76,
            "stop": 76,
            "pnl": 76,
            "reason": 280,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.tag_configure("urgent", background="#ffe5e5")
        self.tree.tag_configure("buy", background="#e8f5e9")
        self.tree.tag_configure("watch", background="#f7f7f7")
        self.tree.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))

        bottom = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        bottom.pack(fill=X)
        Label(
            bottom,
            text="盘中应用只做本地扫描和弹窗；晚上整理/推 GitHub 请用 nightly_publish.py 或命令行。",
            anchor="w",
        ).pack(side=LEFT, fill=X, expand=True)

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.status.set("已启动：交易窗口内自动扫描")
        self.schedule_next(100)

    def stop(self) -> None:
        self.running = False
        self.status.set("已停止")
        self.next_scan.set("下一次扫描：-")

    def run_now(self) -> None:
        if self.scan_in_progress:
            return
        phase = phase_for_time(dt.datetime.now())
        if phase == "closed":
            phase = "intraday"
        self.launch_scan(phase)

    def schedule_next(self, delay_ms: int) -> None:
        if not self.running:
            return
        self.root.after(delay_ms, self.scheduled_tick)

    def scheduled_tick(self) -> None:
        if not self.running:
            return
        now = dt.datetime.now()
        phase = phase_for_time(now)
        if now.weekday() >= 5:
            self.status.set(f"{now:%H:%M:%S} 周末，暂停扫描")
            self.next_scan.set("下一次检查：60 秒后")
            self.schedule_next(60_000)
            return
        if phase == "closed":
            self.status.set(f"{now:%H:%M:%S} 非交易助手窗口，等待")
            self.next_scan.set("下一次检查：60 秒后")
            self.schedule_next(60_000)
            return
        if not self.scan_in_progress:
            self.launch_scan(phase)
        seconds = next_sleep_seconds(phase, DummyArgs())
        self.next_scan.set(f"下一次扫描：约 {seconds} 秒后")
        self.schedule_next(seconds * 1000)

    def launch_scan(self, phase: str) -> None:
        self.scan_in_progress = True
        self.status.set(f"{dt.datetime.now():%H:%M:%S} 正在扫描：{phase}")
        thread = threading.Thread(target=self.scan_worker, args=(phase,), daemon=True)
        thread.start()

    def scan_worker(self, phase: str) -> None:
        command = [
            sys.executable,
            "local_trading_assistant.py",
            "--once",
            "--phase",
            phase,
        ]
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            result = subprocess.run(
                command,
                cwd=self.cwd,
                text=True,
                capture_output=True,
                check=True,
                creationflags=creationflags,
            )
            payload = self.read_latest_payload()
            self.event_queue.put(("scan_ok", {"phase": phase, "payload": payload, "stdout": result.stdout, "stderr": result.stderr}))
        except subprocess.CalledProcessError as exc:
            self.event_queue.put(("scan_error", f"{' '.join(command)}\n{exc.stdout}\n{exc.stderr}"))
        except Exception as exc:
            self.event_queue.put(("scan_error", str(exc)))

    def read_latest_payload(self) -> dict[str, object]:
        if not self.latest_json.exists():
            return {}
        return json.loads(self.latest_json.read_text(encoding="utf-8"))

    def process_queue(self) -> None:
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "scan_ok":
                    self.on_scan_ok(payload if isinstance(payload, dict) else {})
                elif kind == "scan_error":
                    self.on_scan_error(str(payload))
        except queue.Empty:
            pass
        self.root.after(300, self.process_queue)

    def on_scan_ok(self, result: dict[str, object]) -> None:
        self.scan_in_progress = False
        payload = result.get("payload", {}) if isinstance(result, dict) else {}
        phase = result.get("phase", "-") if isinstance(result, dict) else "-"
        generated = payload.get("generated_at", "-") if isinstance(payload, dict) else "-"
        buy = payload.get("buy", []) if isinstance(payload, dict) else []
        sell = payload.get("sell", []) if isinstance(payload, dict) else []
        trade_items = self.trade_items(buy if isinstance(buy, list) else [], sell if isinstance(sell, list) else [])
        self.status.set(f"{dt.datetime.now():%H:%M:%S} 扫描完成：{phase}，交易提示 {len(trade_items)} 条")
        self.last_scan.set(f"上次扫描：{generated}")
        self.render_payload(payload if isinstance(payload, dict) else {})
        if trade_items:
            self.show_trade_alert(trade_items)

    def on_scan_error(self, error: str) -> None:
        self.scan_in_progress = False
        self.status.set("扫描失败")
        messagebox.showerror("交易助手扫描失败", error[:3000])

    def trade_items(self, buy: list[object], sell: list[object]) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for row in sell:
            if isinstance(row, dict) and row.get("action") in URGENT_SELL_ACTIONS:
                row = dict(row)
                row["side"] = "卖出"
                items.append(row)
        for row in buy:
            if isinstance(row, dict) and row.get("action") == "BUY_NOW":
                row = dict(row)
                row["side"] = "买入"
                items.append(row)
        return items

    def render_payload(self, payload: dict[str, object]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        sell = payload.get("sell", [])
        buy = payload.get("buy", [])
        if isinstance(sell, list):
            for row in sell[:20]:
                if isinstance(row, dict):
                    action = str(row.get("action", ""))
                    tag = "urgent" if action in URGENT_SELL_ACTIONS else "watch"
                    self.tree.insert(
                        "",
                        END,
                        values=(
                            "卖出",
                            action,
                            row.get("ticker", ""),
                            row.get("name", ""),
                            self.fmt(row.get("latest_price")),
                            self.fmt(row.get("buy_price")),
                            self.fmt(row.get("target_price")),
                            self.fmt(row.get("hard_stop_price")),
                            f"{self.fmt(row.get('pnl_pct'))}%",
                            row.get("reason", ""),
                        ),
                        tags=(tag,),
                    )
        if isinstance(buy, list):
            for row in buy[:20]:
                if isinstance(row, dict):
                    action = str(row.get("action", ""))
                    tag = "buy" if action == "BUY_NOW" else "watch"
                    self.tree.insert(
                        "",
                        END,
                        values=(
                            "买入",
                            action,
                            row.get("ticker", ""),
                            row.get("name", ""),
                            self.fmt(row.get("latest_price")),
                            self.fmt(row.get("trigger_price")),
                            self.fmt(row.get("target_price")),
                            self.fmt(row.get("hard_stop_price")),
                            "",
                            row.get("reason", ""),
                        ),
                        tags=(tag,),
                    )

    def show_trade_alert(self, trade_items: list[dict[str, object]]) -> None:
        new_items: list[dict[str, object]] = []
        for item in trade_items:
            key = f"{item.get('side')}:{item.get('action')}:{item.get('ticker')}:{self.fmt(item.get('latest_price'))}"
            if key not in self.alerted_keys:
                self.alerted_keys.add(key)
                new_items.append(item)
        if not new_items:
            return
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass

        popup = Toplevel(self.root)
        popup.title("交易动作提示")
        popup.geometry("720x360")
        popup.attributes("-topmost", True)
        popup.lift()
        Label(popup, text="出现需要处理的交易动作", font=("Microsoft YaHei UI", 14, "bold"), anchor="w").pack(fill=X, padx=12, pady=(12, 6))
        text = ttk.Treeview(popup, columns=("side", "action", "ticker", "name", "latest", "reason"), show="headings", height=8)
        for column, label, width in [
            ("side", "方向", 60),
            ("action", "动作", 130),
            ("ticker", "代码", 90),
            ("name", "名称", 100),
            ("latest", "最新", 80),
            ("reason", "理由", 260),
        ]:
            text.heading(column, text=label)
            text.column(column, width=width, anchor="w")
        for item in new_items:
            text.insert(
                "",
                END,
                values=(item.get("side", ""), item.get("action", ""), item.get("ticker", ""), item.get("name", ""), self.fmt(item.get("latest_price")), item.get("reason", "")),
            )
        text.pack(fill=BOTH, expand=True, padx=12, pady=6)
        buttons = ttk.Frame(popup, padding=12)
        buttons.pack(fill=X)
        Button(buttons, text="打开计划", command=self.open_latest_plan).pack(side=LEFT)
        Button(buttons, text="我知道了", command=popup.destroy).pack(side=RIGHT)

    def open_latest_plan(self) -> None:
        if self.latest_md.exists():
            os.startfile(self.latest_md)
        else:
            messagebox.showinfo("暂无计划", "还没有生成 latest_plan.md")

    def open_positions(self) -> None:
        positions = self.cwd / "config" / "live_positions.csv"
        example = self.cwd / "config" / "live_positions.example.csv"
        if not positions.exists() and example.exists():
            positions.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        if positions.exists():
            os.startfile(positions)
        else:
            messagebox.showinfo("暂无持仓文件", "没有找到 config/live_positions.csv")

    def test_alert(self) -> None:
        self.show_trade_alert(
            [
                {
                    "side": "买入",
                    "action": "BUY_NOW",
                    "ticker": "300000",
                    "name": "测试",
                    "latest_price": 10.0,
                    "reason": "弹窗测试，不代表真实交易建议",
                }
            ]
        )

    @staticmethod
    def fmt(value: object) -> str:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return ""


class DummyArgs:
    intraday_interval_seconds = 120
    focus_interval_seconds = 300


def main() -> int:
    root = Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except Exception:
        pass
    app = TradingAssistantApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
