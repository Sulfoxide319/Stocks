#!/usr/bin/env python3
"""Sync parsed broker holdings into the assistant's local position stores."""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app_storage import Position, connect, list_positions, save_position, set_setting


DEFAULT_TARGET_UPPER_PCT = 0.10
DEFAULT_HARD_STOP_PCT = 0.04

CSV_POSITION_FIELDS = [
    "ticker",
    "name",
    "buy_date",
    "buy_time",
    "buy_price",
    "shares",
    "target_price",
    "hard_stop_price",
    "trailing_stop_pct",
    "highest_price",
    "management_state",
    "first_manage_hit_at",
    "profit_protected_at",
    "reduced_at",
    "last_signal_action",
    "last_signal_at",
    "status",
    "notes",
]


@dataclass(frozen=True)
class BrokerSyncSummary:
    imported: int
    skipped: int
    cash_available: float
    holdings_value: float
    total_assets: float
    message: str


@dataclass(frozen=True)
class HoldingManagementLines:
    target_price: float
    hard_stop_price: float
    highest_price: float
    trailing_stop_pct: float
    management_state: str
    first_manage_hit_at: str
    profit_protected_at: str
    note: str


@dataclass(frozen=True)
class ProfitProtectionResult:
    latest_price: float
    current_gain_pct: float
    first_manage_price: float
    trailing_stop_price: float
    protection_price: float
    protected_gain_pct: float
    distance_to_protection_pct: float
    distance_to_target_pct: float
    action: str
    summary: str


def normalize_ticker(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:6]


def as_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_manage_price(buy_price: float, target_price: float) -> float:
    if buy_price <= 0 or target_price <= buy_price:
        return 0.0
    target_pct = (target_price / buy_price - 1.0) * 100.0
    first_manage_pct = max(4.0, target_pct * 0.4)
    return buy_price * (1.0 + first_manage_pct / 100.0)


def _line_is_auto_default(value: float, default_value: float) -> bool:
    if value <= 0 or default_value <= 0:
        return value <= 0
    return abs(value - default_value) <= max(0.02, default_value * 0.01)


def _profit_ladder_step_pct(gain_pct: float) -> float:
    if gain_pct < 20.0:
        return 10.0
    if gain_pct < 60.0:
        return 15.0
    if gain_pct < 120.0:
        return 20.0
    return 25.0


def _next_cost_ladder_target_pct(gain_pct: float) -> tuple[float, float]:
    step_pct = _profit_ladder_step_pct(gain_pct)
    next_pct = (math.floor(max(0.0, gain_pct) / step_pct) + 1) * step_pct
    return max(DEFAULT_TARGET_UPPER_PCT * 100.0, next_pct), step_pct


def calculate_profit_protection_result(
    *,
    cost_price: float,
    latest_price: float,
    target_price: float,
    hard_stop_price: float,
    trailing_stop_pct: float,
    highest_price: float,
    management_state: str = "OPEN",
) -> ProfitProtectionResult:
    cost = max(0.0, as_float(cost_price))
    latest = max(0.0, as_float(latest_price))
    target = max(0.0, as_float(target_price))
    stop = max(0.0, as_float(hard_stop_price))
    trailing_pct = max(0.0, as_float(trailing_stop_pct, 3.0))
    highest = max(cost, latest, as_float(highest_price))
    first_manage = _first_manage_price(cost, target)
    first_manage_hit = first_manage > 0 and highest >= first_manage
    trailing_stop = highest * (1.0 - trailing_pct / 100.0) if first_manage_hit and trailing_pct > 0 else 0.0
    protection = max(stop, trailing_stop) if first_manage_hit else stop
    current_gain = (latest / cost - 1.0) * 100.0 if cost > 0 and latest > 0 else 0.0
    protected_gain = (protection / cost - 1.0) * 100.0 if cost > 0 and protection > 0 else 0.0
    distance_to_protection = (latest / protection - 1.0) * 100.0 if latest > 0 and protection > 0 else 0.0
    distance_to_target = (target / latest - 1.0) * 100.0 if latest > 0 and target > 0 else 0.0
    state = str(management_state or "OPEN").strip().upper()
    managed = state in {"FIRST_MANAGE_HIT", "PROFIT_PROTECTED", "REDUCED"} or first_manage_hit

    if latest <= 0:
        action = "NO_PRICE"
        summary = "缺少最近价格，暂不能判断保护利润"
    elif first_manage_hit and protection > 0 and latest <= protection:
        action = "PROTECT_EXIT"
        summary = (
            f"触发保护利润：最近{latest:.2f} <= 保护线{protection:.2f}；"
            f"已锁定约+{protected_gain:.1f}%"
        )
    elif managed and target > 0 and latest >= target:
        action = "ROLL_TARGET"
        summary = (
            f"达到成本阶梯目标{target:.2f}，不强制卖出；"
            f"按最近高点滚动上移保护线{protection:.2f}，继续留强势仓"
        )
    elif first_manage_hit:
        action = "PROTECT_HOLD"
        summary = (
            f"保护利润：最近{latest:.2f}，保护线{protection:.2f}，"
            f"距保护线{distance_to_protection:.1f}%，已锁定约+{protected_gain:.1f}%"
        )
    elif first_manage > 0:
        action = "WAIT_FIRST_MANAGE"
        summary = f"未到第一管理线{first_manage:.2f}；最近{latest:.2f}，先按硬止损管理"
    else:
        action = "NO_MANAGE_LINE"
        summary = "缺少第一管理线，需补成本/目标"

    return ProfitProtectionResult(
        latest_price=round(latest, 4),
        current_gain_pct=round(current_gain, 4),
        first_manage_price=round(first_manage, 4),
        trailing_stop_price=round(trailing_stop, 4),
        protection_price=round(protection, 4),
        protected_gain_pct=round(protected_gain, 4),
        distance_to_protection_pct=round(distance_to_protection, 4),
        distance_to_target_pct=round(distance_to_target, 4),
        action=action,
        summary=summary,
    )


def calculate_holding_management_lines(
    *,
    cost_price: float,
    latest_price: float,
    previous_target_price: float = 0.0,
    previous_hard_stop_price: float = 0.0,
    previous_highest_price: float = 0.0,
    trailing_stop_pct: float = 3.0,
    previous_management_state: str = "OPEN",
    first_manage_hit_at: str = "",
    profit_protected_at: str = "",
    source_notes: str = "",
    timestamp: str = "",
) -> HoldingManagementLines:
    """Build management lines for broker-imported positions.

    Broker imports can include long-held winners whose current price is far
    above the cost basis. In that case the stale 10% target is advanced to
    the next cost-anchored profit ladder instead of being anchored to price.
    """
    cost = max(0.0, as_float(cost_price))
    latest = max(0.0, as_float(latest_price))
    trailing_pct = max(0.0, as_float(trailing_stop_pct, 3.0))
    previous_target = max(0.0, as_float(previous_target_price))
    previous_stop = max(0.0, as_float(previous_hard_stop_price))
    highest = max(cost, latest, as_float(previous_highest_price))
    default_target = cost * (1.0 + DEFAULT_TARGET_UPPER_PCT)
    default_stop = cost * (1.0 - DEFAULT_HARD_STOP_PCT)
    sync_managed = "国盛睿持仓同步" in str(source_notes or "")
    target_is_auto = sync_managed or _line_is_auto_default(previous_target, default_target)
    stop_is_auto = sync_managed or _line_is_auto_default(previous_stop, default_stop)

    winner_anchor = highest if highest >= default_target and cost > 0 else 0.0
    target_price = previous_target if previous_target > 0 else default_target
    hard_stop_price = previous_stop if previous_stop > 0 else default_stop
    note = ""

    if winner_anchor > 0 and target_is_auto:
        gain_pct = (winner_anchor / cost - 1.0) * 100.0
        target_pct, step_pct = _next_cost_ladder_target_pct(gain_pct)
        target_price = max(default_target, cost * (1.0 + target_pct / 100.0))
        note = f"大幅盈利持仓已按成本阶梯重算目标、按近期高点上移保护线：下一档+{target_pct:.0f}%"
    if winner_anchor > 0 and stop_is_auto:
        gain_pct = (winner_anchor / cost - 1.0) * 100.0
        target_pct, step_pct = _next_cost_ladder_target_pct(gain_pct)
        protect_pct = max(0.0, target_pct - step_pct)
        cost_ladder_stop = cost * (1.0 + protect_pct / 100.0) if protect_pct > 0 else default_stop
        recent_trailing_stop = winner_anchor * (1.0 - trailing_pct / 100.0) if trailing_pct > 0 else 0.0
        protective_stop = max(cost_ladder_stop, recent_trailing_stop)
        hard_stop_price = max(default_stop, protective_stop)
        if previous_stop > hard_stop_price and previous_stop < winner_anchor:
            hard_stop_price = previous_stop
        note = note or f"大幅盈利持仓已按近期高点/成本阶梯上移保护线：保护+{protect_pct:.0f}%"

    first_manage_price = _first_manage_price(cost, target_price)
    first_manage_hit = first_manage_price > 0 and highest >= first_manage_price
    profit_protected = first_manage_hit and hard_stop_price > cost
    state = str(previous_management_state or "OPEN").strip().upper() or "OPEN"
    if state not in {"OPEN", "FIRST_MANAGE_HIT", "PROFIT_PROTECTED", "REDUCED", "EXITED"}:
        state = "OPEN"
    if state not in {"REDUCED", "EXITED"}:
        if profit_protected:
            state = "PROFIT_PROTECTED"
        elif first_manage_hit:
            state = "FIRST_MANAGE_HIT"
    event_time = timestamp or dt.datetime.now().isoformat(timespec="seconds")
    if first_manage_hit and not first_manage_hit_at:
        first_manage_hit_at = event_time
    if profit_protected and not profit_protected_at:
        profit_protected_at = event_time

    return HoldingManagementLines(
        target_price=round(target_price, 4),
        hard_stop_price=round(hard_stop_price, 4),
        highest_price=round(highest, 4),
        trailing_stop_pct=round(trailing_pct, 4),
        management_state=state,
        first_manage_hit_at=first_manage_hit_at,
        profit_protected_at=profit_protected_at,
        note=note,
    )


def _summary(result: Any, imported: int, skipped: int) -> BrokerSyncSummary:
    cash_available = as_float(getattr(result, "cash_available", 0.0))
    holdings_value = as_float(getattr(result, "holdings_value", 0.0))
    total_assets = as_float(getattr(result, "total_assets", 0.0), cash_available + holdings_value)
    if total_assets <= 0:
        total_assets = cash_available + holdings_value
    message = (
        f"已同步国盛睿持仓 {imported} 条，跳过 {skipped} 条；"
        f"可用现金 {cash_available:.2f}，持仓市值 {holdings_value:.2f}，总资产 {total_assets:.2f}"
    )
    return BrokerSyncSummary(imported, skipped, cash_available, holdings_value, total_assets, message)


def sync_holdings_to_sqlite(db_path: Path, result: Any) -> BrokerSyncSummary:
    imported = 0
    skipped = 0
    now_text = dt.datetime.now().isoformat(timespec="seconds")
    with connect(db_path) as conn:
        existing_by_ticker = {
            normalize_ticker(position.ticker): position
            for position in list_positions(conn, open_only=True)
        }
        for holding in getattr(result, "positions", ()) or ():
            ticker = normalize_ticker(getattr(holding, "ticker", ""))
            shares = as_float(getattr(holding, "shares", 0.0))
            cost_price = as_float(getattr(holding, "cost_price", 0.0))
            if not ticker or shares <= 0 or cost_price <= 0:
                skipped += 1
                continue
            existing = existing_by_ticker.get(ticker)
            latest_price = as_float(getattr(holding, "latest_price", 0.0))
            lines = calculate_holding_management_lines(
                cost_price=cost_price,
                latest_price=latest_price,
                previous_target_price=existing.target_price if existing else 0.0,
                previous_hard_stop_price=existing.hard_stop_price if existing else 0.0,
                previous_highest_price=existing.highest_price if existing else 0.0,
                trailing_stop_pct=existing.trailing_stop_pct if existing else 3.0,
                previous_management_state=existing.management_state if existing else "OPEN",
                first_manage_hit_at=existing.first_manage_hit_at if existing else "",
                profit_protected_at=existing.profit_protected_at if existing else "",
                source_notes=existing.notes if existing else "",
                timestamp=now_text,
            )
            note_suffix = f"；{lines.note}" if lines.note else ""
            position = Position(
                id=existing.id if existing else None,
                ticker=ticker,
                name=str(getattr(holding, "name", "") or ""),
                buy_date=existing.buy_date if existing else "",
                buy_time=existing.buy_time if existing else "",
                buy_price=cost_price,
                shares=shares,
                target_price=lines.target_price,
                hard_stop_price=lines.hard_stop_price,
                trailing_stop_pct=lines.trailing_stop_pct,
                highest_price=lines.highest_price,
                management_state=lines.management_state,
                first_manage_hit_at=lines.first_manage_hit_at,
                profit_protected_at=lines.profit_protected_at,
                reduced_at=existing.reduced_at if existing else "",
                last_signal_action=existing.last_signal_action if existing else "",
                last_signal_at=existing.last_signal_at if existing else "",
                status="open",
                notes=(
                    f"国盛睿持仓同步 {now_text}；"
                    f"市值 {as_float(getattr(holding, 'market_value', 0.0)):.2f}；"
                    f"可卖 {as_float(getattr(holding, 'sellable_shares', 0.0)):.0f}；"
                    f"来源 {getattr(result, 'export_path', '')}"
                    f"{note_suffix}"
                ),
            )
            save_position(conn, position)
            imported += 1
        set_setting(conn, "trade_cash_amount", f"{as_float(getattr(result, 'cash_available', 0.0)):.2f}")
        set_setting(conn, "trade_holdings_value", f"{as_float(getattr(result, 'holdings_value', 0.0)):.2f}")
        total_assets = as_float(getattr(result, "total_assets", 0.0))
        if total_assets <= 0:
            total_assets = as_float(getattr(result, "cash_available", 0.0)) + as_float(getattr(result, "holdings_value", 0.0))
        set_setting(conn, "trade_total_assets", f"{total_assets:.2f}")
    return _summary(result, imported, skipped)


def sync_holdings_to_csv(path: Path, result: Any) -> BrokerSyncSummary:
    existing: dict[str, dict[str, str]] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    ticker = normalize_ticker(row.get("ticker"))
                    if ticker:
                        existing[ticker] = {key: str(row.get(key, "") or "") for key in CSV_POSITION_FIELDS}
        except Exception:
            existing = {}
    now_text = dt.datetime.now().isoformat(timespec="seconds")
    imported = 0
    skipped = 0
    for holding in getattr(result, "positions", ()) or ():
        ticker = normalize_ticker(getattr(holding, "ticker", ""))
        shares = as_float(getattr(holding, "shares", 0.0))
        cost_price = as_float(getattr(holding, "cost_price", 0.0))
        if not ticker or shares <= 0 or cost_price <= 0:
            skipped += 1
            continue
        row = existing.get(ticker, {})
        lines = calculate_holding_management_lines(
            cost_price=cost_price,
            latest_price=as_float(getattr(holding, "latest_price", 0.0)),
            previous_target_price=as_float(row.get("target_price")),
            previous_hard_stop_price=as_float(row.get("hard_stop_price")),
            previous_highest_price=as_float(row.get("highest_price")),
            trailing_stop_pct=as_float(row.get("trailing_stop_pct"), 3.0),
            previous_management_state=row.get("management_state") or "OPEN",
            first_manage_hit_at=row.get("first_manage_hit_at") or "",
            profit_protected_at=row.get("profit_protected_at") or "",
            source_notes=row.get("notes") or "",
            timestamp=now_text,
        )
        note_suffix = f"；{lines.note}" if lines.note else ""
        existing[ticker] = {
            **{key: row.get(key, "") for key in CSV_POSITION_FIELDS},
            "ticker": ticker,
            "name": str(getattr(holding, "name", "") or ""),
            "buy_price": f"{cost_price:.4f}",
            "shares": f"{shares:.0f}",
            "target_price": f"{lines.target_price:.4f}",
            "hard_stop_price": f"{lines.hard_stop_price:.4f}",
            "trailing_stop_pct": f"{lines.trailing_stop_pct:.2f}",
            "highest_price": f"{lines.highest_price:.4f}",
            "management_state": lines.management_state,
            "first_manage_hit_at": lines.first_manage_hit_at,
            "profit_protected_at": lines.profit_protected_at,
            "status": "open",
            "notes": (
                f"国盛睿持仓同步 {now_text}；"
                f"市值 {as_float(getattr(holding, 'market_value', 0.0)):.2f}；"
                f"可卖 {as_float(getattr(holding, 'sellable_shares', 0.0)):.0f}"
                f"{note_suffix}"
            ),
        }
        imported += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_POSITION_FIELDS)
        writer.writeheader()
        for ticker in sorted(existing):
            writer.writerow({field: existing[ticker].get(field, "") for field in CSV_POSITION_FIELDS})
    summary = _summary(result, imported, skipped)
    snapshot = {
        "cash_available": round(summary.cash_available, 2),
        "holdings_value": round(summary.holdings_value, 2),
        "total_assets": round(summary.total_assets, 2),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "export_path": str(getattr(result, "export_path", "") or ""),
    }
    try:
        (path.parent / "broker_account_snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return summary
