#!/usr/bin/env python3
"""PySide6 desktop shell for the A-share trading assistant."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
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
    import_positions_csv,
    list_positions,
    load_latest_snapshot,
    migrate_legacy_files,
    save_position,
)


try:
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtGui import QAction, QFont
    from PySide6.QtWidgets import (
        QApplication,
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


ADVICE_COLUMNS = ["方向", "动作", "代码", "名称", "最新", "触发/成本", "目标", "止损", "盈亏", "Edge", "理由"]
POSITION_COLUMNS = ["代码", "名称", "买入日期", "买入时间", "成本", "数量", "目标", "止损", "回撤%", "最高", "状态", "备注"]
REPOSITORY = "Sulfoxide319/Stocks"


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
        return Path(sys.executable).resolve().parent
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

    def run(self) -> None:
        import local_trading_assistant

        previous_emit = local_trading_assistant.emit_progress
        local_trading_assistant.emit_progress = lambda percent, message: self.progress.emit(int(percent), str(message))
        try:
            out_dir = app_data_dir() / "output" / "trading_assistant"
            argv = [
                "--once",
                "--phase",
                self.phase,
                "--out-dir",
                str(out_dir),
                "--db",
                str(self.db_path),
                "--app-db",
                str(self.db_path),
                "--use-app-db",
            ]
            if getattr(sys, "frozen", False):
                argv.extend(["--python", sys.executable, "--monitor-script", "--run-monitor"])
            args = build_arg_parser().parse_args(argv)
            self.log.emit(f"开始 {self.phase} 扫描")
            run_once(args, self.root)
            with connect(self.db_path) as conn:
                payload = load_latest_snapshot(conn)
            self.finished_payload.emit(payload)
        except Exception as exc:  # pragma: no cover - exercised through GUI flow.
            self.failed.emit(str(exc))
        finally:
            local_trading_assistant.emit_progress = previous_emit


class UpdateWorker(QThread):
    finished_update = Signal(dict)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    def run(self) -> None:
        self.finished_update.emit(check_latest_update(self.root))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.root = app_root()
        self.db_path = default_db_path()
        self.selected_position_id: int | None = None
        self.scan_worker: ScanWorker | None = None
        self.update_worker: UpdateWorker | None = None
        with connect(self.db_path) as conn:
            self.migration_notes = migrate_legacy_files(conn, self.root)
        self.setWindowTitle("A股短线交易助手")
        self.resize(1280, 780)
        self._build_ui()
        self._apply_style()
        self.refresh_positions()
        self.load_cached_snapshot()
        for note in self.migration_notes:
            self.append_log(note)

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
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(180)
        layout.addWidget(self.status_label)
        layout.addWidget(self.phase_label)
        layout.addWidget(self.last_scan_label)
        layout.addWidget(self.progress)
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
            "target_price": "目标价",
            "hard_stop_price": "止损价",
            "trailing_stop_pct": "回撤%",
            "highest_price": "持仓最高",
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

    def load_cached_snapshot(self) -> None:
        with connect(self.db_path) as conn:
            payload = load_latest_snapshot(conn)
        if payload:
            self.render_payload(payload)
            generated = str(payload.get("generated_at", "-"))
            self.last_scan_label.setText(f"上次扫描：{generated}")
            self.phase_label.setText(f"阶段：{payload.get('phase', '-')}")
            self.status_label.setText("已加载上次结果")
            self.progress.setValue(100)
            self.append_log(f"已加载上次扫描结果：{generated}")

    def render_payload(self, payload: dict[str, Any]) -> None:
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
                item.get("ticker", ""),
                item.get("name", ""),
                self._fmt(item.get("latest_price")),
                self._fmt(item.get("buy_price") if side == "卖出" else item.get("trigger_price")),
                self._fmt(item.get("target_price")),
                self._fmt(item.get("hard_stop_price")),
                self._fmt(item.get("pnl_pct")) if side == "卖出" else "",
                self._fmt(item.get("edge_score")) if side == "买入" else "",
                item.get("reason", ""),
            ]
            for column, value in enumerate(values):
                self.advice_table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.advice_table.resizeColumnsToContents()

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

    def start_scan(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            return
        phase = phase_for_time(dt.datetime.now())
        if phase == "closed":
            phase = "intraday"
        self.progress.setValue(5)
        self.status_label.setText("扫描中")
        self.scan_button.setEnabled(False)
        self.scan_worker = ScanWorker(self.root, phase, self.db_path)
        self.scan_worker.progress.connect(self.on_scan_progress)
        self.scan_worker.log.connect(self.append_log)
        self.scan_worker.finished_payload.connect(self.on_scan_done)
        self.scan_worker.failed.connect(self.on_scan_failed)
        self.scan_worker.start()

    def on_scan_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(percent)
        self.append_log(message)

    def on_scan_done(self, payload: dict[str, Any]) -> None:
        self.scan_button.setEnabled(True)
        self.progress.setValue(100)
        self.status_label.setText("扫描完成")
        if payload:
            self.render_payload(payload)
            self.last_scan_label.setText(f"上次扫描：{payload.get('generated_at', '-')}")
            self.phase_label.setText(f"阶段：{payload.get('phase', '-')}")
        self.append_log("扫描完成")

    def on_scan_failed(self, message: str) -> None:
        self.scan_button.setEnabled(True)
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
    args, _ = parser.parse_known_args()
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
