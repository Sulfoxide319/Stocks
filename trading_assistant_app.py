#!/usr/bin/env python3
"""Desktop GUI for local 2-minute A-share trading alerts."""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Button, Frame, Label, StringVar, Tk, Toplevel, messagebox
from tkinter import ttk

from local_trading_assistant import next_sleep_seconds, phase_for_time


URGENT_SELL_ACTIONS = {"SELL_NOW", "TAKE_PROFIT", "TRAIL_SELL", "VWAP_WEAK_SELL", "PRE_CLOSE_REDUCE"}
TRADE_ACTIONS = URGENT_SELL_ACTIONS | {"BUY_NOW"}

COLORS = {
    "bg": "#f5f7fb",
    "panel": "#ffffff",
    "muted_panel": "#eef2f7",
    "ink": "#172033",
    "muted": "#667085",
    "line": "#d9e0ea",
    "buy": "#127a3d",
    "buy_bg": "#e8f6ee",
    "sell": "#b42318",
    "sell_bg": "#fff0ed",
    "warn": "#9a5b00",
    "warn_bg": "#fff7df",
    "blue": "#2158a8",
    "blue_bg": "#e9f1ff",
}


class DummyArgs:
    intraday_interval_seconds = 120
    focus_interval_seconds = 300


class TradingAssistantApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.cwd = Path(__file__).resolve().parent
        self.out_dir = self.cwd / "output" / "trading_assistant"
        self.latest_json = self.out_dir / "latest_plan.json"
        self.latest_md = self.out_dir / "latest_plan.md"
        self.positions_csv = self.cwd / "config" / "live_positions.csv"
        self.positions_example = self.cwd / "config" / "live_positions.example.csv"

        self.running = False
        self.scan_in_progress = False
        self.alerted_keys: set[str] = set()
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self.status = StringVar(value="待机")
        self.phase_text = StringVar(value="阶段：-")
        self.last_scan = StringVar(value="上次扫描：-")
        self.next_scan = StringVar(value="下一次扫描：-")
        self.buy_count = StringVar(value="0")
        self.sell_count = StringVar(value="0")
        self.watch_count = StringVar(value="0")
        self.t1_count = StringVar(value="0")
        self.detail_title = StringVar(value="选择一条记录查看细节")
        self.detail_body = StringVar(value="启动后，应用会在交易窗口内自动扫描。真正涉及买卖动作时，会弹出置顶提醒。")

        self.root.title("A股短线交易助手")
        self.root.geometry("1180x760")
        self.root.minsize(980, 640)
        self.root.configure(bg=COLORS["bg"])
        self.configure_style()
        self.build_ui()
        self.root.after(250, self.process_queue)

    def configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        base_font = ("Microsoft YaHei UI", 10)
        mono_font = ("Consolas", 10)
        style.configure(".", font=base_font, background=COLORS["bg"], foreground=COLORS["ink"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Muted.TFrame", background=COLORS["muted_panel"])
        style.configure("Title.TLabel", background=COLORS["panel"], foreground=COLORS["ink"], font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Subtle.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Metric.TLabel", background=COLORS["panel"], foreground=COLORS["ink"], font=("Consolas", 22, "bold"))
        style.configure("MetricCaption.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 9))
        style.configure("Quiet.TButton", font=base_font, padding=(12, 8))
        style.configure("Treeview", font=base_font, rowheight=30, fieldbackground=COLORS["panel"], background=COLORS["panel"], foreground=COLORS["ink"])
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), padding=(6, 8), background=COLORS["muted_panel"], foreground=COLORS["ink"])
        style.map("Treeview", background=[("selected", COLORS["blue"])], foreground=[("selected", "#ffffff")])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TLabelframe", background=COLORS["panel"], bordercolor=COLORS["line"])
        style.configure("TLabelframe.Label", background=COLORS["panel"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9, "bold"))
        self.mono_font = mono_font

    def build_ui(self) -> None:
        outer = Frame(self.root, bg=COLORS["bg"])
        outer.pack(fill=BOTH, expand=True, padx=14, pady=14)

        self.build_header(outer)
        body = Frame(outer, bg=COLORS["bg"])
        body.pack(fill=BOTH, expand=True, pady=(12, 0))

        sidebar = ttk.Frame(body, style="Panel.TFrame", padding=14)
        sidebar.pack(side=LEFT, fill=Y, padx=(0, 12))
        sidebar.configure(width=270)
        sidebar.pack_propagate(False)
        self.build_sidebar(sidebar)

        main = Frame(body, bg=COLORS["bg"])
        main.pack(side=LEFT, fill=BOTH, expand=True)
        self.build_main(main)

    def build_header(self, parent: Frame) -> None:
        header = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        header.pack(fill=X)
        left = ttk.Frame(header, style="Panel.TFrame")
        left.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(left, text="A股短线交易助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="本地 2 分钟扫描，只有买卖动作才弹窗；夜间再整理推 GitHub。", style="Subtle.TLabel").pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(header, style="Panel.TFrame")
        right.pack(side=RIGHT)
        ttk.Button(right, text="启动", command=self.start, style="Primary.TButton").pack(side=LEFT, padx=(0, 8))
        ttk.Button(right, text="停止", command=self.stop, style="Quiet.TButton").pack(side=LEFT, padx=(0, 8))
        ttk.Button(right, text="立即扫描", command=self.run_now, style="Quiet.TButton").pack(side=LEFT)

    def build_sidebar(self, parent: ttk.Frame) -> None:
        self.status_badge = Label(parent, textvariable=self.status, bg=COLORS["blue_bg"], fg=COLORS["blue"], font=("Microsoft YaHei UI", 12, "bold"), padx=12, pady=8)
        self.status_badge.pack(fill=X)
        ttk.Label(parent, textvariable=self.phase_text, style="Subtle.TLabel").pack(anchor="w", pady=(14, 0))
        ttk.Label(parent, textvariable=self.last_scan, style="Subtle.TLabel").pack(anchor="w", pady=(6, 0))
        ttk.Label(parent, textvariable=self.next_scan, style="Subtle.TLabel").pack(anchor="w", pady=(6, 16))

        metrics = ttk.Frame(parent, style="Panel.TFrame")
        metrics.pack(fill=X, pady=(0, 12))
        self.metric_card(metrics, "买入触发", self.buy_count, COLORS["buy"], 0, 0)
        self.metric_card(metrics, "卖出处理", self.sell_count, COLORS["sell"], 0, 1)
        self.metric_card(metrics, "关注等待", self.watch_count, COLORS["blue"], 1, 0)
        self.metric_card(metrics, "T+1锁定", self.t1_count, COLORS["warn"], 1, 1)

        actions = ttk.LabelFrame(parent, text="操作", padding=10)
        actions.pack(fill=X, pady=(8, 12))
        ttk.Button(actions, text="打开最新计划", command=self.open_latest_plan, style="Quiet.TButton").pack(fill=X, pady=(0, 8))
        ttk.Button(actions, text="编辑持仓 CSV", command=self.open_positions, style="Quiet.TButton").pack(fill=X, pady=(0, 8))
        ttk.Button(actions, text="测试弹窗", command=self.test_alert, style="Quiet.TButton").pack(fill=X)

        rules = ttk.LabelFrame(parent, text="盘中规则", padding=10)
        rules.pack(fill=BOTH, expand=True)
        Label(
            rules,
            text="09:20-09:45 关注池\n09:45-11:30 2分钟买卖\n13:00-14:45 2分钟买卖\n14:45-15:05 收盘前复核\n\nT+1：当天买入不提示卖出\n冷却行情：不硬开新仓",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify=LEFT,
            anchor="nw",
            font=("Microsoft YaHei UI", 9),
            pady=4,
        ).pack(fill=BOTH, expand=True)

    def metric_card(self, parent: ttk.Frame, label: str, value: StringVar, color: str, row: int, column: int) -> None:
        card = Frame(parent, bg=COLORS["panel"], highlightbackground=COLORS["line"], highlightthickness=1)
        card.grid(row=row, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0), pady=(0, 8))
        parent.columnconfigure(column, weight=1)
        Label(card, textvariable=value, bg=COLORS["panel"], fg=color, font=("Consolas", 22, "bold")).pack(anchor="w", padx=10, pady=(8, 0))
        Label(card, text=label, bg=COLORS["panel"], fg=COLORS["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=10, pady=(0, 8))

    def build_main(self, parent: Frame) -> None:
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill=BOTH, expand=True)

        self.all_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.buy_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.sell_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.notebook.add(self.all_tab, text="总览")
        self.notebook.add(self.buy_tab, text="买入")
        self.notebook.add(self.sell_tab, text="卖出/持仓")

        self.all_tree = self.create_tree(self.all_tab)
        self.buy_tree = self.create_tree(self.buy_tab)
        self.sell_tree = self.create_tree(self.sell_tab)

        detail = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        detail.pack(fill=X, pady=(12, 0))
        ttk.Label(detail, textvariable=self.detail_title, style="Title.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        Label(detail, textvariable=self.detail_body, bg=COLORS["panel"], fg=COLORS["muted"], justify=LEFT, anchor="w", wraplength=820).pack(fill=X, pady=(6, 0))

    def create_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        columns = ("side", "action", "ticker", "name", "latest", "trigger", "target", "stop", "pnl", "edge", "reason")
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
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
            "edge": "Edge",
            "reason": "理由",
        }
        widths = {
            "side": 58,
            "action": 132,
            "ticker": 82,
            "name": 108,
            "latest": 76,
            "trigger": 86,
            "target": 76,
            "stop": 76,
            "pnl": 72,
            "edge": 64,
            "reason": 360,
        }
        numeric = {"latest", "trigger", "target", "stop", "pnl", "edge"}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="e" if column in numeric else "w", stretch=column == "reason")
        tree.tag_configure("urgent", background=COLORS["sell_bg"], foreground=COLORS["sell"])
        tree.tag_configure("buy", background=COLORS["buy_bg"], foreground=COLORS["buy"])
        tree.tag_configure("hold", background=COLORS["warn_bg"], foreground=COLORS["warn"])
        tree.tag_configure("watch", background=COLORS["panel"], foreground=COLORS["ink"])
        tree.bind("<<TreeviewSelect>>", self.on_select)
        yscroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.pack(side=LEFT, fill=BOTH, expand=True)
        yscroll.pack(side=RIGHT, fill=Y)
        return tree

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.set_status("运行中", COLORS["buy_bg"], COLORS["buy"])
        self.schedule_next(100)

    def stop(self) -> None:
        self.running = False
        self.set_status("已停止", COLORS["muted_panel"], COLORS["muted"])
        self.next_scan.set("下一次扫描：-")

    def run_now(self) -> None:
        if self.scan_in_progress:
            self.set_status("扫描中", COLORS["warn_bg"], COLORS["warn"])
            return
        phase = phase_for_time(dt.datetime.now())
        if phase == "closed":
            phase = "intraday"
        self.launch_scan(phase)

    def schedule_next(self, delay_ms: int) -> None:
        if self.running:
            self.root.after(delay_ms, self.scheduled_tick)

    def scheduled_tick(self) -> None:
        if not self.running:
            return
        now = dt.datetime.now()
        phase = phase_for_time(now)
        self.phase_text.set(f"阶段：{self.phase_label(phase)}")
        if now.weekday() >= 5:
            self.set_status("周末待机", COLORS["muted_panel"], COLORS["muted"])
            self.next_scan.set("下一次检查：60 秒后")
            self.schedule_next(60_000)
            return
        if phase == "closed":
            self.set_status("非交易窗口", COLORS["muted_panel"], COLORS["muted"])
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
        self.phase_text.set(f"阶段：{self.phase_label(phase)}")
        self.set_status("扫描中", COLORS["blue_bg"], COLORS["blue"])
        thread = threading.Thread(target=self.scan_worker, args=(phase,), daemon=True)
        thread.start()

    def scan_worker(self, phase: str) -> None:
        command = [sys.executable, "local_trading_assistant.py", "--once", "--phase", phase]
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            result = subprocess.run(command, cwd=self.cwd, text=True, capture_output=True, check=True, creationflags=creationflags)
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
        self.root.after(250, self.process_queue)

    def on_scan_ok(self, result: dict[str, object]) -> None:
        self.scan_in_progress = False
        payload = result.get("payload", {}) if isinstance(result, dict) else {}
        phase = str(result.get("phase", "-")) if isinstance(result, dict) else "-"
        generated = str(payload.get("generated_at", "-")) if isinstance(payload, dict) else "-"
        buy = payload.get("buy", []) if isinstance(payload, dict) else []
        sell = payload.get("sell", []) if isinstance(payload, dict) else []
        buy_items = buy if isinstance(buy, list) else []
        sell_items = sell if isinstance(sell, list) else []
        trade_items = self.trade_items(buy_items, sell_items)
        self.render_payload(payload if isinstance(payload, dict) else {})
        self.last_scan.set(f"上次扫描：{generated}")
        self.phase_text.set(f"阶段：{self.phase_label(phase)}")
        if trade_items:
            self.set_status("有交易动作", COLORS["sell_bg"], COLORS["sell"])
            self.show_trade_alert(trade_items)
        else:
            self.set_status("无交易动作", COLORS["buy_bg"], COLORS["buy"])

    def on_scan_error(self, error: str) -> None:
        self.scan_in_progress = False
        self.set_status("扫描失败", COLORS["sell_bg"], COLORS["sell"])
        messagebox.showerror("交易助手扫描失败", error[:3000])

    def render_payload(self, payload: dict[str, object]) -> None:
        for tree in (self.all_tree, self.buy_tree, self.sell_tree):
            for item in tree.get_children():
                tree.delete(item)

        buy = payload.get("buy", [])
        sell = payload.get("sell", [])
        buy_items = buy if isinstance(buy, list) else []
        sell_items = sell if isinstance(sell, list) else []
        buy_now = 0
        sell_action = 0
        watch = 0
        t1 = 0

        for row in sell_items:
            if not isinstance(row, dict):
                continue
            values, tag = self.row_values("卖出", row)
            self.sell_tree.insert("", END, values=values, tags=(tag,))
            self.all_tree.insert("", END, values=values, tags=(tag,))
            action = str(row.get("action", ""))
            if action in URGENT_SELL_ACTIONS:
                sell_action += 1
            elif action == "HOLD_T1":
                t1 += 1

        for row in buy_items:
            if not isinstance(row, dict):
                continue
            values, tag = self.row_values("买入", row)
            self.buy_tree.insert("", END, values=values, tags=(tag,))
            self.all_tree.insert("", END, values=values, tags=(tag,))
            action = str(row.get("action", ""))
            if action == "BUY_NOW":
                buy_now += 1
            elif action in {"WATCH_BUY", "WAIT"}:
                watch += 1

        self.buy_count.set(str(buy_now))
        self.sell_count.set(str(sell_action))
        self.watch_count.set(str(watch))
        self.t1_count.set(str(t1))
        if not buy_items and not sell_items:
            self.detail_title.set("没有可展示数据")
            self.detail_body.set("请先点击“立即扫描”，或在交易窗口内启动自动扫描。")

    def row_values(self, side: str, row: dict[str, object]) -> tuple[tuple[object, ...], str]:
        action = str(row.get("action", ""))
        if side == "卖出":
            tag = "urgent" if action in URGENT_SELL_ACTIONS else "hold" if action == "HOLD_T1" else "watch"
            values = (
                side,
                action,
                row.get("ticker", ""),
                row.get("name", ""),
                self.fmt(row.get("latest_price")),
                self.fmt(row.get("buy_price")),
                self.fmt(row.get("target_price")),
                self.fmt(row.get("hard_stop_price")),
                f"{self.fmt(row.get('pnl_pct'))}%",
                "",
                row.get("reason", ""),
            )
        else:
            tag = "buy" if action == "BUY_NOW" else "watch"
            values = (
                side,
                action,
                row.get("ticker", ""),
                row.get("name", ""),
                self.fmt(row.get("latest_price")),
                self.fmt(row.get("trigger_price")),
                self.fmt(row.get("target_price")),
                self.fmt(row.get("hard_stop_price")),
                "",
                self.fmt(row.get("edge_score")),
                row.get("reason", ""),
            )
        return values, tag

    def trade_items(self, buy: list[object], sell: list[object]) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for row in sell:
            if isinstance(row, dict) and row.get("action") in URGENT_SELL_ACTIONS:
                item = dict(row)
                item["side"] = "卖出"
                items.append(item)
        for row in buy:
            if isinstance(row, dict) and row.get("action") == "BUY_NOW":
                item = dict(row)
                item["side"] = "买入"
                items.append(item)
        return items

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
        popup.geometry("760x420")
        popup.configure(bg=COLORS["bg"])
        popup.attributes("-topmost", True)
        popup.lift()

        panel = ttk.Frame(popup, style="Panel.TFrame", padding=16)
        panel.pack(fill=BOTH, expand=True, padx=12, pady=12)
        ttk.Label(panel, text="出现需要处理的交易动作", style="Title.TLabel").pack(anchor="w")
        ttk.Label(panel, text="请按你的券商交易界面确认价格和可卖数量，本程序只给建议，不自动下单。", style="Subtle.TLabel").pack(anchor="w", pady=(4, 10))
        tree = ttk.Treeview(panel, columns=("side", "action", "ticker", "name", "latest", "reason"), show="headings", height=8)
        for column, label, width in [
            ("side", "方向", 64),
            ("action", "动作", 136),
            ("ticker", "代码", 90),
            ("name", "名称", 108),
            ("latest", "最新", 82),
            ("reason", "理由", 280),
        ]:
            tree.heading(column, text=label)
            tree.column(column, width=width, anchor="e" if column == "latest" else "w", stretch=column == "reason")
        tree.tag_configure("urgent", background=COLORS["sell_bg"], foreground=COLORS["sell"])
        tree.tag_configure("buy", background=COLORS["buy_bg"], foreground=COLORS["buy"])
        for item in new_items:
            tag = "buy" if item.get("side") == "买入" else "urgent"
            tree.insert("", END, values=(item.get("side", ""), item.get("action", ""), item.get("ticker", ""), item.get("name", ""), self.fmt(item.get("latest_price")), item.get("reason", "")), tags=(tag,))
        tree.pack(fill=BOTH, expand=True, pady=(0, 12))
        buttons = ttk.Frame(panel, style="Panel.TFrame")
        buttons.pack(fill=X)
        ttk.Button(buttons, text="打开最新计划", command=self.open_latest_plan, style="Primary.TButton").pack(side=LEFT)
        ttk.Button(buttons, text="我知道了", command=popup.destroy, style="Quiet.TButton").pack(side=RIGHT)

    def on_select(self, _event: object) -> None:
        tree = self.root.focus_get()
        if not isinstance(tree, ttk.Treeview):
            return
        selected = tree.selection()
        if not selected:
            return
        values = tree.item(selected[0], "values")
        if not values:
            return
        side, action, ticker, name, latest, trigger, target, stop, pnl, edge, reason = values
        self.detail_title.set(f"{side} {action} - {ticker} {name}")
        pieces = [
            f"最新价：{latest or '-'}",
            f"触发/成本：{trigger or '-'}",
            f"目标价：{target or '-'}",
            f"止损价：{stop or '-'}",
        ]
        if pnl:
            pieces.append(f"盈亏：{pnl}")
        if edge:
            pieces.append(f"Edge：{edge}")
        pieces.append(f"理由：{reason or '-'}")
        self.detail_body.set("    ".join(pieces))

    def open_latest_plan(self) -> None:
        if self.latest_md.exists():
            os.startfile(self.latest_md)
        else:
            messagebox.showinfo("暂无计划", "还没有生成 latest_plan.md。")

    def open_positions(self) -> None:
        if not self.positions_csv.exists() and self.positions_example.exists():
            self.positions_csv.write_text(self.positions_example.read_text(encoding="utf-8"), encoding="utf-8")
        if self.positions_csv.exists():
            os.startfile(self.positions_csv)
        else:
            messagebox.showinfo("暂无持仓文件", "没有找到 config/live_positions.csv。")

    def test_alert(self) -> None:
        self.show_trade_alert(
            [
                {
                    "side": "买入",
                    "action": "BUY_NOW",
                    "ticker": "300000",
                    "name": "测试股票",
                    "latest_price": 10.0,
                    "reason": "弹窗测试，不代表真实交易建议。",
                }
            ]
        )

    def set_status(self, text: str, bg: str, fg: str) -> None:
        self.status.set(text)
        self.status_badge.configure(bg=bg, fg=fg)

    @staticmethod
    def phase_label(phase: str) -> str:
        return {
            "opening": "开盘关注",
            "intraday": "盘中执行",
            "preclose": "收盘前复核",
            "closed": "非交易窗口",
        }.get(phase, phase)

    @staticmethod
    def fmt(value: object) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        if abs(number) >= 1000:
            return f"{number:,.0f}"
        return f"{number:.2f}"


def main() -> int:
    root = Tk()
    try:
        root.call("tk", "scaling", 1.15)
    except Exception:
        pass
    TradingAssistantApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
