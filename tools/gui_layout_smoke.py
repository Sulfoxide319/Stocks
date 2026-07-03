#!/usr/bin/env python3
"""Smoke-test the desktop GUI layout at common window sizes."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

os.environ.setdefault("STOCKS_SKIP_AUTO_INSTALL", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_assistant_app import TradingAssistantApp  # noqa: E402


SIZES = ["980x640+40+40", "1180x760+60+60", "1366x768+80+80", "1600x900+100+100"]


def iter_children(widget: tk.Misc) -> list[tk.Misc]:
    children: list[tk.Misc] = []
    for child in widget.winfo_children():
        children.append(child)
        children.extend(iter_children(child))
    return children


def assert_visible_inside(root: tk.Misc, widget: tk.Misc, label: str, min_height: int = 32) -> None:
    root.update_idletasks()
    assert widget.winfo_ismapped(), f"{label} is not mapped"
    assert widget.winfo_width() >= 40, f"{label} width is too small: {widget.winfo_width()}"
    assert widget.winfo_height() >= min_height, f"{label} height is too small: {widget.winfo_height()}"

    root_x = root.winfo_rootx()
    root_y = root.winfo_rooty()
    root_w = root.winfo_width()
    root_h = root.winfo_height()
    widget_x = widget.winfo_rootx()
    widget_y = widget.winfo_rooty()
    widget_w = widget.winfo_width()
    widget_h = widget.winfo_height()
    assert widget_x >= root_x, f"{label} is clipped on the left"
    assert widget_y >= root_y, f"{label} is clipped on the top"
    assert widget_x + widget_w <= root_x + root_w, f"{label} is clipped on the right"
    assert widget_y + widget_h <= root_y + root_h, f"{label} is clipped on the bottom"


def smoke_size(size: str) -> None:
    root = tk.Tk()
    root.geometry(size)
    try:
        root.call("tk", "scaling", 1.15)
    except Exception:
        pass
    app = TradingAssistantApp(root)
    root.update()

    app.set_update_event("测试摘要", "测试更新日志。" * 80)
    root.update()

    for key, button in app.action_buttons.items():
        assert_visible_inside(root, button, f"{size} action button {key}")

    assert app.detail_frame is not None
    assert_visible_inside(root, app.detail_frame, f"{size} detail panel")

    for index, scrollbar in enumerate(app.tree_xscrollbars):
        app.notebook.select(index)
        root.update()
        assert_visible_inside(root, scrollbar, f"{size} table horizontal scrollbar {index}", min_height=12)

    app.alerted_keys.clear()
    app.show_trade_alert(
        [
            {
                "side": "买入",
                "action": "BUY_NOW",
                "ticker": f"300{len(size):03d}",
                "name": "布局测试",
                "latest_price": 10.0,
                "reason": "弹窗按钮布局测试。",
            }
        ]
    )
    root.update()
    popups = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
    assert popups, f"{size} did not create alert popup"
    popup = popups[0]
    popup_buttons = [child for child in iter_children(popup) if isinstance(child, ttk.Button)]
    assert len(popup_buttons) >= 2, f"{size} popup buttons missing"
    for index, button in enumerate(popup_buttons):
        assert_visible_inside(popup, button, f"{size} popup button {index}")

    root.destroy()


def main() -> int:
    for size in SIZES:
        smoke_size(size)
        print(f"ok {size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
