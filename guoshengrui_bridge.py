#!/usr/bin/env python3
"""Best-effort Windows bridge for focusing Guoshengrui and jumping to a ticker."""

from __future__ import annotations

import ctypes
import datetime as dt
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from ctypes import wintypes

from trade_quantity import calculate_buy_quantity, format_buy_quantity_plan


DEFAULT_GUOSHENGRUI_EXE = Path(r"C:\zd_gszq_gm\TdxW.exe")
MAIN_WINDOW_CLASS = "TdxW_MainFrame_Class"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
SW_SHOW = 5
QUOTE_FOCUS_REL_X = 700
QUOTE_FOCUS_REL_Y = 260
TRADE_CONTEXT_REL_X = 700
TRADE_CONTEXT_REL_Y = 260
TRADE_CONTEXT_ITEM_HEIGHT = 20
TRADE_NAV_REL_X = 490
TRADE_NAV_REL_Y = 24
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
VK_F1 = 0x70
VK_F2 = 0x71
VK_F12 = 0x7B
VK_NUMPAD0 = 0x60
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
ASFW_ANY = -1
DANGEROUS_WINDOW_KEYWORDS = (
    "委托",
    "买入",
    "卖出",
    "撤单",
    "下单",
    "银证",
    "融资融券",
    "资金密码",
    "交易密码",
)
LOGIN_OR_BLOCKING_DIALOG_KEYWORDS = (
    "登录",
    "登陆",
    "验证码",
    "交易密码",
    "资金密码",
    "客户号",
    "资金账号",
    "账号",
    "密码",
    "风险提示",
    "提示",
    "确认",
)
EXPORT_FILENAME_KEYWORDS = (
    "资金股份",
    "股份查询",
    "持仓",
    "资产",
)
ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


@dataclass(frozen=True)
class GuoshengruiJumpResult:
    ok: bool
    ticker: str
    message: str
    code: str
    hwnd: int = 0
    launched: bool = False
    title: str = ""
    class_name: str = ""


@dataclass(frozen=True)
class WindowCandidate:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process_path: str
    score: int


@dataclass(frozen=True)
class FlashOrderDialog:
    hwnd: int
    title: str
    side: str
    ticker_visible: bool


@dataclass(frozen=True)
class GuoshengruiHolding:
    ticker: str
    name: str
    shares: float
    sellable_shares: float
    cost_price: float
    latest_price: float
    market_value: float
    floating_pnl: float
    pnl_pct: float


@dataclass(frozen=True)
class GuoshengruiHoldingsResult:
    ok: bool
    message: str
    code: str
    cash_balance: float = 0.0
    cash_available: float = 0.0
    cash_withdrawable: float = 0.0
    holdings_value: float = 0.0
    total_assets: float = 0.0
    floating_pnl: float = 0.0
    positions: tuple[GuoshengruiHolding, ...] = ()
    export_path: str = ""
    diagnostics: tuple[str, ...] = ()


def normalize_ticker(value: object) -> str:
    digits = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    return digits[:6]


def is_windows() -> bool:
    return sys.platform.startswith("win")


def _user32() -> ctypes.WinDLL:
    return ctypes.WinDLL("user32", use_last_error=True)


def _kernel32() -> ctypes.WinDLL:
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _configure_winapi() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    user32 = _user32()
    kernel32 = _kernel32()
    user32.EnumWindows.argtypes = [ENUM_WINDOWS_PROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, ENUM_WINDOWS_PROC, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.SetWindowTextW.restype = wintypes.BOOL
    user32.GetCurrentThreadId = kernel32.GetCurrentThreadId
    user32.GetCurrentThreadId.argtypes = []
    user32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, wintypes.ULONG]
    user32.keybd_event.restype = None
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.ULONG]
    user32.mouse_event.restype = None
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE
    return user32, kernel32


def _window_text(user32: ctypes.WinDLL, hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def _class_name(user32: ctypes.WinDLL, hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def _window_rect(user32: ctypes.WinDLL, hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _window_pid(user32: ctypes.WinDLL, hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_path(kernel32: ctypes.WinDLL, pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(4096)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _visible_tdx_dialog_summaries(limit: int = 5) -> tuple[str, ...]:
    if not is_windows():
        return ()
    user32, kernel32 = _configure_winapi()
    dialogs: list[str] = []

    def collect(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = _class_name(user32, hwnd)
        title = _window_text(user32, hwnd)
        pid = _window_pid(user32, hwnd)
        process_path = _process_path(kernel32, pid)
        if Path(process_path).name.lower() != "tdxw.exe":
            return True
        if class_name == MAIN_WINDOW_CLASS:
            return True
        text = title or class_name
        if not text:
            return True
        dialogs.append(f"{text} [{class_name}]")
        return len(dialogs) < max(1, limit)

    user32.EnumWindows(ENUM_WINDOWS_PROC(collect), 0)
    return tuple(dialogs[:limit])


def _blocking_dialog_hint() -> str:
    dialogs = _visible_tdx_dialog_summaries()
    if not dialogs:
        return ""
    joined = "；".join(dialogs)
    if any(keyword in joined for keyword in LOGIN_OR_BLOCKING_DIALOG_KEYWORDS):
        return f"检测到国盛睿弹窗：{joined}"
    return f"国盛睿可见弹窗：{joined}"


def _failure_result(message: str, code: str, *diagnostics: object) -> GuoshengruiHoldingsResult:
    clean_diagnostics = tuple(str(item) for item in diagnostics if str(item or "").strip())
    detail = f"{message}（失败码：{code}）"
    if clean_diagnostics:
        detail = f"{detail}；" + "；".join(clean_diagnostics)
    return GuoshengruiHoldingsResult(False, detail, code, diagnostics=clean_diagnostics)


def _has_dangerous_title(title: str, class_name: str = "") -> bool:
    combined = f"{title} {class_name}"
    return any(keyword in combined for keyword in DANGEROUS_WINDOW_KEYWORDS)


def _candidate_score(title: str, class_name: str, process_path: str) -> int:
    score = 0
    process_name = Path(process_path).name.lower()
    if class_name == MAIN_WINDOW_CLASS:
        score += 100
    if "国盛睿" in title:
        score += 80
    if process_name == "tdxw.exe":
        score += 60
    if "通达信" in title:
        score += 20
    return score


def find_guoshengrui_window() -> WindowCandidate | None:
    if not is_windows():
        return None
    user32, kernel32 = _configure_winapi()
    candidates: list[WindowCandidate] = []
    def collect(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(user32, hwnd)
        class_name = _class_name(user32, hwnd)
        if _has_dangerous_title(title, class_name):
            return True
        pid = _window_pid(user32, hwnd)
        process_path = _process_path(kernel32, pid)
        score = _candidate_score(title, class_name, process_path)
        if score > 0:
            candidates.append(WindowCandidate(hwnd, title, class_name, pid, process_path, score))
        return True

    user32.EnumWindows(ENUM_WINDOWS_PROC(collect), 0)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.score, reverse=True)[0]


def _wait_for_window(timeout_seconds: float) -> WindowCandidate | None:
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() <= deadline:
        candidate = find_guoshengrui_window()
        if candidate:
            return candidate
        time.sleep(0.25)
    return None


def _set_clipboard_text(text: str) -> bool:
    user32, kernel32 = _configure_winapi()
    if not user32.OpenClipboard(None):
        return False
    handle = None
    try:
        if not user32.EmptyClipboard():
            return False
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(locked, data, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        handle = None
        return True
    finally:
        user32.CloseClipboard()


def _foreground_hwnd(user32: ctypes.WinDLL) -> int:
    return int(user32.GetForegroundWindow() or 0)


def _focus_window(hwnd: int) -> bool:
    user32, _kernel32 = _configure_winapi()
    user32.AllowSetForegroundWindow(ASFW_ANY)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    if _foreground_hwnd(user32) == hwnd:
        return True

    foreground = _foreground_hwnd(user32)
    current_thread = user32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached_foreground = False
    attached_target = False
    try:
        if foreground_thread:
            attached_foreground = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
        if target_thread:
            attached_target = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        return _foreground_hwnd(user32) == hwnd
    finally:
        if attached_target and target_thread:
            user32.AttachThreadInput(current_thread, target_thread, False)
        if attached_foreground and foreground_thread:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


def _foreground_is_safe(hwnd: int) -> tuple[bool, str]:
    user32, _kernel32 = _configure_winapi()
    foreground = _foreground_hwnd(user32)
    title = _window_text(user32, foreground) if foreground else ""
    class_name = _class_name(user32, foreground) if foreground else ""
    if _has_dangerous_title(title, class_name):
        return False, f"检测到交易/委托相关窗口：{title or class_name}"
    if foreground != hwnd:
        return False, "未能确认国盛睿主窗口处于前台"
    return True, ""


def _tap_key(vk_code: int) -> None:
    user32, _kernel32 = _configure_winapi()
    user32.keybd_event(vk_code, 0, 0, 0)
    time.sleep(0.025)
    user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)


def _click_screen_point(x: int, y: int) -> None:
    user32, _kernel32 = _configure_winapi()
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.04)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.035)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _right_click_screen_point(x: int, y: int) -> None:
    user32, _kernel32 = _configure_winapi()
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.04)
    user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
    time.sleep(0.035)
    user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)


def _click_window_relative(hwnd: int, x: int, y: int) -> None:
    user32, _kernel32 = _configure_winapi()
    left, top, _right, _bottom = _window_rect(user32, hwnd)
    _click_screen_point(left + x, top + y)


def _right_click_window_relative(hwnd: int, x: int, y: int) -> None:
    user32, _kernel32 = _configure_winapi()
    left, top, _right, _bottom = _window_rect(user32, hwnd)
    _right_click_screen_point(left + x, top + y)


def _context_menu_rect() -> tuple[int, int, int, int] | None:
    user32, _kernel32 = _configure_winapi()
    menus: list[tuple[int, int, int, int]] = []

    def collect(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd) and _class_name(user32, hwnd) == "#32768":
            left, top, right, bottom = _window_rect(user32, hwnd)
            if right - left > 50 and bottom - top > 30:
                menus.append((left, top, right, bottom))
        return True

    user32.EnumWindows(ENUM_WINDOWS_PROC(collect), 0)
    return menus[0] if menus else None


def _switch_to_time_share_chart(hwnd: int) -> None:
    # In Guoshengrui V1.49 both Esc and the visible "分时" label can toggle
    # back to the quote list when the stock is already on a time-share chart.
    # Keep this step conservative: focus the chart canvas, then verify the
    # actual ticker from the flash order dialog.
    _click_window_relative(hwnd, QUOTE_FOCUS_REL_X, QUOTE_FOCUS_REL_Y)
    time.sleep(0.08)


def _dialog_contains_text(hwnd: int, text: str) -> bool:
    user32, _kernel32 = _configure_winapi()
    if _window_text(user32, hwnd) == text:
        return True
    found = False

    def inspect(child_hwnd: int, _lparam: int) -> bool:
        nonlocal found
        if not user32.IsWindowVisible(child_hwnd):
            return True
        if _window_text(user32, child_hwnd).strip() == text:
            found = True
            return False
        return True

    user32.EnumChildWindows(hwnd, ENUM_WINDOWS_PROC(inspect), 0)
    return found


def _parse_number_text(value: object) -> float | None:
    text = str(value or "").replace(",", "").replace("，", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _visible_dialog_children(hwnd: int) -> list[tuple[int, int, int, int, str, str, int]]:
    user32, _kernel32 = _configure_winapi()
    children: list[tuple[int, int, int, int, str, str, int]] = []

    def collect(child_hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(child_hwnd):
            return True
        left, top, right, bottom = _window_rect(user32, child_hwnd)
        if right <= left or bottom <= top:
            return True
        children.append((left, top, right, bottom, _class_name(user32, child_hwnd), _window_text(user32, child_hwnd), child_hwnd))
        return True

    user32.EnumChildWindows(hwnd, ENUM_WINDOWS_PROC(collect), 0)
    return sorted(children, key=lambda item: (item[1], item[0]))


def _read_number_after_label(hwnd: int, label: str) -> float | None:
    children = _visible_dialog_children(hwnd)
    for left, top, right, bottom, _class_name_value, title, _child in children:
        if title.strip() != label:
            continue
        center_y = (top + bottom) // 2
        candidates: list[tuple[int, float]] = []
        for c_left, c_top, c_right, c_bottom, _c_class, c_title, _c_hwnd in children:
            if c_left <= right or abs(((c_top + c_bottom) // 2) - center_y) > 10:
                continue
            number = _parse_number_text(c_title)
            if number is not None:
                candidates.append((c_left, number))
        if candidates:
            return sorted(candidates, key=lambda item: item[0])[0][1]
    return None


def _find_labeled_edit(hwnd: int, label: str) -> int:
    children = _visible_dialog_children(hwnd)
    for left, top, right, bottom, _class_name_value, title, _child in children:
        if title.strip() != label:
            continue
        center_y = (top + bottom) // 2
        candidates: list[tuple[int, int]] = []
        for c_left, c_top, c_right, c_bottom, c_class, _c_title, c_hwnd in children:
            if c_class != "Edit" or c_left <= right:
                continue
            if abs(((c_top + c_bottom) // 2) - center_y) <= 10:
                candidates.append((c_left, c_hwnd))
        if candidates:
            return sorted(candidates, key=lambda item: item[0])[0][1]
    return 0


def _set_edit_text(hwnd: int, text: str) -> bool:
    user32, _kernel32 = _configure_winapi()
    if not user32.SetWindowTextW(hwnd, str(text)):
        return False
    time.sleep(0.15)
    return True


def _fill_flash_buy_quantity(
    dialog_hwnd: int,
    *,
    account_cash_amount: float,
    account_holdings_value: float,
    account_total_assets: float | None,
    price: float,
    suggested_capital_pct: float,
    existing_shares: float,
) -> tuple[bool, int, str]:
    max_buy = _read_number_after_label(dialog_hwnd, "最大可买")
    plan = calculate_buy_quantity(
        account_cash_amount=account_cash_amount,
        account_holdings_value=account_holdings_value,
        account_total_assets=account_total_assets,
        price=price,
        suggested_capital_pct=suggested_capital_pct,
        existing_shares=existing_shares,
        max_buy_shares=max_buy,
    )
    plan_text = format_buy_quantity_plan(plan)
    if plan.planned_shares <= 0:
        return False, 0, plan_text
    quantity_edit = _find_labeled_edit(dialog_hwnd, "买入数量")
    if not quantity_edit:
        return False, plan.planned_shares, f"{plan_text}；未找到买入数量输入框"
    if not _set_edit_text(quantity_edit, str(plan.planned_shares)):
        return False, plan.planned_shares, f"{plan_text}；写入数量失败"
    return True, plan.planned_shares, plan_text


def _find_flash_order_dialog(side: str = "", ticker: str = "") -> FlashOrderDialog | None:
    user32, kernel32 = _configure_winapi()
    target_title = {"buy": "闪电买入", "sell": "闪电卖出"}.get(side, "")
    dialogs: list[FlashOrderDialog] = []

    def collect(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(user32, hwnd)
        if title not in {"闪电买入", "闪电卖出"}:
            return True
        if target_title and title != target_title:
            return True
        class_name = _class_name(user32, hwnd)
        pid = _window_pid(user32, hwnd)
        process_path = _process_path(kernel32, pid)
        if class_name != "#32770" or Path(process_path).name.lower() != "tdxw.exe":
            return True
        dialog_side = "buy" if title == "闪电买入" else "sell"
        ticker_visible = bool(ticker and _dialog_contains_text(hwnd, ticker))
        if ticker and not ticker_visible:
            return True
        dialogs.append(FlashOrderDialog(hwnd, title, dialog_side, ticker_visible))
        return True

    user32.EnumWindows(ENUM_WINDOWS_PROC(collect), 0)
    return dialogs[0] if dialogs else None


def _any_flash_order_dialog() -> FlashOrderDialog | None:
    return _find_flash_order_dialog()


def _wait_for_flash_order_dialog(side: str, ticker: str, timeout_seconds: float = 2.0) -> FlashOrderDialog | None:
    deadline = time.monotonic() + max(0.2, timeout_seconds)
    while time.monotonic() <= deadline:
        dialog = _find_flash_order_dialog(side, ticker)
        if dialog:
            return dialog
        time.sleep(0.12)
    return None


def _normalized_button_text(text: str) -> str:
    return "".join(str(text or "").split())


def _trade_panel_visible(hwnd: int) -> bool:
    if _trade_panel_rect(hwnd) is not None:
        return True
    user32, _kernel32 = _configure_winapi()
    found = False

    def inspect(child_hwnd: int, _lparam: int) -> bool:
        nonlocal found
        if not user32.IsWindowVisible(child_hwnd):
            return True
        if _class_name(user32, child_hwnd) != "Button":
            return True
        if _normalized_button_text(_window_text(user32, child_hwnd)) not in {"买入下单", "卖出下单"}:
            return True
        left, top, right, bottom = _window_rect(user32, child_hwnd)
        if right - left > 30 and bottom - top > 12 and right > 0 and bottom > 0:
            found = True
            return False
        return True

    user32.EnumChildWindows(hwnd, ENUM_WINDOWS_PROC(inspect), 0)
    return found


def _trade_panel_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    user32, _kernel32 = _configure_winapi()
    panel_rect: tuple[int, int, int, int] | None = None

    def inspect(child_hwnd: int, _lparam: int) -> bool:
        nonlocal panel_rect
        if not user32.IsWindowVisible(child_hwnd):
            return True
        title = _window_text(user32, child_hwnd)
        class_name = _class_name(user32, child_hwnd)
        left, top, right, bottom = _window_rect(user32, child_hwnd)
        width = right - left
        height = bottom - top
        visible_rect = width > 300 and height > 120 and right > 0 and bottom > 0
        if title == "通达信网上交易V6" and visible_rect:
            panel_rect = (left, top, right, bottom)
            return False
        return True

    user32.EnumChildWindows(hwnd, ENUM_WINDOWS_PROC(inspect), 0)
    return panel_rect


def _wait_for_trade_panel(hwnd: int, timeout_seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + max(0.2, timeout_seconds)
    while time.monotonic() <= deadline:
        if _trade_panel_visible(hwnd):
            return True
        time.sleep(0.15)
    return False


def _trade_side_visible(hwnd: int, side: str) -> bool:
    target = "买入下单" if side == "buy" else "卖出下单"
    user32, _kernel32 = _configure_winapi()
    found = False

    def inspect(child_hwnd: int, _lparam: int) -> bool:
        nonlocal found
        if not user32.IsWindowVisible(child_hwnd):
            return True
        if _normalized_button_text(_window_text(user32, child_hwnd)) != target:
            return True
        left, top, right, bottom = _window_rect(user32, child_hwnd)
        if right - left > 30 and bottom - top > 12 and right > 0 and bottom > 0:
            found = True
            return False
        return True

    user32.EnumChildWindows(hwnd, ENUM_WINDOWS_PROC(inspect), 0)
    return found


def _send_keyboard_command(hwnd: int, command: str, *, use_numpad: bool = False) -> None:
    _click_window_relative(hwnd, QUOTE_FOCUS_REL_X, QUOTE_FOCUS_REL_Y)
    time.sleep(0.08)
    _tap_key(VK_ESCAPE)
    time.sleep(0.08)
    for char in command:
        if char.isdigit():
            vk_code = VK_NUMPAD0 + int(char) if use_numpad else ord(char)
        elif char == ".":
            vk_code = 0xBE
        else:
            vk_code = ord(char.upper())
        _tap_key(vk_code)
        time.sleep(0.04)
    time.sleep(0.1)
    _tap_key(VK_RETURN)


def _click_trade_side_button(hwnd: int, side: str) -> bool:
    panel_rect = _trade_panel_rect(hwnd)
    if panel_rect:
        left, top, _right, _bottom = panel_rect
        row_y = top + (34 if side == "buy" else 55)
        _click_screen_point(left + 38, row_y)
        time.sleep(0.55)
        if _trade_side_visible(hwnd, side):
            return True

    target = "买入" if side == "buy" else "卖出"
    user32, _kernel32 = _configure_winapi()
    candidates: list[tuple[int, int, int, int, int]] = []

    def collect(child_hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(child_hwnd):
            return True
        if _class_name(user32, child_hwnd) != "Button":
            return True
        title = _normalized_button_text(_window_text(user32, child_hwnd))
        if title != target:
            return True
        left, top, right, bottom = _window_rect(user32, child_hwnd)
        if right - left < 20 or bottom - top < 12:
            return True
        candidates.append((left, top, right, bottom, child_hwnd))
        return True

    user32.EnumChildWindows(hwnd, ENUM_WINDOWS_PROC(collect), 0)
    if not candidates:
        return False
    left, top, right, bottom, _child = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    _click_screen_point((left + right) // 2, (top + bottom) // 2)
    time.sleep(0.35)
    return True


def _click_child_button(hwnd: int, button_text: str, *, contains: bool = False) -> bool:
    user32, _kernel32 = _configure_winapi()
    target = _normalized_button_text(button_text)
    candidates: list[tuple[int, int, int, int, int]] = []

    def collect(child_hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(child_hwnd):
            return True
        if _class_name(user32, child_hwnd) != "Button":
            return True
        title = _normalized_button_text(_window_text(user32, child_hwnd))
        if contains:
            matched = target in title
        else:
            matched = title == target
        if not matched:
            return True
        left, top, right, bottom = _window_rect(user32, child_hwnd)
        if right - left < 20 or bottom - top < 10:
            return True
        candidates.append((left, top, right, bottom, child_hwnd))
        return True

    user32.EnumChildWindows(hwnd, ENUM_WINDOWS_PROC(collect), 0)
    if not candidates:
        return False
    left, top, right, bottom, _child = sorted(candidates, key=lambda item: (item[1], item[0]))[0]
    _click_screen_point((left + right) // 2, (top + bottom) // 2)
    time.sleep(0.35)
    return True


def _click_child_button_any(hwnd: int, labels: tuple[str, ...], *, contains: bool = False) -> bool:
    for label in labels:
        if _click_child_button(hwnd, label, contains=contains):
            return True
    return False


def _find_output_dialog() -> int:
    user32, _kernel32 = _configure_winapi()
    found = 0

    def collect(hwnd: int, _lparam: int) -> bool:
        nonlocal found
        title = _window_text(user32, hwnd)
        class_name = _class_name(user32, hwnd)
        if user32.IsWindowVisible(hwnd) and class_name == "#32770" and "输出" in title:
            found = hwnd
            return False
        return True

    user32.EnumWindows(ENUM_WINDOWS_PROC(collect), 0)
    return found


def _wait_for_output_dialog(timeout_seconds: float = 2.0) -> int:
    deadline = time.monotonic() + max(0.2, timeout_seconds)
    while time.monotonic() <= deadline:
        hwnd = _find_output_dialog()
        if hwnd:
            return hwnd
        time.sleep(0.12)
    return 0


def _confirm_output_dialog(dialog_hwnd: int) -> bool:
    return _click_child_button_any(dialog_hwnd, ("确  定", "确定", "确认", "保存", "开始"), contains=False)


def _documents_dir() -> Path:
    user_profile = Path.home()
    return user_profile / "Documents"


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return Path(value)
    except (OSError, ValueError):
        return None


def _holdings_export_search_roots(candidate: WindowCandidate | None = None) -> tuple[Path, ...]:
    roots: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser()
        except (OSError, ValueError):
            return
        if resolved not in roots:
            roots.append(resolved)

    home = Path.home()
    add(_documents_dir())
    add(home / "文档")
    add(home / "Desktop")
    add(home / "桌面")
    add(home / "Downloads")
    add(home / "下载")
    add(Path.cwd())
    add(_path_from_env("USERPROFILE"))
    add(_path_from_env("TEMP"))
    add(_path_from_env("TMP"))

    exe_paths = [DEFAULT_GUOSHENGRUI_EXE]
    if candidate and candidate.process_path:
        exe_paths.append(Path(candidate.process_path))
    for exe_path in exe_paths:
        exe_dir = exe_path.parent
        add(exe_dir)
        add(exe_dir / "T0002")
        add(exe_dir / "T0002" / "export")
        add(exe_dir / "T0002" / "Export")
        add(exe_dir / "export")
        add(exe_dir / "Export")

    return tuple(path for path in roots if path.exists() and path.is_dir())


def _export_search_hint(candidate: WindowCandidate | None = None, limit: int = 8) -> str:
    roots = _holdings_export_search_roots(candidate)
    if not roots:
        return "未找到可搜索的导出目录"
    shown = [str(path) for path in roots[:limit]]
    suffix = f" 等{len(roots)}个目录" if len(roots) > limit else ""
    return "已搜索导出目录：" + " | ".join(shown) + suffix


def _export_file_name_matches(path: Path, *, broad_root: bool) -> bool:
    name = path.name
    if any(keyword in name for keyword in EXPORT_FILENAME_KEYWORDS):
        return True
    return not broad_root and path.suffix.lower() == ".txt"


def _latest_holdings_export_file(since_ts: float, candidate: WindowCandidate | None = None) -> Path | None:
    candidates: list[Path] = []
    for folder in _holdings_export_search_roots(candidate):
        broad_root = folder in {
            _documents_dir(),
            Path.home() / "文档",
            Path.home() / "Desktop",
            Path.home() / "桌面",
            Path.home() / "Downloads",
            Path.home() / "下载",
            Path.cwd(),
            _path_from_env("USERPROFILE"),
            _path_from_env("TEMP"),
            _path_from_env("TMP"),
        }
        try:
            for path in folder.glob("*.txt"):
                if path.is_file() and _export_file_name_matches(path, broad_root=broad_root):
                    candidates.append(path)
        except (OSError, ValueError):
            continue
    fresh = [path for path in candidates if path.is_file() and path.stat().st_mtime >= since_ts - 2.0]
    if not fresh:
        return None
    return max(fresh, key=lambda path: path.stat().st_mtime)


def _read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("gbk", "utf-8-sig", "utf-8", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("gbk", errors="replace")


def _parse_export_float(value: object) -> float:
    text = str(value or "").replace(",", "").replace("，", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def parse_guoshengrui_holdings_export(text: str, export_path: str = "") -> GuoshengruiHoldingsResult:
    cash_balance = 0.0
    cash_available = 0.0
    cash_withdrawable = 0.0
    holdings_value = 0.0
    total_assets = 0.0
    floating_pnl = 0.0
    positions: list[GuoshengruiHolding] = []

    lines = [line.rstrip() for line in str(text or "").splitlines()]
    for line in lines[:8]:
        if "人民币" not in line:
            continue
        pairs = dict(re.findall(r"([\u4e00-\u9fa5]+):\s*(-?\d+(?:\.\d+)?)", line))
        cash_balance = _parse_export_float(pairs.get("余额"))
        cash_available = _parse_export_float(pairs.get("可用"))
        cash_withdrawable = _parse_export_float(pairs.get("可取"))
        holdings_value = _parse_export_float(pairs.get("参考市值"))
        total_assets = _parse_export_float(pairs.get("资产"))
        floating_pnl = _parse_export_float(pairs.get("参考盈亏"))
        break

    for line in lines:
        stripped = line.strip()
        if not re.match(r"^\d{6}\s+", stripped):
            continue
        parts = re.split(r"\s{2,}", stripped)
        if len(parts) < 7:
            continue
        positions.append(
            GuoshengruiHolding(
                ticker=normalize_ticker(parts[0]),
                name=str(parts[1]).strip(),
                shares=_parse_export_float(parts[2]),
                sellable_shares=_parse_export_float(parts[3]),
                cost_price=_parse_export_float(parts[4]),
                latest_price=_parse_export_float(parts[5]),
                market_value=_parse_export_float(parts[6]),
                floating_pnl=_parse_export_float(parts[7]) if len(parts) > 7 else 0.0,
                pnl_pct=_parse_export_float(parts[8]) if len(parts) > 8 else 0.0,
            )
        )

    if not positions and holdings_value <= 0 and cash_available <= 0:
        return GuoshengruiHoldingsResult(
            False,
            f"未能解析国盛睿持仓导出结果（失败码：parse_failed）；文件：{export_path or '未知'}",
            "parse_failed",
            export_path=export_path,
            diagnostics=(f"文件：{export_path}",) if export_path else (),
        )
    if total_assets <= 0:
        total_assets = cash_available + holdings_value
    return GuoshengruiHoldingsResult(
        True,
        f"已解析国盛睿持仓：{len(positions)} 条，可用现金 {cash_available:.2f}，持仓市值 {holdings_value:.2f}",
        "ok",
        cash_balance=cash_balance,
        cash_available=cash_available,
        cash_withdrawable=cash_withdrawable,
        holdings_value=holdings_value,
        total_assets=total_assets,
        floating_pnl=floating_pnl,
        positions=tuple(position for position in positions if position.ticker),
        export_path=export_path,
    )


def _open_holdings_output_dialog(hwnd: int) -> int:
    if _find_output_dialog():
        return _find_output_dialog()
    _click_child_button_any(hwnd, ("持仓", "股份持仓", "资金股份", "查询"), contains=True)
    time.sleep(0.4)
    if _click_child_button_any(hwnd, ("输 出", "输出", "导出"), contains=True):
        return _wait_for_output_dialog()
    return 0


def export_guoshengrui_holdings(timeout_seconds: float = 12.0) -> GuoshengruiHoldingsResult:
    if not is_windows():
        return _failure_result("只能在 Windows 上扫描国盛睿持仓", "not_windows")
    candidate = find_guoshengrui_window()
    if candidate is None:
        exe = DEFAULT_GUOSHENGRUI_EXE
        if not exe.exists():
            return _failure_result(f"未找到国盛睿主程序，请确认是否安装在 {exe.parent}", "missing_executable")
        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return _failure_result(f"启动国盛睿失败：{exc}", "launch_failed")
        candidate = _wait_for_window(timeout_seconds)
    if candidate is None:
        return _failure_result("未找到国盛睿主窗口", "no_window", _blocking_dialog_hint())
    if not _focus_window(candidate.hwnd):
        return _failure_result(
            "未能切到国盛睿前台",
            "foreground_failed",
            f"窗口：{candidate.title or candidate.class_name}",
            _blocking_dialog_hint(),
        )
    if not _open_trade_panel(candidate.hwnd, "buy"):
        return _failure_result(
            "未能打开国盛睿交易/持仓面板",
            "trade_panel_not_found",
            f"窗口：{candidate.title or candidate.class_name}",
            _blocking_dialog_hint(),
        )
    started = time.time()
    dialog = _open_holdings_output_dialog(candidate.hwnd)
    if not dialog:
        return _failure_result(
            "未能打开国盛睿持仓输出对话框",
            "output_dialog_not_found",
            f"窗口：{candidate.title or candidate.class_name}",
            _blocking_dialog_hint(),
        )
    if not _confirm_output_dialog(dialog):
        return _failure_result(
            "未能确认国盛睿持仓输出",
            "output_confirm_failed",
            f"输出窗口：{_window_text(_configure_winapi()[0], dialog) or dialog}",
            _blocking_dialog_hint(),
        )
    deadline = time.monotonic() + 4.0
    export_path: Path | None = None
    while time.monotonic() <= deadline:
        export_path = _latest_holdings_export_file(started, candidate)
        if export_path and export_path.exists():
            break
        time.sleep(0.2)
    if not export_path:
        return _failure_result(
            "国盛睿未生成持仓导出文件",
            "export_file_not_found",
            _export_search_hint(candidate),
            _blocking_dialog_hint(),
        )
    return parse_guoshengrui_holdings_export(_read_text_file(export_path), str(export_path))


def _open_trade_panel_from_context_menu(hwnd: int, side: str) -> bool:
    _click_window_relative(hwnd, QUOTE_FOCUS_REL_X, QUOTE_FOCUS_REL_Y)
    time.sleep(0.08)
    _right_click_window_relative(hwnd, TRADE_CONTEXT_REL_X, TRADE_CONTEXT_REL_Y)
    time.sleep(0.25)
    menu = _context_menu_rect()
    if not menu:
        _tap_key(VK_ESCAPE)
        return False
    left, top, _right, _bottom = menu
    row_index = 0 if side == "buy" else 1
    _click_screen_point(left + 70, top + 14 + row_index * TRADE_CONTEXT_ITEM_HEIGHT)
    return _wait_for_trade_panel(hwnd, timeout_seconds=1.2)


def _open_flash_order_from_context_menu(hwnd: int, side: str, ticker: str) -> FlashOrderDialog | None:
    existing = _any_flash_order_dialog()
    if existing:
        if existing.side == side and (not ticker or existing.ticker_visible or _dialog_contains_text(existing.hwnd, ticker)):
            return existing
        return None
    _switch_to_time_share_chart(hwnd)
    _right_click_window_relative(hwnd, TRADE_CONTEXT_REL_X, TRADE_CONTEXT_REL_Y)
    time.sleep(0.25)
    menu = _context_menu_rect()
    if not menu:
        _tap_key(VK_ESCAPE)
        return None
    left, top, _right, _bottom = menu
    row_index = 0 if side == "buy" else 1
    _click_screen_point(left + 70, top + 14 + row_index * TRADE_CONTEXT_ITEM_HEIGHT)
    dialog = _wait_for_flash_order_dialog(side, ticker, timeout_seconds=2.2)
    if dialog:
        return dialog
    wrong_dialog = _any_flash_order_dialog()
    if wrong_dialog:
        _tap_key(VK_ESCAPE)
        time.sleep(0.2)
    return None


def _open_trade_panel(hwnd: int, side: str) -> bool:
    if _trade_panel_visible(hwnd):
        return True
    if _open_trade_panel_from_context_menu(hwnd, side):
        return True
    _tap_key(VK_F12)
    if _wait_for_trade_panel(hwnd, timeout_seconds=1.2):
        return True
    _send_keyboard_command(hwnd, "20", use_numpad=False)
    if _wait_for_trade_panel(hwnd, timeout_seconds=1.2):
        return True
    _send_keyboard_command(hwnd, "20", use_numpad=True)
    if _wait_for_trade_panel(hwnd, timeout_seconds=1.2):
        return True
    _click_window_relative(hwnd, TRADE_NAV_REL_X, TRADE_NAV_REL_Y)
    return _wait_for_trade_panel(hwnd, timeout_seconds=1.2)


def _input_modes_for_ticker(ticker: str) -> tuple[bool, ...]:
    # Guoshengrui/Tongdaxin treats some leading digit pairs as global shortcuts
    # when they arrive from the wrong key group. Shanghai 600/688 codes are
    # reliable from numpad; ChiNext 300 codes are reliable from the main row.
    if ticker.startswith(("6", "9")):
        return (True, False)
    if ticker.startswith("3"):
        return (False, True)
    return (False, True)


def _type_ticker_and_enter(ticker: str, *, use_numpad: bool) -> None:
    _tap_key(VK_ESCAPE)
    time.sleep(0.08)
    for digit in ticker:
        vk_code = VK_NUMPAD0 + int(digit) if use_numpad else ord(digit)
        _tap_key(vk_code)
        time.sleep(0.045)
    time.sleep(0.12)
    _tap_key(VK_RETURN)


def _title_after_input(hwnd: int, wait_seconds: float = 0.85) -> str:
    time.sleep(wait_seconds)
    user32, _kernel32 = _configure_winapi()
    return _window_text(user32, hwnd)


def _title_suggests_failed_jump(title: str) -> bool:
    return any(
        marker in title
        for marker in ("行情报价", "全部Ａ股", "全部A股", "版面-", "系统页面")
    )


def normalize_trade_side(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"buy", "b", "买入", "买"}:
        return "buy"
    if text in {"sell", "s", "卖出", "卖"}:
        return "sell"
    return ""


def open_guoshengrui_for_ticker(
    ticker_text: object,
    *,
    exe_path: str | Path = DEFAULT_GUOSHENGRUI_EXE,
    timeout_seconds: float = 12.0,
) -> GuoshengruiJumpResult:
    ticker = normalize_ticker(ticker_text)
    if len(ticker) != 6:
        return GuoshengruiJumpResult(False, ticker, "股票代码必须是 6 位数字", "invalid_ticker")
    if not is_windows():
        return GuoshengruiJumpResult(False, ticker, f"未能唤起国盛睿，已复制股票代码：{ticker}", "not_windows")

    exe = Path(exe_path)
    candidate = find_guoshengrui_window()
    launched = False
    if candidate is None:
        if not exe.exists():
            return GuoshengruiJumpResult(
                False,
                ticker,
                f"未找到国盛睿主程序，请确认是否安装在 {exe.parent}",
                "missing_executable",
            )
        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            launched = True
        except OSError as exc:
            return GuoshengruiJumpResult(False, ticker, f"启动国盛睿失败，已复制股票代码：{ticker}；{exc}", "launch_failed")
        candidate = _wait_for_window(timeout_seconds)
    if candidate is None:
        return GuoshengruiJumpResult(False, ticker, f"未找到国盛睿主窗口，已复制股票代码：{ticker}", "no_window", launched=launched)

    if not _set_clipboard_text(ticker):
        return GuoshengruiJumpResult(
            False,
            ticker,
            f"国盛睿已找到，但系统剪贴板写入失败：{ticker}",
            "clipboard_failed",
            hwnd=candidate.hwnd,
            launched=launched,
            title=candidate.title,
            class_name=candidate.class_name,
        )
    if not _focus_window(candidate.hwnd):
        return GuoshengruiJumpResult(
            False,
            ticker,
            f"未能切到国盛睿前台，已复制股票代码：{ticker}",
            "foreground_failed",
            hwnd=candidate.hwnd,
            launched=launched,
            title=candidate.title,
            class_name=candidate.class_name,
        )
    safe, safety_message = _foreground_is_safe(candidate.hwnd)
    if not safe:
        return GuoshengruiJumpResult(
            False,
            ticker,
            f"{safety_message}，已停止自动输入并复制股票代码：{ticker}",
            "dangerous_foreground",
            hwnd=candidate.hwnd,
            launched=launched,
            title=candidate.title,
            class_name=candidate.class_name,
        )
    time.sleep(0.15)
    _click_window_relative(candidate.hwnd, QUOTE_FOCUS_REL_X, QUOTE_FOCUS_REL_Y)
    time.sleep(0.08)
    before_title = _title_after_input(candidate.hwnd, wait_seconds=0.0)
    final_title = before_title
    for use_numpad in _input_modes_for_ticker(ticker):
        _type_ticker_and_enter(ticker, use_numpad=use_numpad)
        final_title = _title_after_input(candidate.hwnd)
        if final_title != before_title and not _title_suggests_failed_jump(final_title):
            break
        if _title_suggests_failed_jump(final_title):
            before_title = final_title
            continue
        if final_title == before_title:
            continue

    if _title_suggests_failed_jump(final_title):
        return GuoshengruiJumpResult(
            False,
            ticker,
            f"已复制股票代码，但国盛睿未确认跳转成功，请在键盘精灵手动粘贴：{ticker}",
            "jump_unconfirmed",
            hwnd=candidate.hwnd,
            launched=launched,
            title=final_title,
            class_name=candidate.class_name,
        )

    suffix = f"（{final_title}）" if final_title else ""
    return GuoshengruiJumpResult(
        True,
        ticker,
        f"已跳转国盛睿：{ticker}{suffix}",
        "ok",
        hwnd=candidate.hwnd,
        launched=launched,
        title=final_title or candidate.title,
        class_name=candidate.class_name,
    )


def open_guoshengrui_trade_for_ticker(
    ticker_text: object,
    side: object,
    *,
    exe_path: str | Path = DEFAULT_GUOSHENGRUI_EXE,
    timeout_seconds: float = 12.0,
    account_cash_amount: float = 0.0,
    account_holdings_value: float = 0.0,
    account_total_assets: float | None = None,
    reference_price: float = 0.0,
    suggested_capital_pct: float = 0.0,
    existing_shares: float = 0.0,
    fill_quantity: bool = False,
) -> GuoshengruiJumpResult:
    """Open Guoshengrui's flash order dialog for manual buy/sell handling.

    The flow intentionally jumps to the quote chart first, switches to the
    time-share chart, and then triggers Guoshengrui's chart context menu. It
    never inputs a ticker into the order dialog, price, Enter, or
    confirmation. When fill_quantity=True for buy-side dialogs, it only fills
    the calculated share quantity.
    """
    ticker = normalize_ticker(ticker_text)
    trade_side = normalize_trade_side(side)
    if len(ticker) != 6:
        return GuoshengruiJumpResult(False, ticker, "股票代码必须是 6 位数字", "invalid_ticker")
    if trade_side not in {"buy", "sell"}:
        return GuoshengruiJumpResult(False, ticker, "交易方向必须是买入或卖出", "invalid_side")
    if not is_windows():
        return GuoshengruiJumpResult(False, ticker, f"未能唤起国盛睿交易界面，已复制股票代码：{ticker}", "not_windows")

    quote_result = open_guoshengrui_for_ticker(ticker, exe_path=exe_path, timeout_seconds=timeout_seconds)
    launched = quote_result.launched
    if not quote_result.ok:
        return GuoshengruiJumpResult(
            False,
            ticker,
            f"未能先定位到目标股票，已复制代码：{ticker}；{quote_result.message}",
            "quote_jump_failed",
            hwnd=quote_result.hwnd,
            launched=launched,
            title=quote_result.title,
            class_name=quote_result.class_name,
        )

    candidate = find_guoshengrui_window()
    if candidate is None:
        return GuoshengruiJumpResult(False, ticker, f"未找到国盛睿主窗口，已复制股票代码：{ticker}", "no_window", launched=launched)

    if not _set_clipboard_text(ticker):
        return GuoshengruiJumpResult(
            False,
            ticker,
            f"国盛睿已找到，但系统剪贴板写入失败：{ticker}",
            "clipboard_failed",
            hwnd=candidate.hwnd,
            launched=launched,
            title=candidate.title,
            class_name=candidate.class_name,
        )
    if not _focus_window(candidate.hwnd):
        return GuoshengruiJumpResult(
            False,
            ticker,
            f"未能切到国盛睿前台，已复制股票代码：{ticker}",
            "foreground_failed",
            hwnd=candidate.hwnd,
            launched=launched,
            title=candidate.title,
            class_name=candidate.class_name,
        )

    stale_dialog = _any_flash_order_dialog()
    if stale_dialog and not _find_flash_order_dialog(trade_side, ticker):
        return GuoshengruiJumpResult(
            False,
            ticker,
            f"检测到已有{stale_dialog.title}窗口，已复制代码：{ticker}；请先处理或关闭该窗口，再重新点击",
            "stale_flash_dialog",
            hwnd=candidate.hwnd,
            launched=launched,
            title=_title_after_input(candidate.hwnd, wait_seconds=0.0),
            class_name=candidate.class_name,
        )

    dialog = _open_flash_order_from_context_menu(candidate.hwnd, trade_side, ticker)
    if not dialog:
        return GuoshengruiJumpResult(
            False,
            ticker,
            f"已定位到分时图并复制代码，但未能从右键菜单打开闪电交易窗口：{ticker}；请手动在图表区右键选择闪电买入/闪电卖出",
            "flash_dialog_not_found",
            hwnd=candidate.hwnd,
            launched=launched,
            title=_title_after_input(candidate.hwnd, wait_seconds=0.0),
            class_name=candidate.class_name,
        )

    user32, _kernel32 = _configure_winapi()
    foreground = _foreground_hwnd(user32)
    foreground_title = _window_text(user32, foreground) if foreground else ""
    label = "买入" if trade_side == "buy" else "卖出"
    suffix = f"（{foreground_title}）" if foreground_title else ""
    quantity_note = ""
    if fill_quantity and trade_side == "buy":
        quantity_ok, quantity, quantity_detail = _fill_flash_buy_quantity(
            dialog.hwnd,
            account_cash_amount=account_cash_amount,
            account_holdings_value=account_holdings_value,
            account_total_assets=account_total_assets,
            price=reference_price,
            suggested_capital_pct=suggested_capital_pct,
            existing_shares=existing_shares,
        )
        if quantity_ok:
            quantity_note = f"；已自动填入买入数量 {quantity} 股（{quantity_detail}）"
        else:
            quantity_note = f"；未自动填数量（{quantity_detail}）"
    return GuoshengruiJumpResult(
        True,
        ticker,
        f"已在国盛睿分时图右键打开闪电{label}窗口并复制代码：{ticker}{suffix}{quantity_note}；请手动核对代码、价格、数量后自行提交",
        "ok",
        hwnd=dialog.hwnd,
        launched=launched,
        title=foreground_title or dialog.title,
        class_name=candidate.class_name,
    )
