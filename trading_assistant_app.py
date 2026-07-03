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
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Canvas, Frame, IntVar, Label, StringVar, Tk, Toplevel, messagebox
from tkinter import ttk

from dependency_bootstrap import ensure_project_dependencies

ensure_project_dependencies()

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
    postclose_interval_seconds = 900


class TradingAssistantApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.cwd = Path(__file__).resolve().parent
        self.out_dir = self.cwd / "output" / "trading_assistant"
        self.latest_json = self.out_dir / "latest_plan.json"
        self.latest_md = self.out_dir / "latest_plan.md"
        self.positions_csv = self.cwd / "config" / "live_positions.csv"
        self.positions_example = self.cwd / "config" / "live_positions.example.csv"
        self.install_dir = self.resolve_install_dir()
        self.version = self.read_app_version()

        self.running = False
        self.scan_in_progress = False
        self.update_in_progress = False
        self.alerted_keys: set[str] = set()
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.action_buttons: dict[str, ttk.Button] = {}
        self.tree_empty_labels: dict[ttk.Treeview, Label] = {}
        self.tree_xscrollbars: list[ttk.Scrollbar] = []
        self.detail_frame: ttk.Frame | None = None
        self.detail_body_label: Label | None = None

        self.status = StringVar(value="待机")
        self.phase_text = StringVar(value="阶段：-")
        self.last_scan = StringVar(value="上次扫描：-")
        self.next_scan = StringVar(value="下一次扫描：-")
        self.scan_progress_text = StringVar(value="扫描进度：待开始")
        self.scan_progress = IntVar(value=0)
        self.scan_log_text = StringVar(value="扫描日志：暂无。")
        self.version_text = StringVar(value=f"版本：v{self.version}")
        self.update_event = StringVar(value=f"更新：当前 v{self.version}")
        self.update_log = StringVar(value="更新日志：可手动检查 GitHub Release。")
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

    def place_trade_popup(self, popup: Toplevel) -> None:
        self.root.update_idletasks()
        screen_w = max(1, self.root.winfo_screenwidth())
        screen_h = max(1, self.root.winfo_screenheight())
        max_w = max(360, screen_w - 96)
        max_h = max(300, screen_h - 120)
        width = min(max(820, int(screen_w * 0.64)), max_w)
        height = min(max(460, int(screen_h * 0.52)), max_h)

        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = max(1, self.root.winfo_width())
        root_h = max(1, self.root.winfo_height())
        x = root_x + (root_w - width) // 2
        y = root_y + (root_h - height) // 2
        x = min(max(24, x), max(24, screen_w - width - 24))
        y = min(max(24, y), max(24, screen_h - height - 48))

        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.minsize(min(720, width), min(380, height))
        popup.maxsize(max_w, max_h)

    def resolve_install_dir(self) -> Path:
        for candidate in (self.cwd, self.cwd.parent):
            if (candidate / "Update-StocksTool.ps1").exists():
                return candidate
        for candidate in (self.cwd, self.cwd.parent):
            if (candidate / "VERSION").exists():
                return candidate
        return self.cwd

    def read_app_version(self) -> str:
        for candidate in (self.install_dir / "VERSION", self.cwd / "VERSION", self.cwd.parent / "VERSION"):
            if candidate.exists():
                text = candidate.read_text(encoding="utf-8").strip()
                return text.lstrip("v") or "0.0.0"
        return "0.0.0"

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
        outer.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        self.build_header(outer)
        body = Frame(outer, bg=COLORS["bg"])
        body.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        body.columnconfigure(0, minsize=282)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(body, style="Panel.TFrame", padding=14)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        sidebar.configure(width=282)
        sidebar.grid_propagate(False)
        self.build_sidebar(sidebar)

        main = Frame(body, bg=COLORS["bg"])
        main.grid(row=0, column=1, sticky="nsew")
        self.build_main(main)

    def build_header(self, parent: Frame) -> None:
        header = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        left = ttk.Frame(header, style="Panel.TFrame")
        left.grid(row=0, column=0, sticky="ew")
        ttk.Label(left, text="A股短线交易助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text=f"本地 2 分钟扫描，只有买卖动作才弹窗；夜间再整理推 GitHub。  v{self.version}", style="Subtle.TLabel").pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(header, style="Panel.TFrame")
        right.grid(row=0, column=1, sticky="e", padx=(12, 0))
        ttk.Button(right, text="启动", command=self.start, style="Primary.TButton").pack(side=LEFT, padx=(0, 8))
        ttk.Button(right, text="停止", command=self.stop, style="Quiet.TButton").pack(side=LEFT, padx=(0, 8))
        ttk.Button(right, text="立即扫描", command=self.run_now, style="Quiet.TButton").pack(side=LEFT)

    def build_sidebar(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)
        self.build_status_panel(parent)
        self.build_metrics_panel(parent)
        self.build_actions_panel(parent)
        self.build_info_panel(parent)

    def build_status_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Panel.TFrame")
        panel.grid(row=0, column=0, sticky="ew")
        panel.columnconfigure(0, weight=1)
        self.status_badge = Label(panel, textvariable=self.status, bg=COLORS["blue_bg"], fg=COLORS["blue"], font=("Microsoft YaHei UI", 12, "bold"), padx=12, pady=9)
        self.status_badge.grid(row=0, column=0, sticky="ew")
        for row, variable in enumerate((self.phase_text, self.last_scan, self.next_scan, self.update_event), start=1):
            ttk.Label(panel, textvariable=variable, style="Subtle.TLabel").grid(row=row, column=0, sticky="w", pady=(8 if row == 1 else 4, 0))
        self.scan_progressbar = ttk.Progressbar(panel, mode="determinate", maximum=100, variable=self.scan_progress)
        self.scan_progressbar.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(panel, textvariable=self.scan_progress_text, style="Subtle.TLabel").grid(row=6, column=0, sticky="w", pady=(4, 0))

    def build_metrics_panel(self, parent: ttk.Frame) -> None:
        metrics = ttk.Frame(parent, style="Panel.TFrame")
        metrics.grid(row=1, column=0, sticky="ew", pady=(14, 8))
        metrics.columnconfigure(0, weight=1)
        metrics.columnconfigure(1, weight=1)
        self.metric_card(metrics, "买入触发", self.buy_count, COLORS["buy"], 0, 0)
        self.metric_card(metrics, "卖出处理", self.sell_count, COLORS["sell"], 0, 1)
        self.metric_card(metrics, "关注等待", self.watch_count, COLORS["blue"], 1, 0)
        self.metric_card(metrics, "T+1锁定", self.t1_count, COLORS["warn"], 1, 1)

    def build_actions_panel(self, parent: ttk.Frame) -> None:
        actions = ttk.LabelFrame(parent, text="操作", padding=10)
        actions.grid(row=2, column=0, sticky="ew", pady=(2, 10))
        actions.columnconfigure(0, weight=1)
        button_specs = [
            ("open_latest", "打开最新计划", self.open_latest_plan),
            ("positions", "编辑持仓 CSV", self.open_positions),
            ("update", "检查更新", self.check_update),
            ("test_alert", "测试弹窗", self.test_alert),
        ]
        for row, (key, text, command) in enumerate(button_specs):
            button = ttk.Button(actions, text=text, command=command, style="Quiet.TButton")
            button.grid(row=row, column=0, sticky="ew", pady=(0, 8 if row < len(button_specs) - 1 else 0), ipady=4)
            self.action_buttons[key] = button
        self.update_button = self.action_buttons["update"]

    def build_info_panel(self, parent: ttk.Frame) -> None:
        info = ttk.LabelFrame(parent, text="规则与更新日志", padding=0)
        info.grid(row=3, column=0, sticky="nsew")
        info.columnconfigure(0, weight=1)
        info.rowconfigure(0, weight=1)

        canvas = Canvas(info, bg=COLORS["panel"], highlightthickness=0, borderwidth=0)
        yscroll = ttk.Scrollbar(info, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas, style="Panel.TFrame", padding=10)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.configure(yscrollcommand=yscroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")

        Label(
            content,
            text="09:20-09:45 关注池\n09:45-11:30 2分钟买卖\n13:00-14:45 2分钟买卖\n14:45-15:05 收盘前复核\n\nT+1：当天买入不提示卖出\n冷却行情：不硬开新仓",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify=LEFT,
            anchor="nw",
            font=("Microsoft YaHei UI", 9),
        ).pack(fill=X, anchor="w")
        Label(
            content,
            textvariable=self.update_log,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify=LEFT,
            anchor="nw",
            wraplength=230,
            font=("Microsoft YaHei UI", 9),
        ).pack(fill=X, anchor="w", pady=(12, 0))
        Label(
            content,
            textvariable=self.scan_log_text,
            bg=COLORS["muted_panel"],
            fg=COLORS["ink"],
            justify=LEFT,
            anchor="nw",
            wraplength=230,
            padx=8,
            pady=7,
            font=("Consolas", 9),
        ).pack(fill=X, anchor="w", pady=(12, 0))

    def metric_card(self, parent: ttk.Frame, label: str, value: StringVar, color: str, row: int, column: int) -> None:
        card = Frame(parent, bg=COLORS["panel"], highlightbackground=COLORS["line"], highlightthickness=1)
        card.grid(row=row, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0), pady=(0, 8))
        parent.columnconfigure(column, weight=1)
        parent.rowconfigure(row, minsize=64)
        Label(card, textvariable=value, bg=COLORS["panel"], fg=color, font=("Consolas", 20, "bold")).pack(anchor="w", padx=10, pady=(6, 0))
        Label(card, text=label, bg=COLORS["panel"], fg=COLORS["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=10, pady=(0, 8))

    def build_main(self, parent: Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(parent)
        self.notebook.grid(row=0, column=0, sticky="nsew")

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
        detail.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        detail.grid_propagate(False)
        detail.configure(height=96)
        detail.columnconfigure(0, weight=1)
        self.detail_frame = detail
        ttk.Label(detail, textvariable=self.detail_title, style="Title.TLabel", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        self.detail_body_label = Label(detail, textvariable=self.detail_body, bg=COLORS["panel"], fg=COLORS["muted"], justify=LEFT, anchor="nw", wraplength=820)
        self.detail_body_label.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        detail.bind("<Configure>", self.on_detail_resize)

    def create_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
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
        xscroll = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        empty = Label(
            parent,
            text="暂无扫描结果\n点击“立即扫描”开始",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify="center",
            font=("Microsoft YaHei UI", 11),
        )
        empty.place(relx=0.5, rely=0.5, anchor="center")
        self.tree_empty_labels[tree] = empty
        self.tree_xscrollbars.append(xscroll)
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
        self.set_scan_progress(5, f"准备扫描 {self.phase_label(phase)}")
        self.scan_log_text.set("扫描日志：\n- 准备启动扫描进程")
        thread = threading.Thread(target=self.scan_worker, args=(phase,), daemon=True)
        thread.start()

    def scan_worker(self, phase: str) -> None:
        command = [sys.executable, "-u", "local_trading_assistant.py", "--once", "--phase", phase]
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                command,
                cwd=self.cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            output_lines: list[str] = []
            if process.stdout is not None:
                for line in process.stdout:
                    text = line.strip()
                    if not text:
                        continue
                    output_lines.append(text)
                    self.event_queue.put(("scan_output", text))
            returncode = process.wait()
            if returncode != 0:
                raise subprocess.CalledProcessError(returncode, command, output="\n".join(output_lines), stderr="")
            payload = self.read_latest_payload()
            self.event_queue.put(("scan_ok", {"phase": phase, "payload": payload, "stdout": "\n".join(output_lines), "stderr": ""}))
        except subprocess.CalledProcessError as exc:
            self.event_queue.put(("scan_error", f"{' '.join(command)}\n{exc.output}\n{exc.stderr}"))
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
                if kind == "scan_output":
                    self.on_scan_output(str(payload))
                elif kind == "scan_progress":
                    self.on_scan_progress(payload if isinstance(payload, dict) else {})
                elif kind == "scan_ok":
                    self.on_scan_ok(payload if isinstance(payload, dict) else {})
                elif kind == "scan_error":
                    self.on_scan_error(str(payload))
                elif kind == "update_done":
                    self.on_update_done(payload if isinstance(payload, dict) else {})
        except queue.Empty:
            pass
        self.root.after(250, self.process_queue)

    def on_scan_output(self, line: str) -> None:
        if line.startswith("SCAN_PROGRESS|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                try:
                    self.set_scan_progress(int(parts[1]), parts[2])
                    self.append_scan_log(parts[2])
                    return
                except ValueError:
                    pass
        self.append_scan_log(line)

    def on_scan_progress(self, payload: dict[str, object]) -> None:
        percent = int(payload.get("percent", 0) or 0)
        message = str(payload.get("message", "扫描中"))
        self.set_scan_progress(percent, message)
        self.append_scan_log(message)

    def set_scan_progress(self, percent: int, message: str) -> None:
        value = max(0, min(100, percent))
        self.scan_progress.set(value)
        self.scan_progress_text.set(f"扫描进度：{value}% · {message}")

    def append_scan_log(self, message: str) -> None:
        lines = self.scan_log_text.get().splitlines()
        if lines and lines[0].startswith("扫描日志"):
            lines = lines[1:]
        lines.append(f"- {dt.datetime.now():%H:%M:%S} {message}")
        self.scan_log_text.set("扫描日志：\n" + "\n".join(lines[-8:]))

    def on_scan_ok(self, result: dict[str, object]) -> None:
        self.scan_in_progress = False
        self.set_scan_progress(100, "扫描完成")
        self.append_scan_log("扫描完成，正在刷新结果")
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
        self.set_scan_progress(100, "扫描失败")
        self.append_scan_log("扫描失败，请查看弹窗错误")
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
        self.update_empty_states()
        if not buy_items and not sell_items:
            self.detail_title.set("没有可展示数据")
            self.detail_body.set("请先点击“立即扫描”，或在交易窗口内启动自动扫描。")

    def update_empty_states(self) -> None:
        for tree, label in self.tree_empty_labels.items():
            if tree.get_children():
                label.place_forget()
            else:
                label.place(relx=0.5, rely=0.5, anchor="center")

    def on_detail_resize(self, event: object) -> None:
        if self.detail_body_label is None:
            return
        width = getattr(event, "width", 820)
        self.detail_body_label.configure(wraplength=max(240, int(width) - 32))

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
        popup.configure(bg=COLORS["bg"])
        self.place_trade_popup(popup)
        popup.transient(self.root)
        popup.attributes("-topmost", True)
        popup.lift()

        panel = ttk.Frame(popup, style="Panel.TFrame", padding=16)
        panel.pack(fill=BOTH, expand=True, padx=12, pady=12)
        ttk.Label(panel, text="出现需要处理的交易动作", style="Title.TLabel").pack(anchor="w")
        ttk.Label(panel, text="请按你的券商交易界面确认价格和可卖数量，本程序只给建议，不自动下单。", style="Subtle.TLabel").pack(anchor="w", pady=(4, 10))
        table_frame = ttk.Frame(panel, style="Panel.TFrame")
        table_frame.pack(fill=BOTH, expand=True, pady=(0, 12))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(table_frame, columns=("side", "action", "ticker", "name", "latest", "reason"), show="headings", height=8)
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
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        for item in new_items:
            tag = "buy" if item.get("side") == "买入" else "urgent"
            tree.insert("", END, values=(item.get("side", ""), item.get("action", ""), item.get("ticker", ""), item.get("name", ""), self.fmt(item.get("latest_price")), item.get("reason", "")), tags=(tag,))
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        buttons = ttk.Frame(panel, style="Panel.TFrame")
        buttons.pack(fill=X)
        ttk.Button(buttons, text="打开最新计划", command=self.open_latest_plan, style="Primary.TButton").pack(side=LEFT)
        ttk.Button(buttons, text="我知道了", command=popup.destroy, style="Quiet.TButton").pack(side=RIGHT)
        popup.focus_force()

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

    def find_updater(self) -> Path | None:
        candidates = [
            self.install_dir / "Update-StocksTool.ps1",
            self.cwd / "Update-StocksTool.ps1",
            self.cwd.parent / "Update-StocksTool.ps1",
            self.cwd / "installer" / "Update-StocksTool.ps1",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def set_update_event(self, summary: str, detail: str | None = None) -> None:
        self.update_event.set(f"更新：{summary}")
        if detail is not None:
            self.update_log.set(f"更新日志：{detail}")

    def check_update(self) -> None:
        if self.update_in_progress:
            return
        updater = self.find_updater()
        if updater is None:
            self.set_update_event("未找到脚本", "未找到 Update-StocksTool.ps1，请使用新版安装包。")
            return
        if (self.install_dir / ".git").exists() and "installer" in updater.parts:
            self.set_update_event("源码目录", "当前是源码目录，请用 git pull 或发布包更新，避免覆盖工作区。")
            return
        self.update_in_progress = True
        self.update_button.state(["disabled"])
        self.set_update_event("正在检查", "正在检查 GitHub Release...")
        thread = threading.Thread(target=self.update_worker, args=(updater,), daemon=True)
        thread.start()

    def update_worker(self, updater: Path) -> None:
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(updater),
            "-InstallDir",
            str(self.install_dir),
        ]
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            result = subprocess.run(command, text=True, capture_output=True, cwd=self.install_dir, creationflags=creationflags)
            self.event_queue.put(
                (
                    "update_done",
                    {
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    },
                )
            )
        except Exception as exc:
            self.event_queue.put(("update_done", {"returncode": 1, "stdout": "", "stderr": str(exc)}))

    def on_update_done(self, result: dict[str, object]) -> None:
        self.update_in_progress = False
        self.update_button.state(["!disabled"])
        stdout = str(result.get("stdout", "")).strip()
        stderr = str(result.get("stderr", "")).strip()
        lines = [line.strip() for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
        message = lines[-1] if lines else "检查完成。"
        returncode = int(result.get("returncode", 0) or 0)
        self.version = self.read_app_version()
        self.version_text.set(f"版本：v{self.version}")
        if returncode == 0:
            if message.lower().startswith("updated to"):
                self.set_update_event("已更新，需重启", f"{message}，重启后使用新版。")
            else:
                self.set_update_event("检查完成", message)
        else:
            self.set_update_event("更新失败", message)

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
