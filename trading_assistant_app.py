#!/usr/bin/env python3
"""Desktop GUI for local 2-minute A-share trading alerts."""

from __future__ import annotations

import datetime as dt
import csv
import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Canvas, Frame, IntVar, Label, StringVar, Tk, Toplevel, messagebox
from tkinter import font as tkfont
from tkinter import ttk

from dependency_bootstrap import ensure_project_dependencies

ensure_project_dependencies()

from broker_position_sync import sync_holdings_to_csv
from guoshengrui_bridge import (
    export_guoshengrui_holdings,
    open_guoshengrui_for_ticker as jump_guoshengrui_for_ticker,
    open_guoshengrui_trade_for_ticker as jump_guoshengrui_trade_for_ticker,
)
from local_trading_assistant import next_sleep_seconds, phase_for_time


URGENT_SELL_ACTIONS = {"SELL_NOW", "TAKE_PROFIT", "TRAIL_SELL", "VWAP_WEAK_SELL", "PRE_CLOSE_REDUCE"}
TRADE_ACTIONS = URGENT_SELL_ACTIONS | {"BUY_NOW"}
FONT_MIN = 8
FONT_MAX = 16
FONT_DEFAULT = 10

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
        self.account_snapshot_path = self.cwd / "config" / "broker_account_snapshot.json"
        self.ui_settings_path = self.cwd / "config" / "ui_settings.json"
        self.install_dir = self.resolve_install_dir()
        self.version = self.read_app_version()
        self.font_size = self.load_ui_settings().get("font_size", FONT_DEFAULT)

        self.running = False
        self.scan_in_progress = False
        self.update_in_progress = False
        self.update_started_at: dt.datetime | None = None
        self.alerted_keys: set[str] = set()
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.action_buttons: dict[str, ttk.Button] = {}
        self.tree_empty_labels: dict[ttk.Treeview, Label] = {}
        self.tree_xscrollbars: list[ttk.Scrollbar] = []
        self.detail_frame: ttk.Frame | None = None
        self.detail_body_label: Label | None = None
        self.sidebar: ttk.Frame | None = None
        self.fonts: dict[str, tkfont.Font] = {}
        self._closing = False
        self._process_queue_after_id: str | None = None

        self.status = StringVar(value="待机")
        self.phase_text = StringVar(value="阶段：-")
        self.last_scan = StringVar(value="上次扫描：-")
        self.next_scan = StringVar(value="下一次扫描：-")
        self.scan_progress_text = StringVar(value="扫描进度：待开始")
        self.scan_progress = IntVar(value=0)
        self.scan_log_text = StringVar(value="扫描日志：暂无。")
        self.font_size_text = StringVar(value=f"字号 {self.font_size}")
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
        self.load_cached_scan_result()
        self.root.bind("<Destroy>", self.on_root_destroy, add="+")
        self.schedule_process_queue()

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

    def load_ui_settings(self) -> dict[str, int]:
        try:
            if self.ui_settings_path.exists():
                data = json.loads(self.ui_settings_path.read_text(encoding="utf-8"))
                font_size = int(data.get("font_size", FONT_DEFAULT))
                return {"font_size": self.clamp_font_size(font_size)}
        except Exception:
            pass
        return {"font_size": FONT_DEFAULT}

    def save_ui_settings(self) -> None:
        try:
            self.ui_settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.ui_settings_path.write_text(json.dumps({"font_size": self.font_size}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def clamp_font_size(self, value: int) -> int:
        return max(FONT_MIN, min(FONT_MAX, value))

    def configure_fonts(self) -> None:
        size = self.clamp_font_size(self.font_size)
        specs = {
            "base": ("Microsoft YaHei UI", size, "normal"),
            "subtle": ("Microsoft YaHei UI", max(FONT_MIN, size - 1), "normal"),
            "title": ("Microsoft YaHei UI", size + 8, "bold"),
            "button": ("Microsoft YaHei UI", size, "normal"),
            "button_bold": ("Microsoft YaHei UI", size, "bold"),
            "heading": ("Microsoft YaHei UI", max(FONT_MIN, size - 1), "bold"),
            "status": ("Microsoft YaHei UI", size + 2, "bold"),
            "detail_title": ("Microsoft YaHei UI", size + 2, "bold"),
            "empty": ("Microsoft YaHei UI", size + 1, "normal"),
            "metric": ("Consolas", size + 10, "bold"),
            "mono": ("Consolas", size, "normal"),
            "mono_small": ("Consolas", max(FONT_MIN, size - 1), "normal"),
        }
        for key, (family, font_size, weight) in specs.items():
            if key not in self.fonts:
                self.fonts[key] = tkfont.Font(root=self.root, family=family, size=font_size, weight=weight)
            else:
                self.fonts[key].configure(family=family, size=font_size, weight=weight)

    def configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        self.style = style
        self.configure_fonts()
        style.configure(".", font=self.fonts["base"], background=COLORS["bg"], foreground=COLORS["ink"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Muted.TFrame", background=COLORS["muted_panel"])
        style.configure("Title.TLabel", background=COLORS["panel"], foreground=COLORS["ink"], font=self.fonts["title"])
        style.configure("Subtle.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=self.fonts["subtle"])
        style.configure("Metric.TLabel", background=COLORS["panel"], foreground=COLORS["ink"], font=self.fonts["metric"])
        style.configure("MetricCaption.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=self.fonts["subtle"])
        style.configure("Primary.TButton", font=self.fonts["button_bold"], padding=(14, 9))
        style.configure("Quiet.TButton", font=self.fonts["button"], padding=(12, 8))
        style.configure("Treeview", font=self.fonts["base"], rowheight=max(30, self.font_size * 3), fieldbackground=COLORS["panel"], background=COLORS["panel"], foreground=COLORS["ink"])
        style.configure("Treeview.Heading", font=self.fonts["heading"], padding=(6, 8), background=COLORS["muted_panel"], foreground=COLORS["ink"])
        style.map("Treeview", background=[("selected", COLORS["blue"])], foreground=[("selected", "#ffffff")])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 9), font=self.fonts["button_bold"])
        style.configure("TLabelframe", background=COLORS["panel"], bordercolor=COLORS["line"])
        style.configure("TLabelframe.Label", background=COLORS["panel"], foreground=COLORS["muted"], font=self.fonts["heading"])
        self.mono_font = self.fonts["mono"]

    def sidebar_width(self) -> int:
        return max(282, 282 + (self.font_size - FONT_DEFAULT) * 18)

    def detail_height(self) -> int:
        return max(96, 96 + (self.font_size - FONT_DEFAULT) * 8)

    def adjust_font_size(self, delta: int) -> None:
        self.apply_font_size(self.font_size + delta, save=True)

    def apply_font_size(self, value: int, save: bool = False) -> None:
        next_size = self.clamp_font_size(value)
        if next_size == self.font_size:
            self.font_size_text.set(f"字号 {self.font_size}")
            return
        self.font_size = next_size
        self.font_size_text.set(f"字号 {self.font_size}")
        self.configure_style()
        if self.sidebar is not None:
            self.sidebar.configure(width=self.sidebar_width())
        if self.detail_frame is not None:
            self.detail_frame.configure(height=self.detail_height())
        if save:
            self.save_ui_settings()
        self.root.update_idletasks()

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
        self.sidebar = sidebar
        sidebar.configure(width=self.sidebar_width())
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
        self.status_badge = Label(panel, textvariable=self.status, bg=COLORS["blue_bg"], fg=COLORS["blue"], font=self.fonts["status"], padx=12, pady=9)
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
        actions.columnconfigure(1, weight=1)
        button_specs = [
            ("open_latest", "打开最新计划", self.open_latest_plan),
            ("positions", "编辑持仓 CSV", self.open_positions),
            ("sync_broker_positions", "扫描国盛睿持仓", self.sync_positions_from_guoshengrui),
            ("update", "检查更新", self.check_update),
            ("test_alert", "测试弹窗", self.test_alert),
        ]
        for index, (key, text, command) in enumerate(button_specs):
            row = index // 2
            column = index % 2
            button = ttk.Button(actions, text=text, command=command, style="Quiet.TButton")
            button.grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(0, 6) if column == 0 else (6, 0),
                pady=(0, 8 if row < (len(button_specs) - 1) // 2 else 0),
                ipady=4,
            )
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

        font_row = ttk.Frame(content, style="Panel.TFrame")
        font_row.pack(fill=X, anchor="w", pady=(0, 10))
        ttk.Button(font_row, text="-", command=lambda: self.adjust_font_size(-1), style="Quiet.TButton", width=3).pack(side=LEFT)
        ttk.Label(font_row, textvariable=self.font_size_text, style="Subtle.TLabel").pack(side=LEFT, padx=8)
        ttk.Button(font_row, text="+", command=lambda: self.adjust_font_size(1), style="Quiet.TButton", width=3).pack(side=LEFT)

        Label(
            content,
            text="09:20-09:45 关注池\n09:45-11:30 2分钟买卖\n13:00-14:45 2分钟买卖\n14:45-15:05 收盘前复核\n\nT+1：当天买入不提示卖出\n冷却行情：不硬开新仓",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify=LEFT,
            anchor="nw",
            font=self.fonts["subtle"],
        ).pack(fill=X, anchor="w")
        Label(
            content,
            textvariable=self.update_log,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify=LEFT,
            anchor="nw",
            wraplength=230,
            font=self.fonts["subtle"],
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
            font=self.fonts["mono_small"],
        ).pack(fill=X, anchor="w", pady=(12, 0))

    def metric_card(self, parent: ttk.Frame, label: str, value: StringVar, color: str, row: int, column: int) -> None:
        card = Frame(parent, bg=COLORS["panel"], highlightbackground=COLORS["line"], highlightthickness=1)
        card.grid(row=row, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0), pady=(0, 8))
        parent.columnconfigure(column, weight=1)
        parent.rowconfigure(row, minsize=64)
        Label(card, textvariable=value, bg=COLORS["panel"], fg=color, font=self.fonts["metric"]).pack(anchor="w", padx=10, pady=(6, 0))
        Label(card, text=label, bg=COLORS["panel"], fg=COLORS["muted"], font=self.fonts["subtle"]).pack(anchor="w", padx=10, pady=(0, 8))

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
        detail.configure(height=self.detail_height())
        detail.columnconfigure(0, weight=1)
        self.detail_frame = detail
        ttk.Label(detail, textvariable=self.detail_title, style="Title.TLabel", font=self.fonts["detail_title"]).grid(row=0, column=0, sticky="w")
        self.detail_body_label = Label(detail, textvariable=self.detail_body, bg=COLORS["panel"], fg=COLORS["muted"], font=self.fonts["subtle"], justify=LEFT, anchor="nw", wraplength=820)
        self.detail_body_label.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        detail.bind("<Configure>", self.on_detail_resize)

    def create_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        columns = ("side", "action", "ticker", "name", "latest", "trigger", "capital", "quality", "score", "target", "first_manage", "stop", "sellable_hit", "touch_hit", "manage_hit", "samples", "sample_bucket", "pnl", "edge", "reason")
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
        headings = {
            "side": "方向",
            "action": "动作",
            "ticker": "代码",
            "name": "名称",
            "latest": "最新",
            "trigger": "触发/成本",
            "capital": "资金%",
            "quality": "质量",
            "score": "分数",
            "target": "目标",
            "first_manage": "管理线",
            "stop": "止损",
            "sellable_hit": "可卖上沿",
            "touch_hit": "触及上沿",
            "manage_hit": "管理线%",
            "samples": "N",
            "sample_bucket": "样本桶",
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
            "capital": 72,
            "quality": 70,
            "score": 58,
            "target": 76,
            "first_manage": 78,
            "stop": 76,
            "sellable_hit": 78,
            "touch_hit": 78,
            "manage_hit": 78,
            "samples": 52,
            "sample_bucket": 150,
            "pnl": 72,
            "edge": 64,
            "reason": 360,
        }
        numeric = {"latest", "trigger", "capital", "score", "target", "first_manage", "stop", "samples", "pnl", "edge"}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="e" if column in numeric else "w", stretch=column == "reason")
        tree.tag_configure("urgent", background=COLORS["sell_bg"], foreground=COLORS["sell"])
        tree.tag_configure("buy", background=COLORS["buy_bg"], foreground=COLORS["buy"])
        tree.tag_configure("hold", background=COLORS["warn_bg"], foreground=COLORS["warn"])
        tree.tag_configure("watch", background=COLORS["panel"], foreground=COLORS["ink"])
        tree.bind("<<TreeviewSelect>>", self.on_select)
        self.bind_stock_tree_interactions(tree)
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
            font=self.fonts["empty"],
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
        try:
            payload = json.loads(self.latest_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.append_scan_log(f"无法加载上次扫描缓存：{exc}")
            return {}
        return payload if isinstance(payload, dict) else {}

    def load_cached_scan_result(self) -> None:
        payload = self.read_latest_payload()
        if not payload:
            return
        buy = payload.get("buy", [])
        sell = payload.get("sell", [])
        buy_items = buy if isinstance(buy, list) else []
        sell_items = sell if isinstance(sell, list) else []
        self.render_payload(payload)
        generated = str(payload.get("generated_at", "-"))
        phase = str(payload.get("phase", "-"))
        self.last_scan.set(f"上次扫描：{generated}")
        self.phase_text.set(f"阶段：{self.phase_label(phase)}")
        trade_items = self.trade_items(buy_items, sell_items)
        if trade_items:
            self.set_status("已加载上次动作", COLORS["warn_bg"], COLORS["warn"])
        else:
            self.set_status("已加载上次结果", COLORS["blue_bg"], COLORS["blue"])
        self.set_scan_progress(100, "已加载上次扫描")
        self.append_scan_log(f"已加载上次扫描结果：{generated}")

    def process_queue(self) -> None:
        self._process_queue_after_id = None
        if self._closing:
            return
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
        self.schedule_process_queue()

    def schedule_process_queue(self) -> None:
        if self._closing:
            return
        try:
            self._process_queue_after_id = self.root.after(250, self.process_queue)
        except Exception:
            self._process_queue_after_id = None

    def on_root_destroy(self, event: object) -> None:
        if getattr(event, "widget", None) is not self.root:
            return
        self._closing = True
        after_id = self._process_queue_after_id
        self._process_queue_after_id = None
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass

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

    @staticmethod
    def normalize_ticker(value: object) -> str:
        digits = "".join(ch for ch in str(value).strip() if ch.isdigit())
        return digits[:6]

    @classmethod
    def xueqiu_symbol(cls, value: object) -> str:
        ticker = cls.normalize_ticker(value)
        if not ticker:
            return ""
        if ticker.startswith(("6", "9")):
            return f"SH{ticker}"
        if ticker.startswith(("4", "8")):
            return f"BJ{ticker}"
        return f"SZ{ticker}"

    @classmethod
    def xueqiu_url(cls, value: object) -> str:
        symbol = cls.xueqiu_symbol(value)
        return f"https://xueqiu.com/S/{symbol}" if symbol else ""

    @staticmethod
    def tree_column_name(tree: ttk.Treeview, x: int) -> str:
        column_id = tree.identify_column(x)
        if not column_id or column_id == "#0":
            return ""
        try:
            index = int(column_id.lstrip("#")) - 1
        except ValueError:
            return ""
        columns = list(tree["columns"])
        return str(columns[index]) if 0 <= index < len(columns) else ""

    @staticmethod
    def tree_ticker(tree: ttk.Treeview, item_id: str) -> str:
        columns = list(tree["columns"])
        values = list(tree.item(item_id, "values") or [])
        if "ticker" not in columns:
            return ""
        index = columns.index("ticker")
        return str(values[index]) if index < len(values) else ""

    def copy_ticker_to_clipboard(self, ticker_text: object) -> None:
        ticker = self.normalize_ticker(ticker_text)
        if not ticker:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(ticker)
        self.root.update()
        self.append_scan_log(f"已复制股票代码，正在跳转国盛睿：{ticker}")
        result = jump_guoshengrui_for_ticker(ticker)
        self.append_scan_log(result.message)
        if not result.ok and result.code in {"missing_executable", "no_window", "dangerous_foreground"}:
            messagebox.showwarning("国盛睿跳转", result.message)

    def open_xueqiu_for_ticker(self, ticker_text: object) -> None:
        symbol = self.xueqiu_symbol(ticker_text)
        if not symbol:
            return
        webbrowser.open_new_tab(f"https://xueqiu.com/S/{symbol}")
        self.append_scan_log(f"已打开雪球：{symbol}")

    @staticmethod
    def trade_side_from_text(side_text: object, action_text: object = "") -> str:
        side = str(side_text or "")
        action = str(action_text or "")
        if side == "买入" or action == "BUY_NOW":
            return "buy"
        return "sell"

    @staticmethod
    def parse_float(value: object, default: float = 0.0) -> float:
        try:
            text = str(value or "").replace(",", "").replace("%", "").strip()
            if not text:
                return default
            return float(text)
        except (TypeError, ValueError):
            return default

    def load_broker_account_snapshot(self) -> dict[str, float]:
        try:
            data = json.loads(self.account_snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            return {"cash_available": 0.0, "holdings_value": 0.0, "total_assets": 0.0}
        cash_available = self.parse_float(data.get("cash_available"))
        holdings_value = self.parse_float(data.get("holdings_value"))
        total_assets = self.parse_float(data.get("total_assets"), cash_available + holdings_value)
        if total_assets <= 0:
            total_assets = cash_available + holdings_value
        return {
            "cash_available": cash_available,
            "holdings_value": holdings_value,
            "total_assets": total_assets,
        }

    def open_position_shares(self, ticker_text: object) -> float:
        ticker = self.normalize_ticker(ticker_text)
        if not ticker or not self.positions_csv.exists():
            return 0.0
        try:
            with self.positions_csv.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = csv.DictReader(handle)
                total = 0.0
                for row in rows:
                    if self.normalize_ticker(row.get("ticker")) != ticker:
                        continue
                    if str(row.get("status") or "open").strip().lower() == "closed":
                        continue
                    total += self.parse_float(row.get("shares"))
                return total
        except Exception:
            return 0.0

    def open_trade_terminal_for_tree_selection(
        self,
        tree: ttk.Treeview,
        item_by_iid: dict[str, dict[str, object]] | None = None,
        cash_var: StringVar | None = None,
        holdings_var: StringVar | None = None,
        total_var: StringVar | None = None,
    ) -> None:
        selected = tree.selection()
        if not selected:
            messagebox.showinfo("交易界面跳转", "请先选中一条买入或卖出提醒。")
            return
        selected_id = str(selected[0])
        source_item = item_by_iid.get(selected_id, {}) if item_by_iid else {}
        values = list(tree.item(selected[0], "values") or [])
        if len(values) < 3:
            messagebox.showwarning("交易界面跳转", "选中行缺少股票代码。")
            return
        side = source_item.get("side", values[0])
        action = source_item.get("action", values[1])
        ticker = source_item.get("ticker", values[2])
        trade_side = self.trade_side_from_text(side, action)
        ticker_code = self.normalize_ticker(ticker)
        if len(ticker_code) != 6:
            messagebox.showwarning("交易界面跳转", "股票代码必须是 6 位数字。")
            return
        cash_amount = self.parse_float(cash_var.get() if cash_var is not None else 0.0)
        holdings_value = self.parse_float(holdings_var.get() if holdings_var is not None else 0.0)
        total_assets = self.parse_float(total_var.get() if total_var is not None else 0.0)
        if total_assets <= 0:
            total_assets = cash_amount + holdings_value
        reference_price = self.parse_float(source_item.get("latest_price"), self.parse_float(values[4] if len(values) > 4 else 0.0))
        suggested_capital_pct = self.parse_float(source_item.get("suggested_capital_pct"), self.parse_float(values[6] if len(values) > 6 else 0.0))
        self.root.clipboard_clear()
        self.root.clipboard_append(ticker_code)
        self.root.update()
        result = jump_guoshengrui_trade_for_ticker(
            ticker_code,
            trade_side,
            account_cash_amount=cash_amount,
            account_holdings_value=holdings_value,
            account_total_assets=total_assets,
            reference_price=reference_price,
            suggested_capital_pct=suggested_capital_pct,
            existing_shares=self.open_position_shares(ticker_code),
            fill_quantity=trade_side == "buy" and total_assets > 0,
        )
        self.append_scan_log(result.message)
        if not result.ok:
            messagebox.showwarning("交易界面跳转", result.message)

    def bind_stock_tree_interactions(self, tree: ttk.Treeview) -> None:
        tree.bind("<ButtonRelease-1>", self.on_stock_tree_click, add="+")
        tree.bind("<Motion>", self.on_stock_tree_motion, add="+")
        tree.bind("<Leave>", lambda _event: tree.configure(cursor=""), add="+")

    def on_stock_tree_click(self, event: object) -> None:
        tree = getattr(event, "widget", None)
        if not isinstance(tree, ttk.Treeview):
            return
        if tree.identify_region(event.x, event.y) != "cell":
            return
        item_id = tree.identify_row(event.y)
        if not item_id:
            return
        column = self.tree_column_name(tree, event.x)
        ticker = self.tree_ticker(tree, item_id)
        if column == "ticker":
            self.copy_ticker_to_clipboard(ticker)
        elif column == "name":
            self.open_xueqiu_for_ticker(ticker)

    def on_stock_tree_motion(self, event: object) -> None:
        tree = getattr(event, "widget", None)
        if not isinstance(tree, ttk.Treeview):
            return
        column = self.tree_column_name(tree, event.x)
        item_id = tree.identify_row(event.y)
        tree.configure(cursor="hand2" if item_id and column in {"ticker", "name"} else "")

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
                "",
                "",
                "",
                self.fmt(row.get("target_price")),
                self.fmt(row.get("first_manage_price")),
                self.fmt(row.get("hard_stop_price")),
                "",
                "",
                "",
                "",
                "",
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
                f"{self.fmt(row.get('suggested_capital_pct'))}%",
                f"{row.get('position_quality_grade', '')}/{self.fmt(row.get('position_quality_score'))}",
                self.fmt(row.get("score")),
                self.fmt(row.get("target_price")),
                self.fmt(row.get("first_manage_price")),
                self.fmt(row.get("hard_stop_price")),
                self.hit_rate_fmt(row.get("target_upper_hit_rate_pct")),
                self.hit_rate_fmt(row.get("target_upper_touch_rate_pct")),
                self.hit_rate_fmt(row.get("first_manage_hit_rate_pct")),
                self.fmt(row.get("hit_rate_sample_size")),
                row.get("hit_rate_bucket", ""),
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
        ttk.Label(panel, text="按钮先跳转分时图，再右键打开国盛睿闪电买/卖窗口；买入按账户总资产比例填数量，不自动下单。", style="Subtle.TLabel").pack(anchor="w", pady=(4, 10))
        snapshot = self.load_broker_account_snapshot()
        cash_var = StringVar(value=f"{snapshot['cash_available']:.2f}" if snapshot["cash_available"] > 0 else "")
        holdings_var = StringVar(value=f"{snapshot['holdings_value']:.2f}" if snapshot["holdings_value"] > 0 else "")
        total_var = StringVar(value=f"{snapshot['total_assets']:.2f}" if snapshot["total_assets"] > 0 else "")
        if any(item.get("side") == "买入" for item in new_items):
            account_row = ttk.Frame(panel, style="Panel.TFrame")
            account_row.pack(fill=X, pady=(0, 10))

            def update_total(*_args: object) -> None:
                total = self.parse_float(cash_var.get()) + self.parse_float(holdings_var.get())
                total_var.set(f"{total:.2f}" if total > 0 else "")

            def scan_account_snapshot() -> None:
                result = export_guoshengrui_holdings()
                if not result.ok:
                    messagebox.showwarning("账户扫描失败", result.message)
                    self.append_scan_log(result.message)
                    return
                summary = sync_holdings_to_csv(self.positions_csv, result)
                cash_var.set(f"{summary.cash_available:.2f}")
                holdings_var.set(f"{summary.holdings_value:.2f}")
                total_var.set(f"{summary.total_assets:.2f}")
                self.append_scan_log(summary.message)

            cash_var.trace_add("write", update_total)
            holdings_var.trace_add("write", update_total)
            ttk.Label(account_row, text="可用现金", style="Subtle.TLabel").pack(side=LEFT)
            ttk.Entry(account_row, textvariable=cash_var, width=14).pack(side=LEFT, padx=(6, 12))
            ttk.Label(account_row, text="持仓市值", style="Subtle.TLabel").pack(side=LEFT)
            ttk.Entry(account_row, textvariable=holdings_var, width=14).pack(side=LEFT, padx=(6, 12))
            ttk.Label(account_row, text="总资产", style="Subtle.TLabel").pack(side=LEFT)
            ttk.Entry(account_row, textvariable=total_var, width=14).pack(side=LEFT, padx=(6, 12))
            ttk.Button(account_row, text="扫描账户", command=scan_account_snapshot, style="Quiet.TButton").pack(side=LEFT)
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
        self.bind_stock_tree_interactions(tree)
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        item_by_iid: dict[str, dict[str, object]] = {}
        for item in new_items:
            tag = "buy" if item.get("side") == "买入" else "urgent"
            iid = tree.insert("", END, values=(item.get("side", ""), item.get("action", ""), item.get("ticker", ""), item.get("name", ""), self.fmt(item.get("latest_price")), item.get("reason", "")), tags=(tag,))
            item_by_iid[str(iid)] = dict(item)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        buttons = ttk.Frame(panel, style="Panel.TFrame")
        buttons.pack(fill=X)
        ttk.Button(buttons, text="打开最新计划", command=self.open_latest_plan, style="Primary.TButton").pack(side=LEFT)
        ttk.Button(
            buttons,
            text="打开选中交易界面",
            command=lambda: self.open_trade_terminal_for_tree_selection(tree, item_by_iid, cash_var, holdings_var, total_var),
            style="Primary.TButton",
        ).pack(side=LEFT, padx=(8, 0))
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
        side, action, ticker, name, latest, trigger, capital, quality, score, target, first_manage, stop, sellable_hit, touch_hit, manage_hit, samples, sample_bucket, pnl, edge, reason = values
        self.detail_title.set(f"{side} {action} - {ticker} {name}")
        pieces = [
            f"最新价：{latest or '-'}",
            f"触发/成本：{trigger or '-'}",
            f"建议资金：{capital or '-'}",
            f"质量：{quality or '-'}",
            f"分数：{score or '-'}",
            f"目标价：{target or '-'}",
            f"第一管理线：{first_manage or '-'}",
            f"止损价：{stop or '-'}",
        ]
        if sellable_hit or touch_hit or manage_hit:
            pieces.append(f"12M概率：可卖上沿 {sellable_hit or '-'} / 触及上沿 {touch_hit or '-'} / 管理线 {manage_hit or '-'} / N={samples or '-'} / 桶={sample_bucket or '-'}")
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

    def sync_positions_from_guoshengrui(self) -> None:
        result = export_guoshengrui_holdings()
        if not result.ok:
            messagebox.showwarning("国盛睿持仓同步失败", result.message)
            self.append_scan_log(result.message)
            return
        summary = sync_holdings_to_csv(self.positions_csv, result)
        self.append_scan_log(summary.message)
        messagebox.showinfo("国盛睿持仓同步完成", summary.message)

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
        self.update_started_at = dt.datetime.now()
        self.update_button.state(["disabled"])
        self.set_update_event("正在检查", "正在检查 GitHub Release，请稍等...")
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
            "-QuietCheckIntervalHours",
            "0",
        ]
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            result = subprocess.run(command, text=True, capture_output=True, cwd=self.install_dir, creationflags=creationflags, timeout=60)
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
        except subprocess.TimeoutExpired as exc:
            self.event_queue.put(("update_done", {"returncode": 1, "stdout": exc.stdout or "", "stderr": "检查更新超时，请稍后重试或检查网络。"}))
        except Exception as exc:
            self.event_queue.put(("update_done", {"returncode": 1, "stdout": "", "stderr": str(exc)}))

    def on_update_done(self, result: dict[str, object]) -> None:
        self.update_in_progress = False
        elapsed = ""
        if self.update_started_at is not None:
            elapsed_seconds = max(0, int((dt.datetime.now() - self.update_started_at).total_seconds()))
            elapsed = f"（用时 {elapsed_seconds} 秒）"
        self.update_started_at = None
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
                detail = f"{message}，重启后使用新版。{elapsed}"
                self.set_update_event("已更新，需重启", detail)
                messagebox.showinfo("更新完成", detail)
            else:
                detail = f"{message}{elapsed}"
                self.set_update_event("检查完成", detail)
                messagebox.showinfo("检查更新", detail)
        else:
            detail = f"{message}{elapsed}"
            self.set_update_event("更新失败", detail)
            messagebox.showerror("更新失败", detail)

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

    @staticmethod
    def hit_rate_fmt(value: object) -> str:
        if value in (None, ""):
            return "样本不足"
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return str(value)


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
