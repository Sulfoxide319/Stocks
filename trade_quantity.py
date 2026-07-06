#!/usr/bin/env python3
"""Order quantity helpers for manual Guoshengrui trade dialogs."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Any


LOT_SIZE = 100


@dataclass(frozen=True)
class BuyQuantityPlan:
    planned_shares: int
    planned_value: float
    total_assets: float
    cash_amount: float
    holdings_value: float
    target_position_value: float
    remaining_buy_value: float
    existing_shares: float
    existing_value: float
    broker_max_buy_shares: int | None
    broker_cash_cap_value: float | None
    price: float
    suggested_capital_pct: float
    reason: str


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def floor_to_lot(shares: float, lot_size: int = LOT_SIZE) -> int:
    if shares <= 0 or lot_size <= 0:
        return 0
    return int(floor(shares / lot_size) * lot_size)


def calculate_buy_quantity(
    *,
    account_cash_amount: float,
    account_holdings_value: float,
    account_total_assets: float | None = None,
    price: float,
    suggested_capital_pct: float,
    existing_shares: float = 0.0,
    max_buy_shares: float | None = None,
    lot_size: int = LOT_SIZE,
) -> BuyQuantityPlan:
    clean_cash = max(0.0, as_float(account_cash_amount))
    clean_holdings_value = max(0.0, as_float(account_holdings_value))
    scanned_total_assets = max(0.0, as_float(account_total_assets)) if account_total_assets is not None else 0.0
    total_assets = scanned_total_assets if scanned_total_assets > 0 else clean_cash + clean_holdings_value
    clean_price = max(0.0, as_float(price))
    requested_pct = max(0.0, as_float(suggested_capital_pct))
    clean_pct = min(100.0, requested_pct)
    clean_existing = max(0.0, as_float(existing_shares))
    broker_max_buy_shares = floor_to_lot(as_float(max_buy_shares), lot_size) if max_buy_shares is not None else None
    broker_cash_cap_value = broker_max_buy_shares * clean_price if broker_max_buy_shares is not None and clean_price > 0 else None

    if total_assets <= 0:
        return BuyQuantityPlan(
            planned_shares=0,
            planned_value=0.0,
            total_assets=0.0,
            cash_amount=clean_cash,
            holdings_value=clean_holdings_value,
            target_position_value=0.0,
            remaining_buy_value=0.0,
            existing_shares=clean_existing,
            existing_value=0.0,
            broker_max_buy_shares=broker_max_buy_shares,
            broker_cash_cap_value=broker_cash_cap_value,
            price=clean_price,
            suggested_capital_pct=clean_pct,
            reason="未输入账户资产",
        )
    if clean_price <= 0:
        return BuyQuantityPlan(
            planned_shares=0,
            planned_value=0.0,
            total_assets=round(total_assets, 2),
            cash_amount=clean_cash,
            holdings_value=clean_holdings_value,
            target_position_value=0.0,
            remaining_buy_value=0.0,
            existing_shares=clean_existing,
            existing_value=0.0,
            broker_max_buy_shares=broker_max_buy_shares,
            broker_cash_cap_value=broker_cash_cap_value,
            price=clean_price,
            suggested_capital_pct=clean_pct,
            reason="缺少有效参考价",
        )
    if clean_pct <= 0:
        return BuyQuantityPlan(
            planned_shares=0,
            planned_value=0.0,
            total_assets=round(total_assets, 2),
            cash_amount=clean_cash,
            holdings_value=clean_holdings_value,
            target_position_value=0.0,
            remaining_buy_value=0.0,
            existing_shares=clean_existing,
            existing_value=clean_existing * clean_price,
            broker_max_buy_shares=broker_max_buy_shares,
            broker_cash_cap_value=broker_cash_cap_value,
            price=clean_price,
            suggested_capital_pct=clean_pct,
            reason="建议资金占比为 0",
        )

    target_value = total_assets * clean_pct / 100.0
    existing_value = clean_existing * clean_price
    remaining_value = max(0.0, target_value - existing_value)
    cash_limited_value = min(remaining_value, clean_cash)
    if broker_cash_cap_value is not None:
        cash_limited_value = min(cash_limited_value, broker_cash_cap_value)
    target_lot_shares = floor_to_lot(remaining_value / clean_price, lot_size)
    planned_shares = floor_to_lot(cash_limited_value / clean_price, lot_size)
    planned_value = planned_shares * clean_price

    if remaining_value <= 0:
        reason = "已有持仓已达到目标仓位"
    elif target_lot_shares <= 0:
        reason = "剩余额度不足一手"
    elif clean_cash <= 0:
        reason = "未输入可用现金"
    elif floor_to_lot(clean_cash / clean_price, lot_size) <= 0:
        reason = "可用现金不足一手"
    elif broker_max_buy_shares is not None and broker_max_buy_shares <= 0:
        reason = "券商可用现金不足一手"
    elif broker_max_buy_shares is not None and planned_shares < min(target_lot_shares, floor_to_lot(clean_cash / clean_price, lot_size)):
        reason = "受券商最大可买/现金约束"
    elif planned_shares < target_lot_shares:
        reason = "受可用现金约束"
    else:
        reason = "按总资产、建议资金占比和持仓取整"
    if requested_pct > 100.0:
        reason = f"{reason}；建议资金占比已从{requested_pct:.2f}%封顶到100%"

    return BuyQuantityPlan(
        planned_shares=planned_shares,
        planned_value=round(planned_value, 2),
        total_assets=round(total_assets, 2),
        cash_amount=round(clean_cash, 2),
        holdings_value=round(clean_holdings_value, 2),
        target_position_value=round(target_value, 2),
        remaining_buy_value=round(remaining_value, 2),
        existing_shares=clean_existing,
        existing_value=round(existing_value, 2),
        broker_max_buy_shares=broker_max_buy_shares,
        broker_cash_cap_value=round(broker_cash_cap_value, 2) if broker_cash_cap_value is not None else None,
        price=clean_price,
        suggested_capital_pct=clean_pct,
        reason=reason,
    )


def format_buy_quantity_plan(plan: BuyQuantityPlan) -> str:
    pieces = [
        f"总资产{plan.total_assets:.2f}",
        f"现金{plan.cash_amount:.2f}",
        f"持仓市值{plan.holdings_value:.2f}",
        f"目标仓位{plan.target_position_value:.2f}",
        f"已持{plan.existing_shares:.0f}股/{plan.existing_value:.2f}",
        f"需买{plan.remaining_buy_value:.2f}",
    ]
    if plan.broker_max_buy_shares is not None:
        pieces.append(f"券商最大可买{plan.broker_max_buy_shares}股")
    pieces.append(f"计划{plan.planned_shares}股")
    pieces.append(plan.reason)
    return "；".join(pieces)
