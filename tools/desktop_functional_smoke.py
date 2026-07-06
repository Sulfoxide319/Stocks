#!/usr/bin/env python3
"""Functional smoke tests for the PySide desktop trading assistant."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("STOCKS_SKIP_AUTO_INSTALL", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import desktop_app  # noqa: E402
from app_storage import connect, export_open_positions_csv, get_setting, list_positions, set_setting  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QDoubleSpinBox, QTableWidget  # noqa: E402


class FakeMessageBox:
    Yes = 0x00004000
    No = 0x00010000
    Information = 1
    Warning = 2

    calls: list[tuple[str, str, str]] = []

    @staticmethod
    def information(_parent: object, title: str, message: str) -> int:
        FakeMessageBox.calls.append(("information", title, message))
        return FakeMessageBox.Yes

    @staticmethod
    def warning(_parent: object, title: str, message: str) -> int:
        FakeMessageBox.calls.append(("warning", title, message))
        return FakeMessageBox.Yes

    @staticmethod
    def critical(_parent: object, title: str, message: str) -> int:
        FakeMessageBox.calls.append(("critical", title, message))
        return FakeMessageBox.Yes

    @staticmethod
    def question(_parent: object, title: str, message: str) -> int:
        FakeMessageBox.calls.append(("question", title, message))
        return FakeMessageBox.Yes


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_payload() -> dict[str, Any]:
    today = dt.date.today().isoformat()
    return {
        "date": today,
        "generated_at": f"{today}T10:02:00",
        "phase": "intraday",
        "buy": [
            {
                "action": "BUY_NOW",
                "ticker": "000725",
                "name": "京东方A",
                "latest_price": 8.30,
                "effective_trigger_price": 8.20,
                "trigger_price": 8.18,
                "suggested_capital_pct": 5.0,
                "position_quality_grade": "A",
                "position_quality_score": 0.82,
                "score": 79,
                "target_price": 9.12,
                "first_manage_price": 8.72,
                "hard_stop_price": 7.86,
                "edge_score": 1.5,
                "reason": "功能烟测买入触发",
            }
        ],
        "sell": [
            {
                "action": "SELL_NOW",
                "ticker": "600519",
                "name": "贵州茅台",
                "latest_price": 1490.00,
                "buy_price": 1520.00,
                "shares": 100,
                "target_price": 1660.00,
                "first_manage_price": 1580.00,
                "hard_stop_price": 1495.00,
                "trailing_stop_price": 1510.00,
                "management_state": "OPEN",
                "pnl_pct": -1.97,
                "signal_points": "跌破硬止损",
                "reason": "功能烟测卖出触发",
            }
        ],
    }


def prepare_window(temp_root: Path) -> tuple[QApplication, desktop_app.MainWindow]:
    os.environ["LOCALAPPDATA"] = str(temp_root)
    desktop_app.set_windows_autostart = lambda _enabled: None
    desktop_app.is_windows_autostart_enabled = lambda: False
    desktop_app.QMessageBox = FakeMessageBox
    desktop_app.QApplication.beep = staticmethod(lambda: None)

    app = QApplication.instance() or QApplication([])
    db_path = desktop_app.default_db_path()
    with connect(db_path) as conn:
        set_setting(conn, "legacy_positions_imported", "1")
        set_setting(conn, "legacy_latest_snapshot_imported", "1")
        set_setting(conn, "autostart_enabled", "0")
        set_setting(conn, "trade_cash_amount", "30000.00")
        set_setting(conn, "trade_holdings_value", "70000.00")
        set_setting(conn, "trade_total_assets", "100000.00")

    window = desktop_app.MainWindow()
    window.auto_scan_timer.stop()
    window.candidate_watch_timer.stop()
    window.scan_timer.stop()
    window.hide()
    return app, window


def test_mid_session_positions(window: desktop_app.MainWindow, temp_root: Path) -> None:
    payload = make_payload()
    window.render_payload(payload)
    buy_row = 1 if window.advice_table.item(1, 3) and window.advice_table.item(1, 3).text() == "000725" else 0
    window.advice_table.selectRow(buy_row)
    window.on_advice_selected()
    assert_true(window.quick_ticker_input.text() == "000725", "selected BUY row should fill quick ticker")
    assert_true(abs(window.quick_buy_price_spin.value() - 8.30) < 0.001, "selected BUY row should fill latest price")

    window.quick_shares_spin.setValue(300)
    window.save_quick_position()
    with connect(window.db_path) as conn:
        positions = [item for item in list_positions(conn, open_only=True) if item.ticker == "000725"]
    assert_true(len(positions) == 1, "quick registration should write one open position")
    assert_true(positions[0].shares == 300, "quick registration should keep entered shares")
    assert_true(abs(positions[0].target_price - 9.12) < 0.001, "quick registration should copy advice target")

    for key, value in {
        "ticker": "002415",
        "name": "海康威视",
        "buy_date": dt.date.today().isoformat(),
        "buy_time": "10:15",
        "buy_price": "31.25",
        "shares": "200",
        "target_price": "34.38",
        "hard_stop_price": "29.70",
        "trailing_stop_pct": "3",
        "highest_price": "31.80",
        "management_state": "OPEN",
        "status": "open",
        "notes": "功能烟测手工中途登记",
    }.items():
        window.position_inputs[key].setText(value)
    window.save_position_from_form()
    with connect(window.db_path) as conn:
        open_positions = list_positions(conn, open_only=True)
        exported = temp_root / "runtime_positions.csv"
        export_open_positions_csv(conn, exported)
    assert_true(any(item.ticker == "002415" for item in open_positions), "manual form should add an open position")
    exported_text = exported.read_text(encoding="utf-8-sig")
    assert_true("000725" in exported_text and "002415" in exported_text, "next scan export should include runtime positions")


def test_broker_sync(window: desktop_app.MainWindow) -> None:
    fake_result = SimpleNamespace(
        ok=True,
        message="fake broker export ok",
        cash_available=12345.67,
        holdings_value=88888.88,
        total_assets=101234.55,
        export_path="functional-smoke.txt",
        positions=(
            SimpleNamespace(
                ticker="000725",
                name="京东方A",
                shares=500,
                cost_price=8.10,
                latest_price=8.50,
                market_value=4250.0,
                sellable_shares=500,
            ),
        ),
    )
    desktop_app.export_guoshengrui_holdings = lambda: fake_result
    window.sync_positions_from_guoshengrui()
    with connect(window.db_path) as conn:
        positions = [item for item in list_positions(conn, open_only=True) if item.ticker == "000725"]
        cash = get_setting(conn, "trade_cash_amount")
        total = get_setting(conn, "trade_total_assets")
    assert_true(len(positions) == 1, "broker sync should update existing ticker instead of duplicating it")
    assert_true(positions[0].shares == 500 and abs(positions[0].buy_price - 8.10) < 0.001, "broker sync should replace broker-owned cost/shares")
    assert_true(cash == "12345.67" and total == "101234.55", "broker sync should refresh account settings")


def test_popup_interactions(app: QApplication, window: desktop_app.MainWindow) -> None:
    original_dialog = desktop_app.QDialog
    captured: dict[str, Any] = {}
    trade_calls: list[dict[str, Any]] = []
    ticker_jumps: list[str] = []
    xueqiu_urls: list[str] = []

    desktop_app.jump_guoshengrui_trade_for_ticker = lambda ticker, side, **kwargs: trade_calls.append(
        {"ticker": ticker, "side": side, **kwargs}
    ) or SimpleNamespace(ok=True, message=f"fake trade {side} {ticker}")
    desktop_app.jump_guoshengrui_for_ticker = lambda ticker: ticker_jumps.append(ticker) or SimpleNamespace(ok=True, message=f"fake jump {ticker}", code="ok")
    desktop_app.webbrowser.open_new_tab = lambda url: xueqiu_urls.append(url)

    class TestDialog(original_dialog):
        def exec(self) -> int:  # type: ignore[override]
            captured["title"] = self.windowTitle()
            captured["topmost"] = bool(self.windowFlags() & Qt.WindowStaysOnTopHint)
            labels = [label.text() for label in self.findChildren(QLabel)]
            captured["has_manual_submit_copy"] = any("不自动下单" in text for text in labels)
            table = self.findChild(QTableWidget)
            assert_true(table is not None, "popup should contain an action table")
            captured["rows"] = table.rowCount()
            captured["columns"] = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
            code_cell = table.item(0, 2)
            name_cell = table.item(0, 3)
            captured["code_tooltip"] = code_cell.toolTip() if code_cell else ""
            captured["name_tooltip"] = name_cell.toolTip() if name_cell else ""
            table.cellClicked.emit(0, 2)
            table.cellClicked.emit(0, 3)
            for row in range(table.rowCount()):
                button = table.cellWidget(row, 6)
                assert_true(isinstance(button, QPushButton), "each popup row should have a trade button")
                button.click()
            spins = self.findChildren(QDoubleSpinBox)
            captured["spin_values"] = [round(spin.value(), 2) for spin in spins]
            app.processEvents()
            return 0

    desktop_app.QDialog = TestDialog
    try:
        window.show_trade_action_popup(
            "功能烟测触发",
            [
                {
                    "side": "买入",
                    "action": "BUY_NOW",
                    "ticker": "000725",
                    "name": "京东方A",
                    "latest_price": 8.50,
                    "suggested_capital_pct": 5.0,
                    "reason": "买入弹窗烟测",
                },
                {
                    "side": "卖出",
                    "action": "SELL_NOW",
                    "ticker": "600519",
                    "name": "贵州茅台",
                    "latest_price": 1490.00,
                    "reason": "卖出弹窗烟测",
                },
            ],
            FakeMessageBox.Warning,
        )
    finally:
        desktop_app.QDialog = original_dialog

    assert_true(captured["topmost"], "popup should be topmost")
    assert_true(captured["has_manual_submit_copy"], "popup should state that it does not submit orders")
    assert_true(captured["rows"] == 2, "popup should list both buy and sell actions")
    assert_true("交易界面" in captured["columns"], "popup should expose trade terminal actions")
    assert_true("复制股票代码" in captured["code_tooltip"], "popup code cell should expose copy/jump tooltip")
    assert_true("打开雪球" in captured["name_tooltip"], "popup name cell should expose Xueqiu tooltip")
    assert_true(ticker_jumps == ["000725"], "clicking popup ticker should jump/copy the ticker")
    assert_true(xueqiu_urls == ["https://xueqiu.com/S/SZ000725"], "clicking popup name should open Xueqiu")
    assert_true(len(trade_calls) == 2, "popup trade buttons should invoke trade bridge for every row")
    assert_true(trade_calls[0]["side"] == "buy" and trade_calls[0]["fill_quantity"] is True, "buy button should request quantity fill")
    assert_true(trade_calls[0]["existing_shares"] == 500, "buy quantity plan should account for existing runtime holdings")
    assert_true(trade_calls[1]["side"] == "sell", "sell button should open sell mode")
    assert_true(12345.67 in captured["spin_values"] and 101234.55 in captured["spin_values"], "popup should load broker account defaults")


def test_alert_dedup(window: desktop_app.MainWindow) -> None:
    calls: list[tuple[str, list[str]]] = []

    def capture_popup(title: str, items: list[dict[str, Any]], _icon: object = None) -> None:
        calls.append((title, [str(item.get("ticker")) for item in items]))

    window.show_trade_action_popup = capture_popup  # type: ignore[method-assign]
    payload = make_payload()
    window.alerted_buy_keys.clear()
    window.alerted_sell_keys.clear()
    window.alert_payload_actions(payload, "第一次", include_sells=True)
    window.alert_payload_actions(payload, "第二次", include_sells=True)
    assert_true(len(calls) == 2, "same-day repeated payload should not show duplicate popups")
    assert_true(calls[0][1] == ["000725"] and calls[1][1] == ["600519"], "first alert should split buy and sell popups")


def main() -> int:
    temp_root = Path(tempfile.mkdtemp(prefix="stocks-functional-smoke-"))
    try:
        app, window = prepare_window(temp_root)
        test_mid_session_positions(window, temp_root)
        test_broker_sync(window)
        test_popup_interactions(app, window)
        test_alert_dedup(window)
        window.close()
        app.processEvents()
        print("ok desktop functional smoke")
        return 0
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
