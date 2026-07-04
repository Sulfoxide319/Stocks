#!/usr/bin/env python3
"""PySide6 desktop shell for the A-share trading assistant."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app_storage import (
    Position,
    app_data_dir,
    connect,
    default_db_path,
    delete_position,
    get_setting,
    import_positions_csv,
    list_positions,
    load_latest_snapshot,
    load_latest_snapshot_excluding,
    migrate_legacy_files,
    save_position,
    set_setting,
)
from short_term_live_monitor import fetch_sina_quote


try:
    from PySide6.QtCore import Qt, QThread, QTimer, Signal
    from PySide6.QtGui import QAction, QFont
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QProgressBar,
        QSpinBox,
        QSplitter,
        QStatusBar,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - only reached before dependencies are installed.
    raise SystemExit("PySide6 is required. Install dependencies or use the packaged EXE.") from exc


ADVICE_COLUMNS = ["方向", "动作", "状态", "代码", "名称", "最新", "触发/成本", "建议资金", "质量", "目标上沿", "第一管理线", "移动止盈", "止损", "VWAP/成本", "可卖上沿", "触及上沿", "管理线", "样本数", "样本桶", "盈亏", "Edge", "理由"]
POSITION_COLUMNS = ["代码", "名称", "买入日期", "买入时间", "成本", "数量", "目标上沿", "止损", "回撤%", "最高", "管理状态", "状态", "备注"]
REPOSITORY = "Sulfoxide319/Stocks"
AUTOSTART_VALUE_NAME = "StocksTradingAssistant"
LIVE_WATCH_LIMIT = 30
DEFAULT_LIVE_WATCH_NORMAL_SECONDS = 30
DEFAULT_LIVE_WATCH_FAST_SECONDS = 5
DEFAULT_LIVE_WATCH_NEAR_THRESHOLD_PCT = 0.25
DEFAULT_LIVE_WATCH_TRIGGER_ADJUST_PCT = 0.0
LIVE_WATCH_MIN_NEAR_TICKS = 3
ASHARE_TICK_SIZE = 0.01
URGENT_SELL_ACTIONS = {"SELL_NOW", "TAKE_PROFIT", "REDUCE_PROFIT", "MANAGE_PROFIT", "TRAIL_SELL", "VWAP_WEAK_SELL", "PRE_CLOSE_REDUCE"}


class NullTextWriter:
    encoding = "utf-8"
    errors = "replace"

    def write(self, value: object) -> int:
        return len(str(value))

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def ensure_text_stdio() -> None:
    if sys.stdout is None:
        sys.stdout = NullTextWriter()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = NullTextWriter()  # type: ignore[assignment]


ensure_text_stdio()

from local_trading_assistant import build_arg_parser, phase_for_time, run_once


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        package_root = exe_dir.parent
        if exe_dir.name.lower() == "app" and any(
            (package_root / marker).exists()
            for marker in ("VERSION", "update_manifest.json", "Start-TradingAssistant.bat")
        ):
            return package_root
        return exe_dir
    return Path(__file__).resolve().parent


def parse_version_text(value: str) -> tuple[int, ...]:
    clean = value.strip().lstrip("v")
    parts: list[int] = []
    for part in clean.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts or [0])


def read_installed_version(root: Path) -> str:
    version_path = root / "VERSION"
    if version_path.exists():
        return version_path.read_text(encoding="utf-8").strip()
    return "0.0.0"


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    installed_exe = Path(os.environ.get("LOCALAPPDATA", "")) / "StocksTradingAssistant" / "StocksTradingAssistant.exe"
    if installed_exe.exists():
        return f'"{installed_exe}"'
    return f'"{Path(sys.executable).resolve()}" "{Path(__file__).resolve()}"'


def set_windows_autostart(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                return


def is_windows_autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
    except FileNotFoundError:
        return False
    return bool(str(value).strip())


def is_live_watch_window(now: dt.datetime) -> bool:
    if now.weekday() >= 5:
        return False
    current = now.time()
    return dt.time(9, 30) <= current <= dt.time(11, 30) or dt.time(13, 0) <= current <= dt.time(14, 57)


def latest_payload_trade_date(payload: dict[str, Any]) -> dt.date | None:
    raw = str(payload.get("date") or "")
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def scan_debug_log_path() -> Path:
    return app_data_dir() / "logs" / f"scan_debug_{dt.date.today():%Y%m%d}.log"


def write_scan_debug_log(message: str) -> Path:
    path = scan_debug_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")
    return path


def fetch_latest_release(repository: str = REPOSITORY, timeout: int = 12) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "StocksTradingAssistant",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_public_latest_release(repository: str = REPOSITORY, timeout: int = 12) -> dict[str, Any]:
    latest_url = f"https://github.com/{repository}/releases/latest"
    latest_request = urllib.request.Request(latest_url, headers={"User-Agent": "StocksTradingAssistant"})
    with urllib.request.urlopen(latest_request, timeout=timeout) as response:
        resolved_url = response.geturl()
        body = response.read().decode("utf-8", errors="replace")
    tag_match = re.search(r"/releases/tag/([^/?#]+)", resolved_url)
    if not tag_match:
        tag_match = re.search(rf"/{re.escape(repository)}/releases/tag/([^\"?#<]+)", body)
    if not tag_match:
        raise RuntimeError("无法从 GitHub Releases 页面读取最新版本。")
    tag = urllib.parse.unquote(tag_match.group(1))

    assets_url = f"https://github.com/{repository}/releases/expanded_assets/{tag}"
    assets_request = urllib.request.Request(assets_url, headers={"User-Agent": "StocksTradingAssistant"})
    with urllib.request.urlopen(assets_request, timeout=timeout) as response:
        assets_body = response.read().decode("utf-8", errors="replace")
    asset_url = ""
    pattern = rf'href="/{re.escape(repository)}/releases/download/{re.escape(tag)}/([^"]+)"'
    for match in re.finditer(pattern, assets_body):
        name = urllib.parse.unquote(match.group(1))
        if name.startswith("StocksTradingAssistant-v") and name.endswith(".zip"):
            asset_url = f"https://github.com/{repository}/releases/download/{tag}/{name}"
            break
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/{repository}/releases/tag/{tag}",
        "assets": [{"name": asset_url.rsplit("/", 1)[-1], "browser_download_url": asset_url}] if asset_url else [],
    }


def check_latest_update(root: Path) -> dict[str, Any]:
    current = read_installed_version(root)
    try:
        release = fetch_latest_release()
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 404}:
            try:
                release = fetch_public_latest_release()
            except Exception as fallback_exc:
                if exc.code == 403:
                    return {"ok": False, "message": f"GitHub 暂时限制了更新检查请求，公开页面 fallback 也失败：{fallback_exc}"}
                return {"ok": False, "message": f"没有找到 GitHub 最新版本，公开页面 fallback 也失败：{fallback_exc}"}
        else:
            return {"ok": False, "message": f"检查更新失败：HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "message": f"检查更新失败：{exc}"}

    latest = str(release.get("tag_name") or "v0.0.0")
    assets = release.get("assets") or []
    asset_url = ""
    for asset in assets:
        if str(asset.get("name", "")).startswith("StocksTradingAssistant-v") and str(asset.get("name", "")).endswith(".zip"):
            asset_url = str(asset.get("browser_download_url") or "")
            break
    update_available = parse_version_text(latest) > parse_version_text(current)
    if update_available:
        message = f"发现新版本 {latest}，当前版本 v{current}。"
    else:
        message = f"已经是最新版本：v{current}。"
    return {
        "ok": True,
        "message": message,
        "current": current,
        "latest": latest,
        "update_available": update_available,
        "release_url": str(release.get("html_url") or ""),
        "asset_url": asset_url,
    }


def run_internal_monitor(argv: list[str]) -> int:
    import short_term_live_monitor

    previous = sys.argv[:]
    try:
        sys.argv = ["short_term_live_monitor.py", *argv]
        return int(short_term_live_monitor.main() or 0)
    finally:
        sys.argv = previous


def build_scan_args(root: Path, phase: str, db_path: Path, trace: Any) -> argparse.Namespace:
    trace(9, "准备扫描参数：计算输出目录")
    out_dir = app_data_dir() / "output" / "trading_assistant"
    watchlist_path = root / "config" / "watchlist.mainboard_liquid.csv"
    trace(9, f"准备扫描参数：输出目录 {out_dir}")
    trace(9, f"准备扫描参数：数据库 {db_path}")
    trace(9, f"准备扫描参数：股票池 {watchlist_path}")
    if not watchlist_path.exists():
        raise FileNotFoundError(f"股票池文件不存在：{watchlist_path}")
    argv = [
        "--once",
        "--phase",
        phase,
        "--watchlist",
        str(watchlist_path),
        "--out-dir",
        str(out_dir),
        "--db",
        str(db_path),
        "--app-db",
        str(db_path),
        "--use-app-db",
    ]
    trace(9, "准备扫描参数：检查打包运行模式")
    if getattr(sys, "frozen", False):
        argv.extend(["--python", sys.executable, "--monitor-script=--run-monitor"])
        trace(9, f"准备扫描参数：打包 EXE {sys.executable}")
    trace(9, "准备扫描参数：解析命令参数")
    args = build_arg_parser().parse_args(argv)
    trace(9, "准备扫描参数：解析完成")
    return args


class ScanWorker(QThread):
    progress = Signal(int, str)
    log = Signal(str)
    finished_payload = Signal(dict)
    failed = Signal(str)

    def __init__(self, root: Path, phase: str, db_path: Path) -> None:
        super().__init__()
        self.root = root
        self.phase = phase
        self.db_path = db_path
        self.python_thread_id: int | None = None

    def trace(self, percent: int, message: str) -> None:
        write_scan_debug_log(f"[scan-worker] {message}")
        self.progress.emit(percent, message)

    def run(self) -> None:
        self.python_thread_id = threading.get_ident()
        self.trace(7, f"准备加载扫描模块；thread={self.python_thread_id}")
        import local_trading_assistant

        self.trace(8, "扫描模块已加载")
        previous_emit = local_trading_assistant.emit_progress
        local_trading_assistant.emit_progress = lambda percent, message: self.trace(int(percent), str(message))
        try:
            args = build_scan_args(self.root, self.phase, self.db_path, self.trace)
            self.log.emit(f"开始 {self.phase} 扫描")
            self.trace(10, "进入扫描流程")
            run_once(args, self.root)
            self.trace(97, "读取最新扫描结果")
            with connect(self.db_path) as conn:
                payload = load_latest_snapshot(conn)
            self.finished_payload.emit(payload)
        except BaseException as exc:  # pragma: no cover - exercised through GUI flow.
            detail = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                stdout = str(exc.output or "").strip()
                stderr = str(exc.stderr or "").strip()
                parts = [detail]
                if stdout:
                    parts.append(f"stdout:\n{stdout[-3000:]}")
                if stderr:
                    parts.append(f"stderr:\n{stderr[-3000:]}")
                detail = "\n\n".join(parts)
            write_scan_debug_log(f"[scan-worker] 扫描异常：{type(exc).__name__}: {detail}")
            write_scan_debug_log(traceback.format_exc().rstrip())
            self.failed.emit(detail)
        finally:
            local_trading_assistant.emit_progress = previous_emit
            write_scan_debug_log("[scan-worker] 扫描线程退出")


class UpdateWorker(QThread):
    finished_update = Signal(dict)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    def run(self) -> None:
        self.finished_update.emit(check_latest_update(self.root))


class CandidateWatchWorker(QThread):
    finished_watch = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        trigger_adjust_pct: float,
        near_threshold_pct: float,
        limit: int = LIVE_WATCH_LIMIT,
    ) -> None:
        super().__init__()
        self.payload = json.loads(json.dumps(payload, ensure_ascii=False))
        self.trigger_adjust_pct = trigger_adjust_pct
        self.near_threshold_pct = near_threshold_pct
        self.limit = limit

    @staticmethod
    def _float(value: object, default: float = 0.0) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def run(self) -> None:
        try:
            now = dt.datetime.now()
            session = __import__("requests").Session()
            buys = [item for item in self.payload.get("buy") or [] if isinstance(item, dict) and item.get("ticker")]
            watched = 0
            triggered = 0
            near_threshold = 0
            unavailable = 0
            closest_distance_pct: float | None = None
            for item in buys[: self.limit]:
                if item.get("buy_enabled") is False:
                    item["action"] = "WATCH_BUY"
                    item["reason"] = "监听：观察池标的低于买入分数线，仅跟踪，不触发买入"
                    continue
                quote = fetch_sina_quote(session, str(item.get("ticker") or ""))
                if not quote:
                    unavailable += 1
                    item["reason"] = "监听：实时行情暂不可用，保留上次判断"
                    continue
                watched += 1
                latest = round(float(quote.price), 4)
                base_trigger = self._float(item.get("trigger_price"), self._float(item.get("latest_price")))
                trigger = base_trigger * (1 + self.trigger_adjust_pct / 100) if base_trigger > 0 else 0.0
                distance_pct = (trigger / latest - 1) * 100 if trigger > 0 and latest > 0 else 999.0
                min_tick_pct = LIVE_WATCH_MIN_NEAR_TICKS * ASHARE_TICK_SIZE / latest * 100 if latest > 0 else 0.0
                effective_near_pct = max(self.near_threshold_pct, min_tick_pct)
                closest_distance_pct = distance_pct if closest_distance_pct is None else min(closest_distance_pct, distance_pct)
                item["latest_price"] = latest
                item["effective_trigger_price"] = round(trigger, 4)
                item["effective_near_threshold_pct"] = round(effective_near_pct, 4)
                item["watched_at"] = quote.timestamp or now.isoformat(timespec="seconds")
                if now.time() < dt.time(9, 45):
                    item["action"] = "WAIT"
                    item["reason"] = f"监听：{quote.timestamp} 最新 {latest:.2f}，有效阈值 {trigger:.2f}，9:45 前只观察"
                elif trigger > 0 and latest >= trigger:
                    item["action"] = "BUY_NOW"
                    item["reason"] = f"监听：{quote.timestamp} 最新 {latest:.2f} >= 有效阈值 {trigger:.2f}"
                    triggered += 1
                else:
                    if 0 <= distance_pct <= effective_near_pct:
                        near_threshold += 1
                    item["action"] = "WATCH_BUY"
                    item["reason"] = f"监听：{quote.timestamp} 最新 {latest:.2f}，距有效阈值 {trigger:.2f} 还差 {max(distance_pct, 0):.2f}%（接近线 {effective_near_pct:.2f}%）"
            self.payload["watched_at"] = now.isoformat(timespec="seconds")
            self.payload["watch_summary"] = {
                "watched": watched,
                "triggered": triggered,
                "near_threshold": near_threshold,
                "unavailable": unavailable,
                "limit": self.limit,
                "trigger_adjust_pct": self.trigger_adjust_pct,
                "near_threshold_pct": self.near_threshold_pct,
                "min_near_ticks": LIVE_WATCH_MIN_NEAR_TICKS,
                "closest_distance_pct": round(closest_distance_pct, 4) if closest_distance_pct is not None else None,
            }
            self.finished_watch.emit(self.payload)
        except BaseException as exc:  # pragma: no cover - surfaced in GUI log.
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.root = app_root()
        self.db_path = default_db_path()
        self.selected_position_id: int | None = None
        self.scan_worker: ScanWorker | None = None
        self.update_worker: UpdateWorker | None = None
        self.candidate_watch_worker: CandidateWatchWorker | None = None
        self.current_payload: dict[str, Any] = {}
        self.alerted_buy_keys: set[str] = set()
        self.alerted_sell_keys: set[str] = set()
        self.last_live_watch_skip_log_at: dt.datetime | None = None
        self.live_watch_fast_mode = False
        self.scan_started_at: dt.datetime | None = None
        self.scan_stage_text = ""
        self.scan_last_heartbeat_seconds = 0
        self.scan_last_stack_dump_seconds = 0
        self.last_auto_opening_scan_date: dt.date | None = None
        self.scan_timer = QTimer(self)
        self.scan_timer.setInterval(10_000)
        self.scan_timer.timeout.connect(self.on_scan_heartbeat)
        self.auto_scan_timer = QTimer(self)
        self.auto_scan_timer.setInterval(30_000)
        self.auto_scan_timer.timeout.connect(self.check_auto_opening_scan)
        self.candidate_watch_timer = QTimer(self)
        self.candidate_watch_timer.timeout.connect(self.check_live_candidate_watch)
        with connect(self.db_path) as conn:
            self.live_watch_enabled = get_setting(conn, "live_candidate_watch_enabled", "1") != "0"
            self.autostart_preferred = get_setting(conn, "autostart_enabled", "1") != "0"
            self.live_watch_normal_seconds = self._setting_int(conn, "live_watch_normal_seconds", DEFAULT_LIVE_WATCH_NORMAL_SECONDS, 10, 300)
            self.live_watch_fast_seconds = self._setting_int(conn, "live_watch_fast_seconds", DEFAULT_LIVE_WATCH_FAST_SECONDS, 3, 30)
            self.live_watch_near_threshold_pct = self._setting_float(conn, "live_watch_near_threshold_pct", DEFAULT_LIVE_WATCH_NEAR_THRESHOLD_PCT, 0.05, 5.0)
            self.live_watch_trigger_adjust_pct = self._setting_float(conn, "live_watch_trigger_adjust_pct", DEFAULT_LIVE_WATCH_TRIGGER_ADJUST_PCT, -5.0, 5.0)
            self.migration_notes = migrate_legacy_files(conn, self.root)
        self.candidate_watch_timer.setInterval(self.live_watch_normal_seconds * 1000)
        self.setWindowTitle("A股短线交易助手")
        self.resize(1280, 780)
        self._build_ui()
        self._apply_style()
        self.sync_autostart_preference()
        self.refresh_positions()
        self.load_cached_snapshot()
        for note in self.migration_notes:
            self.append_log(note)
        self.auto_scan_timer.start()
        self.candidate_watch_timer.start()
        QTimer.singleShot(1_000, self.check_auto_opening_scan)
        QTimer.singleShot(1_500, self.check_live_candidate_watch)

    def _build_ui(self) -> None:
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 14, 16, 14)
        root_layout.setSpacing(12)
        root_layout.addLayout(self._build_header())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_side_panel())
        splitter.addWidget(self._build_tabs())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

    def _build_header(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        title_box = QVBoxLayout()
        self.title_label = QLabel("A股短线交易助手")
        self.version_label = QLabel(self._version_text())
        title_box.addWidget(self.title_label)
        title_box.addWidget(self.version_label)
        layout.addLayout(title_box, 1)

        self.scan_button = QPushButton("立即扫描")
        self.scan_button.clicked.connect(self.start_scan)
        self.update_button = QPushButton("检查更新")
        self.update_button.clicked.connect(self.check_update)
        layout.addWidget(self.scan_button)
        layout.addWidget(self.update_button)
        return layout

    def _build_side_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(310)
        panel.setMaximumWidth(420)
        layout = QVBoxLayout(panel)
        self.status_label = QLabel("待机")
        self.phase_label = QLabel("阶段：-")
        self.last_scan_label = QLabel("上次扫描：-")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.autostart_checkbox = QCheckBox("开机自启动")
        self.autostart_checkbox.toggled.connect(self.on_autostart_toggled)
        self.live_watch_checkbox = QCheckBox("盘中30秒监听候选股")
        self.live_watch_checkbox.setChecked(self.live_watch_enabled)
        self.live_watch_checkbox.toggled.connect(self.on_live_watch_toggled)
        self.update_live_watch_checkbox_text()
        watch_form = QFormLayout()
        watch_form.setContentsMargins(0, 0, 0, 0)
        self.trigger_adjust_spin = QDoubleSpinBox()
        self.trigger_adjust_spin.setRange(-5.0, 5.0)
        self.trigger_adjust_spin.setDecimals(2)
        self.trigger_adjust_spin.setSingleStep(0.1)
        self.trigger_adjust_spin.setSuffix("%")
        self.trigger_adjust_spin.setValue(self.live_watch_trigger_adjust_pct)
        self.trigger_adjust_spin.editingFinished.connect(self.on_live_watch_settings_changed)
        self.near_threshold_spin = QDoubleSpinBox()
        self.near_threshold_spin.setRange(0.05, 5.0)
        self.near_threshold_spin.setDecimals(2)
        self.near_threshold_spin.setSingleStep(0.05)
        self.near_threshold_spin.setSuffix("%")
        self.near_threshold_spin.setValue(self.live_watch_near_threshold_pct)
        self.near_threshold_spin.editingFinished.connect(self.on_live_watch_settings_changed)
        self.normal_interval_spin = QSpinBox()
        self.normal_interval_spin.setRange(10, 300)
        self.normal_interval_spin.setSuffix(" 秒")
        self.normal_interval_spin.setValue(self.live_watch_normal_seconds)
        self.normal_interval_spin.editingFinished.connect(self.on_live_watch_settings_changed)
        self.fast_interval_spin = QSpinBox()
        self.fast_interval_spin.setRange(3, 30)
        self.fast_interval_spin.setSuffix(" 秒")
        self.fast_interval_spin.setValue(self.live_watch_fast_seconds)
        self.fast_interval_spin.editingFinished.connect(self.on_live_watch_settings_changed)
        watch_form.addRow("触发价调整", self.trigger_adjust_spin)
        watch_form.addRow("接近阈值基准", self.near_threshold_spin)
        watch_form.addRow("普通监听", self.normal_interval_spin)
        watch_form.addRow("快速监听", self.fast_interval_spin)
        quick_form = QFormLayout()
        quick_form.setContentsMargins(0, 0, 0, 0)
        self.quick_ticker_input = QLineEdit()
        self.quick_ticker_input.setPlaceholderText("例如 000725")
        self.quick_buy_price_spin = QDoubleSpinBox()
        self.quick_buy_price_spin.setRange(0.01, 9999.99)
        self.quick_buy_price_spin.setDecimals(3)
        self.quick_buy_price_spin.setSingleStep(0.01)
        self.quick_buy_price_spin.setValue(1.0)
        self.quick_shares_spin = QSpinBox()
        self.quick_shares_spin.setRange(1, 10_000_000)
        self.quick_shares_spin.setSingleStep(100)
        self.quick_shares_spin.setValue(100)
        quick_form.addRow("代码", self.quick_ticker_input)
        quick_form.addRow("买入价", self.quick_buy_price_spin)
        quick_form.addRow("数量", self.quick_shares_spin)
        quick_buttons = QHBoxLayout()
        self.quick_fill_button = QPushButton("填入选中")
        self.quick_fill_button.clicked.connect(self.fill_quick_position_from_selection)
        self.quick_save_button = QPushButton("同步持仓库")
        self.quick_save_button.clicked.connect(self.save_quick_position)
        quick_buttons.addWidget(self.quick_fill_button)
        quick_buttons.addWidget(self.quick_save_button)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(180)
        layout.addWidget(self.status_label)
        layout.addWidget(self.phase_label)
        layout.addWidget(self.last_scan_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.autostart_checkbox)
        layout.addWidget(self.live_watch_checkbox)
        layout.addLayout(watch_form)
        layout.addWidget(QLabel("快速登记持仓"))
        layout.addLayout(quick_form)
        layout.addLayout(quick_buttons)
        layout.addWidget(QLabel("运行日志"))
        layout.addWidget(self.log_box, 1)
        return panel

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        advice_page = QWidget()
        advice_layout = QVBoxLayout(advice_page)
        self.advice_table = QTableWidget(0, len(ADVICE_COLUMNS))
        self.advice_table.setHorizontalHeaderLabels(ADVICE_COLUMNS)
        self.advice_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.advice_table.horizontalHeader().setStretchLastSection(True)
        self.advice_table.setAlternatingRowColors(True)
        self.advice_table.itemSelectionChanged.connect(self.on_advice_selected)
        advice_layout.addWidget(self.advice_table)
        self.detail_label = QLabel("选择一条建议查看详情。")
        self.detail_label.setWordWrap(True)
        advice_layout.addWidget(self.detail_label)
        tabs.addTab(advice_page, "扫描结果")

        positions_page = QWidget()
        positions_layout = QVBoxLayout(positions_page)
        self.positions_table = QTableWidget(0, len(POSITION_COLUMNS))
        self.positions_table.setHorizontalHeaderLabels(POSITION_COLUMNS)
        self.positions_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.positions_table.horizontalHeader().setStretchLastSection(True)
        self.positions_table.setAlternatingRowColors(True)
        self.positions_table.itemSelectionChanged.connect(self.on_position_selected)
        positions_layout.addWidget(self.positions_table, 1)
        positions_layout.addWidget(self._build_position_form())
        tabs.addTab(positions_page, "持仓管理")
        return tabs

    def _build_position_form(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        form = QFormLayout()
        self.position_inputs: dict[str, QLineEdit] = {}
        labels = {
            "ticker": "代码",
            "name": "名称",
            "buy_date": "买入日期",
            "buy_time": "买入时间",
            "buy_price": "成本",
            "shares": "数量",
            "target_price": "目标上沿",
            "hard_stop_price": "止损价",
            "trailing_stop_pct": "回撤%",
            "highest_price": "持仓最高",
            "management_state": "管理状态",
            "status": "状态",
            "notes": "备注",
        }
        for key, label in labels.items():
            edit = QLineEdit()
            if key == "status":
                edit.setText("open")
            self.position_inputs[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        new_button = QPushButton("新建")
        new_button.clicked.connect(self.clear_position_form)
        save_button = QPushButton("保存持仓")
        save_button.clicked.connect(self.save_position_from_form)
        delete_button = QPushButton("删除")
        delete_button.clicked.connect(self.delete_selected_position)
        import_button = QPushButton("导入 CSV")
        import_button.clicked.connect(self.import_positions)
        buttons.addWidget(new_button)
        buttons.addWidget(save_button)
        buttons.addWidget(delete_button)
        buttons.addStretch(1)
        buttons.addWidget(import_button)
        layout.addLayout(buttons)
        return container

    def _apply_style(self) -> None:
        base_font = QFont("Microsoft YaHei UI", 10)
        self.setFont(base_font)
        self.title_label.setFont(QFont("Microsoft YaHei UI", 18, QFont.Bold))
        self.status_label.setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f5f7fb; color: #172033; }
            QLabel { color: #172033; }
            QPushButton {
                min-height: 34px;
                padding: 6px 14px;
                border: 1px solid #b8c0cc;
                background: #ffffff;
            }
            QPushButton:hover { background: #e9f1ff; }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f7f9fc;
                gridline-color: #d9e0ea;
            }
            QHeaderView::section {
                background: #eef2f7;
                padding: 7px;
                border: 1px solid #d9e0ea;
                font-weight: 600;
            }
            QTextEdit { background: #ffffff; border: 1px solid #d9e0ea; }
            QProgressBar { min-height: 18px; border: 1px solid #b8c0cc; }
            QProgressBar::chunk { background: #2158a8; }
            """
        )

    def _version_text(self) -> str:
        version_path = self.root / "VERSION"
        version = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else "0.0.0"
        return f"本地扫描 · SQLite 数据 · v{version}"

    def append_log(self, message: str) -> None:
        self.log_box.append(f"{dt.datetime.now():%H:%M:%S}  {message}")

    @staticmethod
    def _setting_int(conn: Any, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(float(get_setting(conn, key, str(default))))
        except ValueError:
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _setting_float(conn: Any, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(get_setting(conn, key, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(maximum, value))

    def update_live_watch_checkbox_text(self) -> None:
        mode = "快速" if self.live_watch_fast_mode else "普通"
        seconds = self.live_watch_fast_seconds if self.live_watch_fast_mode else self.live_watch_normal_seconds
        self.live_watch_checkbox.setText(f"盘中监听候选股（{mode}{seconds}秒）")

    def set_live_watch_interval_mode(self, fast: bool, reason: str = "") -> None:
        if self.live_watch_fast_mode == fast:
            return
        self.live_watch_fast_mode = fast
        seconds = self.live_watch_fast_seconds if fast else self.live_watch_normal_seconds
        self.candidate_watch_timer.setInterval(seconds * 1000)
        self.update_live_watch_checkbox_text()
        mode = "快速" if fast else "普通"
        suffix = f"：{reason}" if reason else ""
        self.append_log(f"候选监听切换到{mode}模式，每 {seconds} 秒一次{suffix}")

    def sync_autostart_preference(self) -> None:
        try:
            set_windows_autostart(self.autostart_preferred)
            enabled = is_windows_autostart_enabled()
            self.autostart_checkbox.blockSignals(True)
            self.autostart_checkbox.setChecked(enabled)
            self.autostart_checkbox.blockSignals(False)
            self.append_log("开机自启动已开启" if enabled else "开机自启动已关闭")
        except Exception as exc:
            self.autostart_checkbox.blockSignals(True)
            self.autostart_checkbox.setChecked(False)
            self.autostart_checkbox.blockSignals(False)
            self.append_log(f"开机自启动设置失败：{exc}")

    def on_autostart_toggled(self, checked: bool) -> None:
        try:
            set_windows_autostart(checked)
        except Exception as exc:
            QMessageBox.warning(self, "开机自启动失败", str(exc))
            self.autostart_checkbox.blockSignals(True)
            self.autostart_checkbox.setChecked(is_windows_autostart_enabled())
            self.autostart_checkbox.blockSignals(False)
            return
        self.autostart_preferred = checked
        with connect(self.db_path) as conn:
            set_setting(conn, "autostart_enabled", "1" if checked else "0")
        self.append_log("开机自启动已开启" if checked else "开机自启动已关闭")

    def on_live_watch_toggled(self, checked: bool) -> None:
        self.live_watch_enabled = checked
        with connect(self.db_path) as conn:
            set_setting(conn, "live_candidate_watch_enabled", "1" if checked else "0")
        state = "已开启" if checked else "已关闭"
        self.append_log(f"盘中候选监听{state}")

    def on_live_watch_settings_changed(self) -> None:
        self.live_watch_trigger_adjust_pct = round(float(self.trigger_adjust_spin.value()), 4)
        self.live_watch_near_threshold_pct = round(float(self.near_threshold_spin.value()), 4)
        self.live_watch_normal_seconds = int(self.normal_interval_spin.value())
        self.live_watch_fast_seconds = int(self.fast_interval_spin.value())
        with connect(self.db_path) as conn:
            set_setting(conn, "live_watch_trigger_adjust_pct", str(self.live_watch_trigger_adjust_pct))
            set_setting(conn, "live_watch_near_threshold_pct", str(self.live_watch_near_threshold_pct))
            set_setting(conn, "live_watch_normal_seconds", str(self.live_watch_normal_seconds))
            set_setting(conn, "live_watch_fast_seconds", str(self.live_watch_fast_seconds))
        seconds = self.live_watch_fast_seconds if self.live_watch_fast_mode else self.live_watch_normal_seconds
        self.candidate_watch_timer.setInterval(seconds * 1000)
        self.update_live_watch_checkbox_text()
        self.append_log(
            "候选监听参数已保存："
            f"触发价调整 {self.live_watch_trigger_adjust_pct:.2f}%，"
            f"接近阈值基准 {self.live_watch_near_threshold_pct:.2f}%（至少 {LIVE_WATCH_MIN_NEAR_TICKS} tick），"
            f"普通 {self.live_watch_normal_seconds} 秒，快速 {self.live_watch_fast_seconds} 秒"
        )

    def load_cached_snapshot(self) -> None:
        with connect(self.db_path) as conn:
            payload = load_latest_snapshot(conn)
            skipped_phase = ""
            if self.should_skip_cached_snapshot(payload):
                skipped_phase = str(payload.get("phase", ""))
                payload = load_latest_snapshot_excluding(conn, {"opening", "intraday", "preclose"})
        if payload:
            self.current_payload = payload
            self.render_payload(payload)
            generated = str(payload.get("generated_at", "-"))
            self.last_scan_label.setText(f"上次扫描：{generated}")
            self.phase_label.setText(f"阶段：{payload.get('phase', '-')}")
            self.status_label.setText("已加载上次结果")
            self.progress.setValue(100)
            if skipped_phase:
                self.append_log(f"跳过非交易日盘中缓存：{skipped_phase}")
            self.append_log(f"已加载上次扫描结果：{generated}")

    @staticmethod
    def should_skip_cached_snapshot(payload: dict[str, Any]) -> bool:
        if not payload:
            return False
        if dt.datetime.now().weekday() < 5:
            return False
        return str(payload.get("phase", "")).lower() in {"opening", "intraday", "preclose"}

    def render_payload(self, payload: dict[str, Any]) -> None:
        self.current_payload = payload
        rows: list[tuple[str, dict[str, Any]]] = []
        for item in payload.get("sell") or []:
            if isinstance(item, dict):
                rows.append(("卖出", item))
        for item in payload.get("buy") or []:
            if isinstance(item, dict):
                rows.append(("买入", item))
        self.advice_table.setRowCount(len(rows))
        for row_index, (side, item) in enumerate(rows):
            values = [
                side,
                item.get("action", ""),
                item.get("management_state", "") if side == "卖出" else "",
                item.get("ticker", ""),
                item.get("name", ""),
                self._fmt(item.get("latest_price")),
                self._fmt(item.get("buy_price") if side == "卖出" else item.get("effective_trigger_price", item.get("trigger_price"))),
                f"{self._fmt(item.get('suggested_capital_pct'))}%" if side == "买入" else "",
                f"{item.get('position_quality_grade', '')}/{self._fmt(item.get('position_quality_score'))}" if side == "买入" else "",
                self._fmt(item.get("target_price")),
                self._fmt(item.get("first_manage_price")),
                self._fmt(item.get("trailing_stop_price")) if side == "卖出" else "",
                self._fmt(item.get("hard_stop_price")),
                self._fmt(item.get("vwap_fail_price")) if side == "卖出" else "",
                self._hit_rate_fmt(item.get("target_upper_hit_rate_pct")) if side == "买入" else "",
                self._hit_rate_fmt(item.get("target_upper_touch_rate_pct")) if side == "买入" else "",
                self._hit_rate_fmt(item.get("first_manage_hit_rate_pct")) if side == "买入" else "",
                self._fmt(item.get("hit_rate_sample_size")) if side == "买入" else "",
                item.get("hit_rate_bucket", "") if side == "买入" else "",
                self._fmt(item.get("pnl_pct")) if side == "卖出" else "",
                self._fmt(item.get("edge_score")) if side == "买入" else "",
                item.get("reason", ""),
            ]
            for column, value in enumerate(values):
                self.advice_table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.advice_table.resizeColumnsToContents()

    @staticmethod
    def normalize_ticker(value: str) -> str:
        digits = "".join(ch for ch in value.strip() if ch.isdigit())
        return digits[:6]

    def selected_advice_row(self) -> dict[str, str]:
        items = self.advice_table.selectedItems()
        if not items:
            return {}
        row = items[0].row()
        keys = ["side", "action", "management_state", "ticker", "name", "latest_price", "trigger_price", "suggested_capital_pct", "position_quality", "target_price", "first_manage_price", "trailing_stop_price", "hard_stop_price", "vwap_fail_price", "target_upper_hit_rate", "target_upper_touch_rate", "first_manage_hit_rate", "hit_rate_sample_size", "hit_rate_bucket", "pnl_pct", "edge_score", "reason"]
        values: dict[str, str] = {}
        for column, key in enumerate(keys):
            item = self.advice_table.item(row, column)
            values[key] = item.text().strip() if item else ""
        return values

    def on_advice_selected(self) -> None:
        row = self.selected_advice_row()
        if not row:
            return
        self.detail_label.setText(
            f"{row.get('side', '')} {row.get('action', '')} {row.get('ticker', '')} {row.get('name', '')}；"
            f"状态 {row.get('management_state', '')}，最新 {row.get('latest_price', '')}，触发/成本 {row.get('trigger_price', '')}，"
            f"建议资金 {row.get('suggested_capital_pct', '')}，质量 {row.get('position_quality', '')}，"
            f"目标上沿 {row.get('target_price', '')}，第一管理线 {row.get('first_manage_price', '')}，"
            f"移动止盈 {row.get('trailing_stop_price', '')}，止损 {row.get('hard_stop_price', '')}，"
            f"VWAP/成本 {row.get('vwap_fail_price', '')}，可卖上沿 {row.get('target_upper_hit_rate', '')}，"
            f"触及上沿 {row.get('target_upper_touch_rate', '')}，管理线 {row.get('first_manage_hit_rate', '')}，"
            f"样本数 {row.get('hit_rate_sample_size', '')}，样本桶 {row.get('hit_rate_bucket', '')}。"
        )
        if row.get("side") == "买入":
            self.fill_quick_position_from_selection()

    def find_buy_advice(self, ticker: str) -> dict[str, Any]:
        normalized = self.normalize_ticker(ticker)
        for item in self.current_payload.get("buy") or []:
            if isinstance(item, dict) and self.normalize_ticker(str(item.get("ticker", ""))) == normalized:
                return item
        return {}

    def fill_quick_position_from_selection(self) -> None:
        row = self.selected_advice_row()
        if not row or row.get("side") != "买入":
            return
        ticker = self.normalize_ticker(row.get("ticker", ""))
        if ticker:
            self.quick_ticker_input.setText(ticker)
        price = self._float(row.get("latest_price"), self._float(row.get("trigger_price"), 0.0))
        if price > 0:
            self.quick_buy_price_spin.setValue(price)

    def save_quick_position(self) -> None:
        ticker = self.normalize_ticker(self.quick_ticker_input.text())
        buy_price = float(self.quick_buy_price_spin.value())
        shares = float(self.quick_shares_spin.value())
        if not ticker or len(ticker) != 6:
            QMessageBox.warning(self, "持仓信息有误", "股票代码必须是 6 位数字。")
            return
        advice = self.find_buy_advice(ticker)
        now = dt.datetime.now()
        existing = None
        with connect(self.db_path) as conn:
            for position in list_positions(conn, open_only=True):
                if self.normalize_ticker(position.ticker) == ticker:
                    existing = position
                    break
        if existing:
            answer = QMessageBox.question(self, "更新已有持仓", f"{ticker} 已有 open 持仓，是否更新成本和数量？")
            if answer != QMessageBox.Yes:
                return
        position = Position(
            id=existing.id if existing else None,
            ticker=ticker,
            name=str(advice.get("name") or (existing.name if existing else "")),
            buy_date=now.date().isoformat(),
            buy_time=now.time().isoformat(timespec="minutes"),
            buy_price=buy_price,
            shares=shares,
            target_price=self._float(advice.get("target_price"), existing.target_price if existing else 0.0),
            hard_stop_price=self._float(advice.get("hard_stop_price"), existing.hard_stop_price if existing else 0.0),
            trailing_stop_pct=self._float(advice.get("trailing_stop_pct"), existing.trailing_stop_pct if existing else 3.0),
            highest_price=max(buy_price, self._float(advice.get("latest_price"), buy_price)),
            management_state=existing.management_state if existing else "OPEN",
            status="open",
            notes=f"快速登记；来源：{self.current_payload.get('generated_at', '手动输入')}",
        )
        try:
            with connect(self.db_path) as conn:
                self.selected_position_id = save_position(conn, position)
        except ValueError as exc:
            QMessageBox.warning(self, "持仓信息有误", str(exc))
            return
        self.refresh_positions()
        self.append_log(f"已同步持仓库：{ticker} 成本 {buy_price:.3f} 数量 {shares:.0f}")
        QMessageBox.information(self, "已同步持仓库", f"{ticker} 已写入本地持仓库。")

    def show_trade_popup(self, title: str, lines: list[str], icon: QMessageBox.Icon = QMessageBox.Warning) -> None:
        if not lines:
            return
        QApplication.beep()
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText("\n".join(lines[:8]))
        if len(lines) > 8:
            box.setDetailedText("\n".join(lines))
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.exec()

    def alert_payload_actions(self, payload: dict[str, Any], source: str, include_sells: bool = True) -> None:
        date_key = str(payload.get("date") or dt.date.today().isoformat())
        buy_lines: list[str] = []
        sell_lines: list[str] = []
        for item in payload.get("buy") or []:
            if not isinstance(item, dict) or item.get("action") != "BUY_NOW":
                continue
            ticker = str(item.get("ticker") or "")
            key = f"{date_key}:BUY_NOW:{ticker}"
            if key in self.alerted_buy_keys:
                continue
            self.alerted_buy_keys.add(key)
            latest = self._fmt(item.get("latest_price"))
            trigger = self._fmt(item.get("effective_trigger_price", item.get("trigger_price")))
            buy_lines.append(f"{ticker} {item.get('name', '')} 买入触发：最新 {latest} / 阈值 {trigger}")
        if include_sells:
            for item in payload.get("sell") or []:
                if not isinstance(item, dict) or item.get("action") not in URGENT_SELL_ACTIONS:
                    continue
                ticker = str(item.get("ticker") or "")
                action = str(item.get("action") or "")
                key = f"{date_key}:{action}:{ticker}"
                if key in self.alerted_sell_keys:
                    continue
                self.alerted_sell_keys.add(key)
                latest = self._fmt(item.get("latest_price"))
                pnl = self._fmt(item.get("pnl_pct"))
                points = str(item.get("signal_points") or "").strip()
                suffix = f" / {points}" if points else ""
                sell_lines.append(f"{ticker} {item.get('name', '')} {action}：最新 {latest} / 盈亏 {pnl}%{suffix}")
        if buy_lines:
            self.append_log(f"{source}买入触发：" + "、".join(line.split()[0] for line in buy_lines))
            self.show_trade_popup(f"{source}买入触发", buy_lines, QMessageBox.Information)
        if sell_lines:
            self.append_log(f"{source}卖出触发：" + "、".join(line.split()[0] for line in sell_lines))
            self.show_trade_popup(f"{source}卖出提醒", sell_lines, QMessageBox.Warning)

    def refresh_positions(self) -> None:
        with connect(self.db_path) as conn:
            positions = list_positions(conn)
        self.positions_table.setRowCount(len(positions))
        for row_index, position in enumerate(positions):
            values = [
                position.ticker,
                position.name,
                position.buy_date,
                position.buy_time,
                self._fmt(position.buy_price),
                self._fmt(position.shares),
                self._fmt(position.target_price),
                self._fmt(position.hard_stop_price),
                self._fmt(position.trailing_stop_pct),
                self._fmt(position.highest_price),
                position.management_state,
                position.status,
                position.notes,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, position.id)
                self.positions_table.setItem(row_index, column, item)
        self.positions_table.resizeColumnsToContents()

    def on_position_selected(self) -> None:
        items = self.positions_table.selectedItems()
        if not items:
            return
        row = items[0].row()
        first = self.positions_table.item(row, 0)
        self.selected_position_id = int(first.data(Qt.UserRole)) if first and first.data(Qt.UserRole) else None
        keys = list(self.position_inputs)
        for column, key in enumerate(keys):
            item = self.positions_table.item(row, column)
            self.position_inputs[key].setText(item.text() if item else "")

    def clear_position_form(self) -> None:
        self.selected_position_id = None
        for edit in self.position_inputs.values():
            edit.clear()
        self.position_inputs["status"].setText("open")

    def save_position_from_form(self) -> None:
        row = {key: edit.text().strip() for key, edit in self.position_inputs.items()}
        position = Position(
            id=self.selected_position_id,
            ticker=row["ticker"],
            name=row["name"],
            buy_date=row["buy_date"],
            buy_time=row["buy_time"],
            buy_price=self._float(row["buy_price"]),
            shares=self._float(row["shares"]),
            target_price=self._float(row["target_price"]),
            hard_stop_price=self._float(row["hard_stop_price"]),
            trailing_stop_pct=self._float(row["trailing_stop_pct"], 3.0),
            highest_price=self._float(row["highest_price"], self._float(row["buy_price"])),
            management_state=row["management_state"] or "OPEN",
            status=row["status"] or "open",
            notes=row["notes"],
        )
        try:
            with connect(self.db_path) as conn:
                self.selected_position_id = save_position(conn, position)
        except ValueError as exc:
            QMessageBox.warning(self, "持仓信息有误", str(exc))
            return
        self.refresh_positions()
        self.append_log(f"已保存持仓：{position.ticker}")

    def delete_selected_position(self) -> None:
        if not self.selected_position_id:
            return
        if QMessageBox.question(self, "删除持仓", "确定删除选中的持仓？") != QMessageBox.Yes:
            return
        with connect(self.db_path) as conn:
            delete_position(conn, self.selected_position_id)
        self.clear_position_form()
        self.refresh_positions()
        self.append_log("已删除持仓")

    def import_positions(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入持仓 CSV", str(self.root), "CSV Files (*.csv)")
        if not path:
            return
        with connect(self.db_path) as conn:
            count = import_positions_csv(conn, Path(path))
        self.refresh_positions()
        QMessageBox.information(self, "导入完成", f"已导入 {count} 条持仓。")

    def start_scan(self, phase_override: str | None = None, trigger_reason: str = "手动") -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            return
        phase = phase_override or phase_for_time(dt.datetime.now())
        if phase == "closed":
            phase = "postclose"
        self.progress.setValue(5)
        self.status_label.setText("扫描中")
        self.scan_button.setEnabled(False)
        self.scan_started_at = dt.datetime.now()
        self.scan_stage_text = "启动扫描线程"
        self.scan_last_heartbeat_seconds = 0
        self.scan_last_stack_dump_seconds = 0
        debug_path = write_scan_debug_log(f"[ui] 启动扫描；trigger={trigger_reason} phase={phase} root={self.root} db={self.db_path}")
        self.append_log(f"{trigger_reason}启动扫描：{phase}")
        self.append_log(f"诊断日志：{debug_path}")
        self.scan_timer.start()
        self.scan_worker = ScanWorker(self.root, phase, self.db_path)
        self.scan_worker.started.connect(lambda: self.on_scan_progress(6, "扫描线程已启动"))
        self.scan_worker.progress.connect(self.on_scan_progress)
        self.scan_worker.log.connect(self.append_log)
        self.scan_worker.finished_payload.connect(self.on_scan_done)
        self.scan_worker.failed.connect(self.on_scan_failed)
        self.scan_worker.start()

    def should_auto_opening_scan(self, now: dt.datetime) -> bool:
        if now.weekday() >= 5:
            return False
        if self.last_auto_opening_scan_date == now.date():
            return False
        if self.scan_worker and self.scan_worker.isRunning():
            return False
        return dt.time(9, 0) <= now.time() < dt.time(9, 30)

    def check_auto_opening_scan(self) -> None:
        now = dt.datetime.now()
        if not self.should_auto_opening_scan(now):
            return
        self.last_auto_opening_scan_date = now.date()
        self.append_log("开盘前半小时自动扫描触发")
        write_scan_debug_log(f"[auto-scan] opening scan triggered at {now:%Y-%m-%d %H:%M:%S}")
        self.start_scan("opening", "开盘前自动")

    def should_live_candidate_watch(self, now: dt.datetime) -> tuple[bool, str]:
        if not self.live_watch_enabled:
            return False, "监听未开启"
        if not is_live_watch_window(now):
            return False, "不在交易监听时段"
        if self.scan_worker and self.scan_worker.isRunning():
            return False, "扫描正在运行"
        if self.candidate_watch_worker and self.candidate_watch_worker.isRunning():
            return False, "上一轮监听未结束"
        if not self.current_payload:
            return False, "没有可监听的扫描结果"
        payload_date = latest_payload_trade_date(self.current_payload)
        if payload_date != now.date():
            return False, "扫描结果不是今天"
        buys = [item for item in self.current_payload.get("buy") or [] if isinstance(item, dict) and item.get("ticker")]
        if not buys:
            return False, "扫描结果里没有候选股"
        return True, ""

    def check_live_candidate_watch(self) -> None:
        now = dt.datetime.now()
        ok, reason = self.should_live_candidate_watch(now)
        if not ok:
            if self.live_watch_fast_mode and reason in {"不在交易监听时段", "没有可监听的扫描结果", "扫描结果不是今天"}:
                self.set_live_watch_interval_mode(False, reason)
            if reason in {"没有可监听的扫描结果", "扫描结果不是今天"}:
                if self.last_live_watch_skip_log_at is None or (now - self.last_live_watch_skip_log_at).total_seconds() >= 300:
                    self.last_live_watch_skip_log_at = now
                    self.append_log(f"候选监听跳过：{reason}")
            return
        self.candidate_watch_worker = CandidateWatchWorker(
            self.current_payload,
            trigger_adjust_pct=self.live_watch_trigger_adjust_pct,
            near_threshold_pct=self.live_watch_near_threshold_pct,
        )
        self.candidate_watch_worker.finished_watch.connect(self.on_live_candidate_watch_done)
        self.candidate_watch_worker.failed.connect(self.on_live_candidate_watch_failed)
        self.candidate_watch_worker.start()

    def on_live_candidate_watch_done(self, payload: dict[str, Any]) -> None:
        self.current_payload = payload
        self.render_payload(payload)
        summary = payload.get("watch_summary") or {}
        watched = int(summary.get("watched") or 0)
        triggered = int(summary.get("triggered") or 0)
        near_threshold = int(summary.get("near_threshold") or 0)
        unavailable = int(summary.get("unavailable") or 0)
        self.status_label.setText("有买入触发" if triggered else "候选监听中")
        closest = summary.get("closest_distance_pct")
        closest_text = f"，最近差 {float(closest):.2f}%" if closest is not None else ""
        self.append_log(f"候选监听完成：监听 {watched} 只，接近阈值 {near_threshold} 只，触发 {triggered} 只，行情不可用 {unavailable} 只{closest_text}")
        self.set_live_watch_interval_mode(
            near_threshold > 0 or triggered > 0,
            f"{near_threshold} 只接近阈值，{triggered} 只触发" if near_threshold > 0 or triggered > 0 else "暂未接近阈值",
        )
        self.alert_payload_actions(payload, "候选监听", include_sells=False)

    def on_live_candidate_watch_failed(self, message: str) -> None:
        write_scan_debug_log(f"[live-watch] 监听失败：{message}")
        self.append_log(f"候选监听失败：{message}")

    def on_scan_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(percent)
        self.scan_stage_text = message
        self.append_log(message)

    def on_scan_heartbeat(self) -> None:
        if not self.scan_worker or not self.scan_worker.isRunning() or not self.scan_started_at:
            self.scan_timer.stop()
            return
        elapsed = int((dt.datetime.now() - self.scan_started_at).total_seconds())
        if elapsed <= self.scan_last_heartbeat_seconds:
            return
        self.scan_last_heartbeat_seconds = elapsed
        if self.progress.value() < 25:
            self.progress.setValue(max(self.progress.value(), min(24, 6 + elapsed // 10)))
        stage = self.scan_stage_text or "等待后台扫描响应"
        self.status_label.setText(f"扫描中（{elapsed} 秒）")
        self.append_log(f"扫描仍在运行：已 {elapsed} 秒，当前阶段：{stage}")
        if elapsed >= 30 and elapsed - self.scan_last_stack_dump_seconds >= 30:
            self.scan_last_stack_dump_seconds = elapsed
            self.dump_scan_worker_stack(elapsed, stage)

    def dump_scan_worker_stack(self, elapsed: int, stage: str) -> None:
        worker = self.scan_worker
        thread_id = getattr(worker, "python_thread_id", None) if worker else None
        write_scan_debug_log(f"[watchdog] elapsed={elapsed}s stage={stage} worker_thread={thread_id}")
        if thread_id is None:
            write_scan_debug_log("[watchdog] 后台线程还没有登记 Python thread id")
            return
        frame = sys._current_frames().get(thread_id)
        if frame is None:
            write_scan_debug_log("[watchdog] 没找到后台线程 frame，可能停在 C 扩展或已退出")
            return
        stack = "".join(traceback.format_stack(frame)).rstrip()
        write_scan_debug_log("[watchdog] 后台扫描线程栈：\n" + stack)
        self.append_log("已写入后台线程诊断栈")

    def on_scan_done(self, payload: dict[str, Any]) -> None:
        self.scan_button.setEnabled(True)
        self.scan_timer.stop()
        self.scan_started_at = None
        write_scan_debug_log("[ui] 扫描完成")
        self.progress.setValue(100)
        self.status_label.setText("扫描完成")
        if payload:
            self.render_payload(payload)
            self.last_scan_label.setText(f"上次扫描：{payload.get('generated_at', '-')}")
            self.phase_label.setText(f"阶段：{payload.get('phase', '-')}")
            self.alert_payload_actions(payload, "扫描结果")
        self.append_log("扫描完成")

    def on_scan_failed(self, message: str) -> None:
        self.scan_button.setEnabled(True)
        self.scan_timer.stop()
        self.scan_started_at = None
        write_scan_debug_log(f"[ui] 扫描失败：{message}")
        self.status_label.setText("扫描失败")
        self.append_log(f"扫描失败：{message}")
        QMessageBox.critical(self, "扫描失败", message[:3000])

    def check_update(self) -> None:
        if self.update_worker and self.update_worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("检查中...")
        self.append_log("开始检查更新")
        self.update_worker = UpdateWorker(self.root)
        self.update_worker.finished_update.connect(self.on_update_done)
        self.update_worker.start()

    def on_update_done(self, result: dict[str, Any]) -> None:
        self.update_button.setEnabled(True)
        self.update_button.setText("检查更新")
        message = str(result.get("message") or "检查完成。")
        self.append_log(f"更新检查：{message}")
        if not result.get("ok"):
            QMessageBox.critical(self, "更新失败", message)
            return
        if result.get("update_available"):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("检查更新")
            box.setText(message)
            open_button = box.addButton("打开下载页面", QMessageBox.AcceptRole)
            box.addButton("稍后", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() == open_button:
                url = str(result.get("asset_url") or result.get("release_url") or "")
                if url:
                    webbrowser.open(url)
        else:
            QMessageBox.information(self, "检查更新", message)

    @staticmethod
    def _fmt(value: object) -> str:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "" if value is None else str(value)

    @staticmethod
    def _hit_rate_fmt(value: object) -> str:
        if value in {None, ""}:
            return "样本不足"
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _float(value: object, default: float = 0.0) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default


def main() -> int:
    ensure_text_stdio()
    if len(sys.argv) > 1 and sys.argv[1] == "--run-monitor":
        return run_internal_monitor(sys.argv[2:])
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--no-update-check", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--scan-prepare-test", action="store_true")
    args, _ = parser.parse_known_args()
    if args.scan_prepare_test:
        phase = phase_for_time(dt.datetime.now())
        if phase == "closed":
            phase = "postclose"
        write_scan_debug_log(f"[scan-prepare-test] root={app_root()} db={default_db_path()} phase={phase}")
        build_scan_args(
            app_root(),
            phase,
            default_db_path(),
            lambda percent, message: write_scan_debug_log(f"[scan-prepare-test] {percent}% {message}"),
        )
        write_scan_debug_log("[scan-prepare-test] 参数准备完成")
        return 0
    app = QApplication(sys.argv)
    window = MainWindow()
    if args.smoke_test:
        window.close()
        app.quit()
        return 0
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
