#!/usr/bin/env python3
"""Strict 10-minute execution ledger for the current short-term strategy."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from baostock_intraday import BaoStock5mClient, IntradayBar
from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from short_term_pattern_miner import official_event_score_adjustment, official_event_score_by_symbol
from short_term_strategy_backtest import (
    PlannedTrade,
    build_bar_maps,
    build_signal_rows,
    evaluate_regime,
    max_drawdown,
    passes_strategy,
)
from tech_event_backtest import PriceBar, fetch_yahoo_history
from tech_event_backtest import BaoStockDailyClient
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date
from tools.backtest_existing_strategy_ledger import (
    cached_history,
    default_end_date,
    dynamic_param_overrides,
    market_temperature,
    planned_passes_dynamic_filters,
    price_bars_to_dicts,
    selection_key,
    subtract_months,
    write_outputs,
)


@dataclass(frozen=True)
class OpenPosition:
    planned: PlannedTrade
    shares: int
    remaining_shares: int
    cost_basis: float
    remaining_cost_basis: float
    buy_fee: float
    first_manage_done: bool = False
    realized_partial_pnl: float = 0.0


def parse_clock(raw: str) -> dt.time:
    hour, minute = raw.split(":", 1)
    return dt.time(int(hour), int(minute))


def parse_optional_clock(raw: str) -> dt.time | None:
    text = str(raw or "").strip()
    if not text:
        return None
    return parse_clock(text)


def first_index_after(bars: list[PriceBar], date_value: dt.date) -> int | None:
    for index, bar in enumerate(bars):
        if bar.date > date_value:
            return index
    return None


def final_holding_date(bars: list[PriceBar], entry_date: dt.date, horizon: int, end_date: dt.date) -> dt.date | None:
    entry_index = next((index for index, bar in enumerate(bars) if bar.date >= entry_date), None)
    if entry_index is None:
        return None
    final_index = min(entry_index + max(1, horizon) - 1, len(bars) - 1)
    return min(bars[final_index].date, end_date)


def next_trading_date_after(bars: list[PriceBar], date_value: dt.date) -> dt.date | None:
    for bar in bars:
        if bar.date > date_value:
            return bar.date
    return None


def close_on_or_before(bars: list[PriceBar], date_value: dt.date) -> float | None:
    value: float | None = None
    for bar in bars:
        if bar.date <= date_value:
            value = bar.close
        else:
            break
    return value


def previous_close_before(bars: list[PriceBar], date_value: dt.date) -> float | None:
    value: float | None = None
    for bar in bars:
        if bar.date < date_value:
            value = bar.close
        else:
            break
    return value


def aggregate_10m(bars: Iterable[IntradayBar]) -> list[IntradayBar]:
    grouped: dict[tuple[dt.date, int], list[IntradayBar]] = {}
    for bar in sorted(bars, key=lambda item: item.moment):
        if not (dt.time(9, 30) <= bar.time <= dt.time(15, 0)):
            continue
        elapsed = (bar.time.hour * 60 + bar.time.minute) - (9 * 60 + 30)
        if elapsed <= 0:
            continue
        bucket = (elapsed - 1) // 10
        grouped.setdefault((bar.date, bucket), []).append(bar)
    result: list[IntradayBar] = []
    for (_date, _bucket), items in sorted(grouped.items()):
        if not items:
            continue
        first = items[0]
        last = items[-1]
        result.append(
            IntradayBar(
                date=first.date,
                time=last.time,
                code=first.code,
                open=first.open,
                high=max(item.high for item in items),
                low=min(item.low for item in items),
                close=last.close,
                volume=sum(item.volume for item in items),
                amount=sum(item.amount for item in items),
            )
        )
    return result


def intraday_vwap_by_moment(bars: list[IntradayBar]) -> dict[dt.datetime, float]:
    values: dict[dt.datetime, float] = {}
    current_date: dt.date | None = None
    amount = 0.0
    volume = 0.0
    for bar in sorted(bars, key=lambda item: item.moment):
        if current_date != bar.date:
            current_date = bar.date
            amount = 0.0
            volume = 0.0
        amount += bar.amount
        volume += bar.volume
        values[bar.moment] = amount / volume if volume > 0 else bar.close
    return values


def stock_limit_threshold(ticker: str, args: argparse.Namespace) -> float:
    code = ticker.strip()[:6]
    if code.startswith(("300", "301", "688")):
        return float(args.growth_limit_threshold)
    return float(args.mainboard_limit_threshold)


def round_to_tick(value: float, tick: float) -> float:
    if value <= 0 or tick <= 0:
        return 0.0
    return round(math.floor(value / tick + 0.5 + 1e-9) * tick, 4)


def round_buy_price(value: float, args: argparse.Namespace) -> float:
    tick = float(args.price_tick)
    if value <= 0 or tick <= 0:
        return 0.0
    return round(math.ceil((value - 1e-9) / tick) * tick, 4)


def round_sell_price(value: float, args: argparse.Namespace) -> float:
    tick = float(args.price_tick)
    if value <= 0 or tick <= 0:
        return 0.0
    return round(math.floor((value + 1e-9) / tick) * tick, 4)


def daily_limit_price(reference: float, threshold: float, direction: int, args: argparse.Namespace) -> float:
    return round_to_tick(reference * (1 + direction * threshold), float(args.price_tick))


def is_limit_up(reference: float, price: float, threshold: float, tick: float = 0.01) -> bool:
    return reference > 0 and price >= round_to_tick(reference * (1 + threshold), tick) - tick / 2


def is_limit_down(reference: float, price: float, threshold: float, tick: float = 0.01) -> bool:
    return reference > 0 and price <= round_to_tick(reference * (1 - threshold), tick) + tick / 2


def buy_execution_price(reference_price: float, args: argparse.Namespace) -> float:
    return round_buy_price(reference_price * (1 + args.slippage_bps / 10000), args)


def sell_execution_price(reference_price: float, args: argparse.Namespace) -> float:
    return round_sell_price(reference_price * (1 - args.slippage_bps / 10000), args)


def trade_fee(amount: float, side: str, args: argparse.Namespace) -> float:
    if amount <= 0:
        return 0.0
    commission = amount * args.commission_bps / 10000
    if args.min_commission > 0:
        commission = max(args.min_commission, commission)
    transfer_fee = amount * args.transfer_fee_bps / 10000
    stamp_tax = amount * args.stamp_tax_bps / 10000 if side.upper() == "SELL" else 0.0
    return commission + transfer_fee + stamp_tax


def max_affordable_lot_shares(cash_budget: float, price: float, args: argparse.Namespace) -> tuple[int, float, float]:
    lot_size = max(1, int(args.lot_size))
    if cash_budget <= 0 or price <= 0:
        return 0, 0.0, 0.0
    shares = int(cash_budget // (price * lot_size)) * lot_size
    while shares >= lot_size:
        amount = shares * price
        fee = trade_fee(amount, "BUY", args)
        total_cost = amount + fee
        if total_cost <= cash_budget + 1e-6:
            return shares, total_cost, fee
        shares -= lot_size
    return 0, 0.0, 0.0


def market_capital_factor(state: str, args: argparse.Namespace) -> float:
    if state == "hot":
        return max(0.0, min(1.0, float(args.hot_capital_factor)))
    if state in {"cold", "narrow_rally"}:
        return max(0.0, min(1.0, float(args.cold_capital_factor)))
    return max(0.0, min(1.0, float(args.normal_capital_factor)))


def first_manage_pct(planned: PlannedTrade, args: argparse.Namespace) -> float:
    return max(float(args.first_manage_min), planned.target_pct * float(args.first_manage_ratio))


def partial_sell_ratio_for_state(state: str, args: argparse.Namespace) -> float:
    if state == "cold":
        return max(0.0, min(1.0, float(args.partial_sell_ratio_cold)))
    if state == "narrow_rally":
        return max(0.0, min(1.0, float(args.partial_sell_ratio_narrow_rally)))
    return max(0.0, min(1.0, float(args.partial_sell_ratio_normal)))


def partial_sell_shares(remaining_shares: int, ratio: float, args: argparse.Namespace) -> int:
    lot_size = max(1, int(args.lot_size))
    if remaining_shares < lot_size * 2 or ratio <= 0:
        return 0
    lots = int((remaining_shares * ratio) // lot_size)
    if lots <= 0:
        lots = 1
    lots = min(lots, remaining_shares // lot_size - 1)
    return max(0, lots * lot_size)


def passes_market_quality(planned: PlannedTrade, state: str, args: argparse.Namespace) -> bool:
    features = planned.features
    if state == "cold":
        if float(features.get("traded_value_ratio") or 0.0) < float(args.cold_min_traded_value_ratio):
            return False
        if float(features.get("atr_pct") or 0.0) < float(args.cold_min_atr_pct):
            return False
        if float(features.get("momentum_10d_pct") or 0.0) < float(args.cold_min_momentum_10d_pct):
            return False
        if float(features.get("sector_momentum_5d_pct") or 0.0) < float(args.cold_min_sector_momentum_5d_pct):
            return False
    if state == "normal":
        if float(features.get("max_5d_range_pct") or 0.0) < float(args.normal_min_5d_range_pct):
            return False
        if float(features.get("atr_pct") or 0.0) < float(args.normal_min_atr_pct):
            return False
    return True


def apply_official_event_adjustments(rows: list[Any], event_scores: dict[str, int]) -> None:
    if not event_scores:
        return
    for row in rows:
        adjustment = official_event_score_adjustment(int(event_scores.get(row.ticker, 0)))
        if adjustment:
            row.score = round(max(0.0, min(100.0, float(row.score) + adjustment)), 2)


RETURN_BUCKETS = (
    (float("-inf"), -2.0, "<-2%"),
    (-2.0, 0.0, "-2~0%"),
    (0.0, 2.0, "0~2%"),
    (2.0, 4.0, "2~4%"),
    (4.0, 7.2, "4~7.2%"),
    (7.2, float("inf"), ">7.2%"),
)


def return_bucket(value: float) -> str:
    for low, high, label in RETURN_BUCKETS:
        if low <= value < high:
            return label
    return ">7.2%"


def numeric_bucket(value: object, buckets: tuple[tuple[float, float, str], ...], missing_label: str = "missing") -> str:
    numeric = safe_float(value)
    if value in {None, ""}:
        return missing_label
    for low, high, label in buckets:
        if low <= numeric < high:
            return label
    return buckets[-1][2] if buckets else missing_label


def time_bucket(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "missing"
    try:
        hour, minute = text.split(":", 1)
        minutes = int(hour) * 60 + int(minute[:2])
    except (ValueError, IndexError):
        return "invalid"
    if minutes <= 9 * 60 + 50:
        return "<=09:50"
    if minutes <= 10 * 60 + 10:
        return "09:51~10:10"
    if minutes <= 10 * 60 + 40:
        return "10:11~10:40"
    if minutes <= 11 * 60 + 20:
        return "10:41~11:20"
    return ">11:20"


def truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def safe_float(value: object) -> float:
    try:
        if value in {None, ""}:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def bar_date(bar: IntradayBar | None) -> str:
    return bar.date.isoformat() if bar else ""


def bar_time(bar: IntradayBar | None) -> str:
    return bar.time.isoformat(timespec="minutes") if bar else ""


def rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 4) if denominator else 0.0


def summarize_distribution_group(
    period: str,
    group_type: str,
    group_value: str,
    sells: list[dict[str, Any]],
    partial_sells: list[dict[str, Any]],
) -> dict[str, Any]:
    returns = [safe_float(row.get("return_pct")) for row in sells]
    trades = len(sells)
    wins = sum(1 for value in returns if value > 0)
    target_hits = sum(1 for row in sells if str(row.get("reason", "")).startswith("take_profit"))
    first_manage_hits = sum(1 for row in sells if truthy(row.get("first_manage_hit")))
    partial_pnl = sum(safe_float(row.get("realized_pnl")) for row in partial_sells)
    remaining_pnl = sum(safe_float(row.get("realized_pnl")) for row in sells)
    return {
        "period": period,
        "group_type": group_type,
        "group_value": group_value,
        "closed_trades": trades,
        "win_rate_pct": round(wins / trades * 100, 4) if trades else 0.0,
        "avg_return_pct": round(sum(returns) / trades, 4) if trades else 0.0,
        "target_upper_hits": target_hits,
        "target_upper_hit_rate_pct": round(target_hits / trades * 100, 4) if trades else 0.0,
        "first_manage_hits": first_manage_hits,
        "first_manage_hit_rate_pct": round(first_manage_hits / trades * 100, 4) if trades else 0.0,
        "partial_sells": len(partial_sells),
        "partial_realized_pnl": round(partial_pnl, 2),
        "remaining_realized_pnl": round(remaining_pnl, 2),
        "total_realized_pnl": round(partial_pnl + remaining_pnl, 2),
    }


def period_sort_key(period: str) -> int:
    try:
        return int(str(period).rstrip("M"))
    except ValueError:
        return 999


def build_distribution_rows(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sells = [row for row in ledger if row.get("action") == "SELL"]
    partial_sells = [row for row in ledger if row.get("action") == "PARTIAL_SELL"]
    periods = sorted({str(row.get("period", "")) for row in sells}, key=period_sort_key)
    rows: list[dict[str, Any]] = []
    for period in periods:
        period_sells = [row for row in sells if row.get("period") == period]
        period_partials = [row for row in partial_sells if row.get("period") == period]
        rows.append(summarize_distribution_group(period, "overall", "all", period_sells, period_partials))
        for field, group_type in (
            ("market_state", "market_state"),
            ("reason", "exit_reason"),
        ):
            for value in sorted({str(row.get(field, "") or "unknown") for row in period_sells}):
                group_sells = [row for row in period_sells if str(row.get(field, "") or "unknown") == value]
                group_partials = [row for row in period_partials if str(row.get(field, "") or "unknown") == value]
                rows.append(summarize_distribution_group(period, group_type, value, group_sells, group_partials))
        for month in sorted({str(row.get("date", ""))[:7] for row in period_sells}):
            group_sells = [row for row in period_sells if str(row.get("date", ""))[:7] == month]
            group_partials = [row for row in period_partials if str(row.get("date", ""))[:7] == month]
            rows.append(summarize_distribution_group(period, "month", month, group_sells, group_partials))
        for bucket in [item[2] for item in RETURN_BUCKETS]:
            group_sells = [row for row in period_sells if return_bucket(safe_float(row.get("return_pct"))) == bucket]
            rows.append(summarize_distribution_group(period, "return_bucket", bucket, group_sells, []))
    return rows


CONDITION_BUCKETS: tuple[tuple[str, str, tuple[tuple[float, float, str], ...]], ...] = (
    (
        "score",
        "score",
        (
            (float("-inf"), 87.0, "<87"),
            (87.0, 90.0, "87~90"),
            (90.0, 93.0, "90~93"),
            (93.0, 96.0, "93~96"),
            (96.0, float("inf"), ">=96"),
        ),
    ),
    (
        "entry_gap",
        "entry_gap_pct",
        (
            (float("-inf"), -1.0, "<-1%"),
            (-1.0, 0.0, "-1~0%"),
            (0.0, 1.0, "0~1%"),
            (1.0, 2.0, "1~2%"),
            (2.0, float("inf"), ">2%"),
        ),
    ),
    (
        "entry_vwap_distance",
        "entry_vwap_distance_pct",
        (
            (float("-inf"), 0.0, "<0%"),
            (0.0, 0.2, "0~0.2%"),
            (0.2, 0.5, "0.2~0.5%"),
            (0.5, 1.0, "0.5~1%"),
            (1.0, float("inf"), ">1%"),
        ),
    ),
    (
        "traded_value_ratio",
        "traded_value_ratio",
        (
            (float("-inf"), 1.2, "<1.2x"),
            (1.2, 1.5, "1.2~1.5x"),
            (1.5, 2.0, "1.5~2.0x"),
            (2.0, 3.0, "2.0~3.0x"),
            (3.0, float("inf"), ">=3.0x"),
        ),
    ),
    (
        "atr",
        "atr_pct",
        (
            (float("-inf"), 4.1, "<4.1%"),
            (4.1, 5.5, "4.1~5.5%"),
            (5.5, 7.0, "5.5~7%"),
            (7.0, float("inf"), ">=7%"),
        ),
    ),
    (
        "max_5d_range",
        "max_5d_range_pct",
        (
            (float("-inf"), 10.0, "<10%"),
            (10.0, 18.0, "10~18%"),
            (18.0, 25.0, "18~25%"),
            (25.0, float("inf"), ">=25%"),
        ),
    ),
    (
        "momentum_10d",
        "momentum_10d_pct",
        (
            (float("-inf"), 0.0, "<0%"),
            (0.0, 10.0, "0~10%"),
            (10.0, 20.0, "10~20%"),
            (20.0, 26.0, "20~26%"),
            (26.0, float("inf"), ">=26%"),
        ),
    ),
    (
        "close_position_20d",
        "close_position_20d_pct",
        (
            (float("-inf"), 50.0, "<50%"),
            (50.0, 70.0, "50~70%"),
            (70.0, 85.0, "70~85%"),
            (85.0, float("inf"), ">=85%"),
        ),
    ),
    (
        "sector_momentum_5d",
        "sector_momentum_5d_pct",
        (
            (float("-inf"), 0.0, "<0%"),
            (0.0, 3.0, "0~3%"),
            (3.0, 6.0, "3~6%"),
            (6.0, float("inf"), ">=6%"),
        ),
    ),
    (
        "sector_above_ma20",
        "sector_above_ma20_ratio",
        (
            (float("-inf"), 0.35, "<35%"),
            (0.35, 0.55, "35~55%"),
            (0.55, 0.70, "55~70%"),
            (0.70, float("inf"), ">=70%"),
        ),
    ),
)


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def realized_pnl_for_row(row: dict[str, Any]) -> float:
    total = safe_float(row.get("trade_total_realized_pnl"))
    if total:
        return total
    return safe_float(row.get("realized_pnl"))


def summarize_condition_group(
    period: str,
    condition: str,
    bucket: str,
    rows: list[dict[str, Any]],
    baseline: dict[str, float],
) -> dict[str, Any]:
    returns = [safe_float(row.get("return_pct")) for row in rows]
    pnls = [realized_pnl_for_row(row) for row in rows]
    trades = len(rows)
    wins = sum(1 for value in returns if value > 0)
    losses = trades - wins
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    first_manage_hits = sum(1 for row in rows if truthy(row.get("first_manage_hit")))
    target_hits = sum(1 for row in rows if str(row.get("reason", "")).startswith("take_profit"))
    hard_stops = sum(1 for row in rows if str(row.get("reason", "")).startswith("hard_stop"))
    vwap_fails = sum(1 for row in rows if str(row.get("reason", "")).startswith("vwap_fail"))
    trailing_stops = sum(1 for row in rows if str(row.get("reason", "")).startswith("trailing_stop"))
    avg_return = sum(returns) / trades if trades else 0.0
    win_rate = wins / trades * 100 if trades else 0.0
    sample_warning = "low_sample" if trades < 10 else ""
    return {
        "period": period,
        "condition": condition,
        "bucket": bucket,
        "closed_trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 4) if trades else 0.0,
        "avg_return_pct": round(avg_return, 4) if trades else 0.0,
        "median_return_pct": round(median(returns), 4) if trades else 0.0,
        "total_realized_pnl": round(sum(pnls), 2),
        "avg_realized_pnl": round(sum(pnls) / trades, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (999.0 if gross_profit > 0 else 0.0),
        "target_upper_hit_rate_pct": round(target_hits / trades * 100, 4) if trades else 0.0,
        "first_manage_hit_rate_pct": round(first_manage_hits / trades * 100, 4) if trades else 0.0,
        "hard_stop_rate_pct": round(hard_stops / trades * 100, 4) if trades else 0.0,
        "vwap_fail_rate_pct": round(vwap_fails / trades * 100, 4) if trades else 0.0,
        "trailing_stop_rate_pct": round(trailing_stops / trades * 100, 4) if trades else 0.0,
        "avg_return_lift_pct": round(avg_return - baseline.get("avg_return_pct", 0.0), 4),
        "win_rate_lift_pct": round(win_rate - baseline.get("win_rate_pct", 0.0), 4),
        "sample_warning": sample_warning,
    }


def condition_value(row: dict[str, Any], condition: str, field: str, buckets: tuple[tuple[float, float, str], ...]) -> str:
    if condition == "entry_time":
        return time_bucket(row.get(field))
    return numeric_bucket(row.get(field), buckets)


def build_condition_diagnostic_rows(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sells = [row for row in ledger if row.get("action") == "SELL"]
    periods = sorted({str(row.get("period", "")) for row in sells}, key=period_sort_key)
    rows: list[dict[str, Any]] = []
    categorical_fields = (
        ("market_state", "market_state"),
        ("setup_type", "setup_type"),
        ("sector_group", "sector_group"),
        ("exit_reason", "reason"),
        ("entry_time", "entry_time"),
    )
    for period in periods:
        period_sells = [row for row in sells if str(row.get("period", "")) == period]
        if not period_sells:
            continue
        returns = [safe_float(row.get("return_pct")) for row in period_sells]
        wins = sum(1 for value in returns if value > 0)
        baseline = {
            "avg_return_pct": sum(returns) / len(returns),
            "win_rate_pct": wins / len(returns) * 100,
        }
        for condition, field in categorical_fields:
            if condition == "entry_time":
                values = sorted({time_bucket(row.get(field)) for row in period_sells})
                for value in values:
                    group = [row for row in period_sells if time_bucket(row.get(field)) == value]
                    rows.append(summarize_condition_group(period, condition, value, group, baseline))
                continue
            values = sorted({str(row.get(field, "") or "unknown") for row in period_sells})
            for value in values:
                group = [row for row in period_sells if str(row.get(field, "") or "unknown") == value]
                rows.append(summarize_condition_group(period, condition, value, group, baseline))
        for condition, field, buckets in CONDITION_BUCKETS:
            values = [label for _low, _high, label in buckets]
            if any(row.get(field) in {None, ""} for row in period_sells):
                values.append("missing")
            for value in values:
                group = [row for row in period_sells if condition_value(row, condition, field, buckets) == value]
                if not group:
                    continue
                rows.append(summarize_condition_group(period, condition, value, group, baseline))
    return rows


def write_condition_diagnostic_outputs(out_dir: Path, prefix: str, ledger: list[dict[str, Any]]) -> dict[str, Path]:
    rows = build_condition_diagnostic_rows(ledger)
    csv_path = out_dir / f"{prefix}_condition_diagnostics.csv"
    md_path = out_dir / f"{prefix}_condition_diagnostics.md"
    fieldnames = list(rows[0].keys()) if rows else [
        "period",
        "condition",
        "bucket",
        "closed_trades",
        "wins",
        "losses",
        "win_rate_pct",
        "avg_return_pct",
        "median_return_pct",
        "total_realized_pnl",
        "avg_realized_pnl",
        "profit_factor",
        "target_upper_hit_rate_pct",
        "first_manage_hit_rate_pct",
        "hard_stop_rate_pct",
        "vwap_fail_rate_pct",
        "trailing_stop_rate_pct",
        "avg_return_lift_pct",
        "win_rate_lift_pct",
        "sample_warning",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        f"# Strict 10m Condition Diagnostics - {prefix}",
        "",
        "Rows compare each condition bucket against the same-period overall average. Small buckets are marked `low_sample`.",
        "",
        "## Negative Buckets",
        "",
        "| Period | Condition | Bucket | Trades | Avg Return% | Lift% | Win% | First Manage Hit% | Hard Stop% | VWAP Fail% | Warning |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    negative_rows = sorted(
        [row for row in rows if row["closed_trades"] >= 5 and row["avg_return_lift_pct"] < 0],
        key=lambda item: (period_sort_key(item["period"]), item["avg_return_lift_pct"]),
    )
    for row in negative_rows[:40]:
        lines.append(
            f"| {row['period']} | {row['condition']} | {row['bucket']} | {row['closed_trades']} | {row['avg_return_pct']:.2f} | {row['avg_return_lift_pct']:.2f} | {row['win_rate_pct']:.2f} | {row['first_manage_hit_rate_pct']:.2f} | {row['hard_stop_rate_pct']:.2f} | {row['vwap_fail_rate_pct']:.2f} | {row['sample_warning']} |"
        )
    lines.extend(
        [
            "",
            "## Positive Buckets",
            "",
            "| Period | Condition | Bucket | Trades | Avg Return% | Lift% | Win% | Target Hit% | First Manage Hit% | Profit Factor | Warning |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    positive_rows = sorted(
        [row for row in rows if row["closed_trades"] >= 5 and row["avg_return_lift_pct"] > 0],
        key=lambda item: (period_sort_key(item["period"]), -item["avg_return_lift_pct"]),
    )
    for row in positive_rows[:40]:
        lines.append(
            f"| {row['period']} | {row['condition']} | {row['bucket']} | {row['closed_trades']} | {row['avg_return_pct']:.2f} | {row['avg_return_lift_pct']:.2f} | {row['win_rate_pct']:.2f} | {row['target_upper_hit_rate_pct']:.2f} | {row['first_manage_hit_rate_pct']:.2f} | {row['profit_factor']:.2f} | {row['sample_warning']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"condition_diagnostics_csv": csv_path, "condition_diagnostics_md": md_path}


def write_distribution_outputs(out_dir: Path, prefix: str, ledger: list[dict[str, Any]]) -> dict[str, Path]:
    rows = build_distribution_rows(ledger)
    csv_path = out_dir / f"{prefix}_distribution.csv"
    md_path = out_dir / f"{prefix}_distribution.md"
    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    lines = [
        f"# Strict 10m Distribution - {prefix}",
        "",
        "| Period | Group | Value | Trades | Win% | Avg Return% | Target Hit% | First Manage Hit% | Partial PnL | Remaining PnL |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        if row["group_type"] not in {"overall", "market_state"}:
            continue
        lines.append(
            f"| {row['period']} | {row['group_type']} | {row['group_value']} | {row['closed_trades']} | {row['win_rate_pct']:.2f} | {row['avg_return_pct']:.2f} | {row['target_upper_hit_rate_pct']:.2f} | {row['first_manage_hit_rate_pct']:.2f} | {row['partial_realized_pnl']:.2f} | {row['remaining_realized_pnl']:.2f} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"distribution_csv": csv_path, "distribution_md": md_path}


def summarize_sell_path_group(period: str, group_type: str, group_value: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [safe_float(row.get("return_pct")) for row in rows]
    trades = len(rows)
    wins = sum(1 for value in returns if value > 0)
    target_touch = sum(1 for row in rows if truthy(row.get("target_upper_touch")))
    target_sellable = sum(1 for row in rows if truthy(row.get("target_upper_sellable_hit")))
    first_manage_touch = sum(1 for row in rows if truthy(row.get("first_manage_touch")))
    first_manage_sellable = sum(1 for row in rows if truthy(row.get("first_manage_hit")))
    trailing_active = sum(1 for row in rows if truthy(row.get("trailing_activated")))
    trailing_exit = sum(1 for row in rows if str(row.get("reason", "")).startswith("trailing_stop"))
    hard_stop = sum(1 for row in rows if str(row.get("reason", "")).startswith("hard_stop"))
    vwap_fail = sum(1 for row in rows if str(row.get("reason", "")).startswith("vwap_fail"))
    time_exit = sum(1 for row in rows if str(row.get("reason", "")).startswith("time_exit"))
    max_runups = [safe_float(row.get("max_runup_pct")) for row in rows]
    sellable_runups = [safe_float(row.get("sellable_max_runup_pct")) for row in rows]
    target_gaps = [safe_float(row.get("target_upper_gap_at_exit_pct")) for row in rows]
    return {
        "period": period,
        "group_type": group_type,
        "group_value": group_value,
        "closed_trades": trades,
        "win_rate_pct": rate(wins, trades),
        "avg_return_pct": round(sum(returns) / trades, 4) if trades else 0.0,
        "target_upper_touch_rate_pct": rate(target_touch, trades),
        "target_upper_sellable_hit_rate_pct": rate(target_sellable, trades),
        "first_manage_touch_rate_pct": rate(first_manage_touch, trades),
        "first_manage_sellable_hit_rate_pct": rate(first_manage_sellable, trades),
        "trailing_activation_rate_pct": rate(trailing_active, trades),
        "trailing_exit_rate_pct": rate(trailing_exit, trades),
        "hard_stop_rate_pct": rate(hard_stop, trades),
        "vwap_fail_rate_pct": rate(vwap_fail, trades),
        "time_exit_rate_pct": rate(time_exit, trades),
        "avg_max_runup_pct": round(sum(max_runups) / trades, 4) if trades else 0.0,
        "avg_sellable_max_runup_pct": round(sum(sellable_runups) / trades, 4) if trades else 0.0,
        "avg_target_upper_gap_at_exit_pct": round(sum(target_gaps) / trades, 4) if trades else 0.0,
    }


def build_sell_path_summary_rows(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sells = [row for row in ledger if row.get("action") == "SELL"]
    periods = sorted({str(row.get("period", "")) for row in sells}, key=period_sort_key)
    rows: list[dict[str, Any]] = []
    for period in periods:
        period_sells = [row for row in sells if row.get("period") == period]
        rows.append(summarize_sell_path_group(period, "overall", "all", period_sells))
        for field, group_type in (("market_state", "market_state"), ("reason", "exit_reason"), ("setup_type", "setup_type")):
            values = sorted({str(row.get(field, "") or "unknown") for row in period_sells})
            for value in values:
                group = [row for row in period_sells if str(row.get(field, "") or "unknown") == value]
                rows.append(summarize_sell_path_group(period, group_type, value, group))
    return rows


def build_sell_path_detail_rows(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = [
        "period",
        "ticker",
        "name",
        "market_state",
        "setup_type",
        "score",
        "date",
        "entry_time",
        "exit_time",
        "reason",
        "return_pct",
        "target_upper_price",
        "target_upper_touch",
        "target_upper_touch_date",
        "target_upper_touch_time",
        "target_upper_sellable_hit",
        "target_upper_gap_at_exit_pct",
        "first_manage_price",
        "first_manage_touch",
        "first_manage_hit",
        "trailing_activated",
        "trailing_stop_price",
        "hard_stop_price",
        "vwap_fail_time",
        "max_below_vwap_count",
        "min_vwap_distance_pct",
        "max_runup_pct",
        "sellable_max_runup_pct",
        "max_drawdown_pct",
        "sellable_max_drawdown_pct",
        "trade_total_realized_pnl",
        "exit_signal_path",
    ]
    return [{field: row.get(field, "") for field in fields} for row in ledger if row.get("action") == "SELL"]


def write_sell_path_outputs(out_dir: Path, prefix: str, ledger: list[dict[str, Any]]) -> dict[str, Path]:
    summary_rows = build_sell_path_summary_rows(ledger)
    detail_rows = build_sell_path_detail_rows(ledger)
    summary_csv = out_dir / f"{prefix}_sell_path_summary.csv"
    detail_csv = out_dir / f"{prefix}_sell_path_detail.csv"
    md_path = out_dir / f"{prefix}_sell_path.md"
    for path, rows in ((summary_csv, summary_rows), (detail_csv, detail_rows)):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
            if rows:
                writer.writeheader()
                writer.writerows(rows)
    lines = [
        f"# Strict 10m Sell Path Audit - {prefix}",
        "",
        "Target upper is split into `touch` (price reached the upper level at any time after entry) and `sellable hit` (reached on a T+1 sellable bar and therefore could trigger a take-profit exit).",
        "",
        "## Overall",
        "",
        "| Period | Trades | Win% | Avg Return% | Target Touch% | Target Sellable% | First Manage% | Trail Active% | Hard Stop% | VWAP Fail% | Avg Runup% | Avg Target Gap At Exit% |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary_rows:
        if row["group_type"] != "overall":
            continue
        lines.append(
            f"| {row['period']} | {row['closed_trades']} | {row['win_rate_pct']:.2f} | {row['avg_return_pct']:.2f} | {row['target_upper_touch_rate_pct']:.2f} | {row['target_upper_sellable_hit_rate_pct']:.2f} | {row['first_manage_sellable_hit_rate_pct']:.2f} | {row['trailing_activation_rate_pct']:.2f} | {row['hard_stop_rate_pct']:.2f} | {row['vwap_fail_rate_pct']:.2f} | {row['avg_max_runup_pct']:.2f} | {row['avg_target_upper_gap_at_exit_pct']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Market State",
            "",
            "| Period | State | Trades | Avg Return% | Target Touch% | Target Sellable% | First Manage% | Trail Active% | Hard Stop% | VWAP Fail% |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summary_rows:
        if row["group_type"] != "market_state":
            continue
        lines.append(
            f"| {row['period']} | {row['group_value']} | {row['closed_trades']} | {row['avg_return_pct']:.2f} | {row['target_upper_touch_rate_pct']:.2f} | {row['target_upper_sellable_hit_rate_pct']:.2f} | {row['first_manage_sellable_hit_rate_pct']:.2f} | {row['trailing_activation_rate_pct']:.2f} | {row['hard_stop_rate_pct']:.2f} | {row['vwap_fail_rate_pct']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Exit Reason",
            "",
            "| Period | Exit | Trades | Avg Return% | Target Touch% | First Manage% | Avg Runup% |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summary_rows:
        if row["group_type"] != "exit_reason":
            continue
        lines.append(
            f"| {row['period']} | {row['group_value']} | {row['closed_trades']} | {row['avg_return_pct']:.2f} | {row['target_upper_touch_rate_pct']:.2f} | {row['first_manage_sellable_hit_rate_pct']:.2f} | {row['avg_max_runup_pct']:.2f} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"sell_path_summary_csv": summary_csv, "sell_path_detail_csv": detail_csv, "sell_path_md": md_path}


def write_hit_rate_calibration_outputs(out_dir: Path, prefix: str, ledger: list[dict[str, Any]]) -> dict[str, Path]:
    summary_rows = build_sell_path_summary_rows(ledger)
    csv_path = out_dir / f"{prefix}_hit_rate_calibration.csv"
    json_path = out_dir / f"{prefix}_hit_rate_calibration.json"
    latest_json_path = out_dir / "latest_hit_rate_calibration.json"
    md_path = out_dir / f"{prefix}_hit_rate_calibration.md"
    calibration_rows = [
        row
        for row in summary_rows
        if row.get("group_type") in {"overall", "market_state"}
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(calibration_rows[0].keys()) if calibration_rows else [])
        if calibration_rows:
            writer.writeheader()
            writer.writerows(calibration_rows)
    payload: dict[str, Any] = {
        "source_prefix": prefix,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "default_period": "12M",
        "periods": {},
    }
    for row in calibration_rows:
        period = str(row["period"])
        period_data = payload["periods"].setdefault(period, {"overall": None, "market_state": {}})
        item = {
            "sample_size": int(row["closed_trades"]),
            "target_upper_touch_rate_pct": row["target_upper_touch_rate_pct"],
            "target_upper_sellable_hit_rate_pct": row["target_upper_sellable_hit_rate_pct"],
            "first_manage_touch_rate_pct": row["first_manage_touch_rate_pct"],
            "first_manage_sellable_hit_rate_pct": row["first_manage_sellable_hit_rate_pct"],
            "win_rate_pct": row["win_rate_pct"],
            "avg_return_pct": row["avg_return_pct"],
        }
        if row["group_type"] == "overall":
            period_data["overall"] = item
        elif row["group_type"] == "market_state":
            period_data["market_state"][str(row["group_value"])] = item
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(text + "\n", encoding="utf-8")
    latest_json_path.write_text(text + "\n", encoding="utf-8")
    lines = [
        f"# Hit Rate Calibration - {prefix}",
        "",
        "The live monitor should use the 12M market-state row when available, then fall back to the 12M overall row.",
        "",
        "| Period | Group | Value | Samples | Target Touch% | Target Sellable% | First Manage% | Win% | Avg Return% |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in calibration_rows:
        lines.append(
            f"| {row['period']} | {row['group_type']} | {row['group_value']} | {row['closed_trades']} | {row['target_upper_touch_rate_pct']:.2f} | {row['target_upper_sellable_hit_rate_pct']:.2f} | {row['first_manage_sellable_hit_rate_pct']:.2f} | {row['win_rate_pct']:.2f} | {row['avg_return_pct']:.2f} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "hit_rate_calibration_csv": csv_path,
        "hit_rate_calibration_json": json_path,
        "latest_hit_rate_calibration_json": latest_json_path,
        "hit_rate_calibration_md": md_path,
    }


def write_daily_cache(cache_dir: Path, ticker: str, fetch_start: dt.date, fetch_end: dt.date, bars: list[PriceBar]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{ticker}_{fetch_start:%Y%m%d}_{fetch_end:%Y%m%d}.json"
    cache_path.write_text(json.dumps(price_bars_to_dicts(bars), ensure_ascii=False), encoding="utf-8")


def plan_trade_base(
    row: Any,
    bars: list[PriceBar],
    end_date: dt.date,
    args: argparse.Namespace,
) -> PlannedTrade | None:
    signal_date = parse_date(row.date)
    if not signal_date:
        return None
    entry_index = first_index_after(bars, signal_date)
    if entry_index is None:
        return None
    entry_bar = bars[entry_index]
    if entry_bar.date > end_date:
        return None
    target = args.take_profit
    stop = args.hard_stop
    trail = args.trailing_stop
    if args.dynamic_exit:
        raw_target = row.atr_pct / 100 * args.target_atr_mult + row.max_5d_range_pct / 100 * args.target_range_mult
        target = min(args.target_max, max(args.target_min, raw_target))
        stop = min(args.stop_max, max(args.stop_min, row.atr_pct / 100 * args.stop_atr_mult))
        trail = min(args.trail_max, max(args.trail_min, row.atr_pct / 100 * args.trail_atr_mult))
    return PlannedTrade(
        ticker=row.ticker,
        name=row.name,
        signal_date=signal_date,
        entry_date=entry_bar.date,
        exit_date=entry_bar.date,
        entry_price=0.0,
        exit_price=0.0,
        return_pct=0.0,
        exit_reason="planned",
        target_pct=target,
        hard_stop_pct=stop,
        trailing_stop_pct=trail,
        score=row.score,
        setup_type=row.setup_type,
        features={
            "traded_value_ratio": row.traded_value_ratio,
            "atr_pct": row.atr_pct,
            "max_5d_range_pct": row.max_5d_range_pct,
            "change_1d_pct": row.change_1d_pct,
            "momentum_3d_pct": row.momentum_3d_pct,
            "momentum_10d_pct": row.momentum_10d_pct,
            "value_ratio_3d": row.value_ratio_3d,
            "distance_to_ma5_pct": row.distance_to_ma5_pct,
            "distance_to_20d_high_pct": row.distance_to_20d_high_pct,
            "close_position_20d_pct": row.close_position_20d_pct,
            "above_ma5": row.above_ma5,
            "above_ma20": row.above_ma20,
            "sector_group": row.sector_group,
            "sector_momentum_5d_pct": row.sector_momentum_5d_pct,
            "sector_above_ma20_ratio": row.sector_above_ma20_ratio,
        },
    )


def refine_trade_strict_10m(
    planned: PlannedTrade,
    daily_bars: list[PriceBar],
    intraday_bars_5m: list[IntradayBar],
    end_date: dt.date,
    args: argparse.Namespace,
) -> PlannedTrade | None:
    base_final_date = final_holding_date(daily_bars, planned.entry_date, args.horizon, end_date)
    if base_final_date is None:
        return None
    signal_close = close_on_or_before(daily_bars, planned.signal_date)
    if not signal_close:
        return None
    bars = [
        bar
        for bar in aggregate_10m(intraday_bars_5m)
        if planned.entry_date <= bar.date <= base_final_date and dt.time(9, 30) <= bar.time <= dt.time(15, 0)
    ]
    if not bars:
        return None
    entry_day_bars = [bar for bar in bars if bar.date == planned.entry_date]
    if not entry_day_bars:
        return None
    first_bar = entry_day_bars[0]
    gap_pct = first_bar.open / signal_close - 1 if signal_close else 0.0
    limit_threshold = stock_limit_threshold(planned.ticker, args)
    if gap_pct > args.max_gap_up or gap_pct < -args.max_gap_down:
        return None
    if args.reject_limit_open and (
        is_limit_up(signal_close, first_bar.open, limit_threshold, args.price_tick)
        or is_limit_down(signal_close, first_bar.open, limit_threshold, args.price_tick)
    ):
        return None
    if gap_pct > args.gap_volume_threshold and float(planned.features.get("traded_value_ratio") or 0.0) < args.gap_volume_min_ratio:
        return None

    vwap_by_moment = intraday_vwap_by_moment(bars)
    entry_start = parse_clock(args.entry_start_time)
    entry_end = parse_clock(args.entry_end_time)
    entry_bar: IntradayBar | None = None
    entry_vwap = 0.0
    entry_price = 0.0
    for bar in entry_day_bars:
        if bar.time < entry_start or bar.time > entry_end:
            continue
        current_vwap = vwap_by_moment.get(bar.moment, bar.close)
        trigger = max(signal_close * (1 + args.confirm_buffer), current_vwap * (1 + args.vwap_buffer))
        if bar.close < trigger:
            continue
        if bar.close / signal_close - 1 > args.max_entry_extension:
            continue
        candidate_entry_price = buy_execution_price(bar.close, args)
        if args.reject_limit_entry and is_limit_up(signal_close, candidate_entry_price, limit_threshold, args.price_tick):
            continue
        entry_bar = bar
        entry_vwap = current_vwap
        entry_price = candidate_entry_price
        break
    if entry_bar is None:
        return None

    first_sell_date = next_trading_date_after(daily_bars, planned.entry_date)
    if first_sell_date is None or first_sell_date > base_final_date:
        return None
    exit_bar = next((bar for bar in reversed(bars) if bar.date <= base_final_date), bars[-1])
    exit_price = sell_execution_price(exit_bar.close, args)
    exit_reason = "time_exit_10m"
    best_high = entry_price
    below_vwap_count = 0
    max_below_vwap_count = 0
    limit_down_blocked_exits = 0
    first_manage_bar: IntradayBar | None = None
    first_manage_touch_bar: IntradayBar | None = None
    first_manage_price = 0.0
    first_manage_target_pct = first_manage_pct(planned, args)
    first_manage_raw_price = entry_price * (1 + first_manage_target_pct)
    target_upper_raw_price = entry_price * (1 + planned.target_pct)
    target_upper_price = sell_execution_price(target_upper_raw_price, args)
    hard_stop_price = sell_execution_price(entry_price * (1 - planned.hard_stop_pct), args)
    target_upper_touch_bar: IntradayBar | None = None
    target_upper_sellable_bar: IntradayBar | None = None
    trailing_activation_bar: IntradayBar | None = None
    trailing_trigger_bar: IntradayBar | None = None
    trailing_activation_pct = max(0.04, planned.target_pct * 0.4)
    trailing_stop_price = 0.0
    trailing_reference_high_at_exit = 0.0
    hard_stop_bar: IntradayBar | None = None
    vwap_fail_bar: IntradayBar | None = None
    vwap_fail_vwap = 0.0
    vwap_fail_close = 0.0
    effective_vwap_fail_bars = int(args.vwap_fail_bars)
    max_high_after_entry = entry_price
    min_low_after_entry = entry_price
    max_sellable_high = entry_price
    min_sellable_low = entry_price
    min_vwap_distance_pct = 0.0
    for bar in bars:
        if bar.moment <= entry_bar.moment:
            continue
        max_high_after_entry = max(max_high_after_entry, bar.high)
        min_low_after_entry = min(min_low_after_entry, bar.low)
        if first_manage_touch_bar is None and bar.high >= first_manage_raw_price:
            first_manage_touch_bar = bar
        if target_upper_touch_bar is None and bar.high >= target_upper_raw_price:
            target_upper_touch_bar = bar
        if bar.date < first_sell_date:
            if bar.high > best_high:
                best_high = bar.high
            continue
        max_sellable_high = max(max_sellable_high, bar.high)
        min_sellable_low = min(min_sellable_low, bar.low)
        current_vwap = vwap_by_moment.get(bar.moment, bar.close)
        if current_vwap > 0:
            distance = (bar.close / current_vwap - 1) * 100
            min_vwap_distance_pct = min(min_vwap_distance_pct, distance)
        previous_close = previous_close_before(daily_bars, bar.date) or entry_price
        limit_down_blocked = args.reject_limit_exit and is_limit_down(previous_close, bar.close, limit_threshold, args.price_tick)
        if args.trailing_reference_policy == "same_bar_high" and bar.high > best_high:
            best_high = bar.high
        trailing_reference_high = best_high
        if bar.low <= entry_price * (1 - planned.hard_stop_pct):
            if limit_down_blocked:
                limit_down_blocked_exits += 1
                if args.trailing_reference_policy == "previous_high" and bar.high > best_high:
                    best_high = bar.high
                continue
            exit_bar = bar
            raw_stop_price = max(entry_price * (1 - planned.hard_stop_pct), daily_limit_price(previous_close, limit_threshold, -1, args))
            exit_price = sell_execution_price(raw_stop_price, args)
            exit_reason = "hard_stop_10m"
            hard_stop_bar = bar
            break
        if first_manage_bar is None and bar.high >= entry_price * (1 + first_manage_target_pct):
            first_manage_bar = bar
            first_manage_price = sell_execution_price(entry_price * (1 + first_manage_target_pct), args)
        if bar.high >= entry_price * (1 + planned.target_pct):
            exit_bar = bar
            exit_price = sell_execution_price(entry_price * (1 + planned.target_pct), args)
            exit_reason = "take_profit_10m"
            target_upper_sellable_bar = bar
            break
        if trailing_reference_high >= entry_price * (1 + trailing_activation_pct):
            if trailing_activation_bar is None:
                trailing_activation_bar = bar
            trailing_price = trailing_reference_high * (1 - planned.trailing_stop_pct)
            trailing_stop_price = sell_execution_price(max(trailing_price, daily_limit_price(previous_close, limit_threshold, -1, args)), args)
            if bar.low <= trailing_price:
                if limit_down_blocked:
                    limit_down_blocked_exits += 1
                    if args.trailing_reference_policy == "previous_high" and bar.high > best_high:
                        best_high = bar.high
                    continue
                exit_bar = bar
                exit_price = trailing_stop_price
                exit_reason = "trailing_stop_10m"
                trailing_trigger_bar = bar
                trailing_reference_high_at_exit = trailing_reference_high
                break
        if bar.close < current_vwap * (1 - args.vwap_fail_buffer) and bar.close < entry_price:
            below_vwap_count += 1
            max_below_vwap_count = max(max_below_vwap_count, below_vwap_count)
        else:
            below_vwap_count = 0
        effective_vwap_fail_bars = int(args.vwap_fail_bars)
        if first_manage_bar is not None and int(args.vwap_fail_after_first_manage_bars) > 0:
            effective_vwap_fail_bars = int(args.vwap_fail_after_first_manage_bars)
        if effective_vwap_fail_bars > 0 and below_vwap_count >= effective_vwap_fail_bars:
            if limit_down_blocked:
                limit_down_blocked_exits += 1
                if args.trailing_reference_policy == "previous_high" and bar.high > best_high:
                    best_high = bar.high
                continue
            exit_bar = bar
            exit_price = sell_execution_price(max(bar.close, daily_limit_price(previous_close, limit_threshold, -1, args)), args)
            exit_reason = "vwap_fail_10m"
            vwap_fail_bar = bar
            vwap_fail_vwap = current_vwap
            vwap_fail_close = bar.close
            break
        if args.trailing_reference_policy == "previous_high" and bar.high > best_high:
            best_high = bar.high

    features = dict(planned.features)
    max_runup_pct = (max_high_after_entry / entry_price - 1) * 100 if entry_price else 0.0
    max_drawdown_pct = (min_low_after_entry / entry_price - 1) * 100 if entry_price else 0.0
    sellable_max_runup_pct = (max_sellable_high / entry_price - 1) * 100 if entry_price else 0.0
    sellable_max_drawdown_pct = (min_sellable_low / entry_price - 1) * 100 if entry_price else 0.0
    target_upper_gap_at_exit_pct = (target_upper_price / exit_price - 1) * 100 if target_upper_price and exit_price else 0.0
    exit_path_flags = [
        f"first_manage_any={bool(first_manage_touch_bar)}",
        f"first_manage_sellable={bool(first_manage_bar)}",
        f"target_any={bool(target_upper_touch_bar)}",
        f"target_sellable={bool(target_upper_sellable_bar)}",
        f"trail_active={bool(trailing_activation_bar)}",
        f"exit={exit_reason}",
    ]
    features.update(
        {
            "entry_time": entry_bar.time.isoformat(timespec="minutes"),
            "exit_time": exit_bar.time.isoformat(timespec="minutes"),
            "entry_vwap": round(entry_vwap, 4),
            "entry_gap_pct": round(gap_pct * 100, 4),
            "target_upper_price": round(target_upper_price, 4),
            "target_upper_touch": bool(target_upper_touch_bar),
            "target_upper_touch_date": bar_date(target_upper_touch_bar),
            "target_upper_touch_time": bar_time(target_upper_touch_bar),
            "target_upper_sellable_hit": bool(target_upper_sellable_bar),
            "target_upper_sellable_date": bar_date(target_upper_sellable_bar),
            "target_upper_sellable_time": bar_time(target_upper_sellable_bar),
            "target_upper_gap_at_exit_pct": round(target_upper_gap_at_exit_pct, 4),
            "first_manage_pct": round(first_manage_target_pct * 100, 4),
            "first_manage_hit": bool(first_manage_bar),
            "first_manage_time": first_manage_bar.time.isoformat(timespec="minutes") if first_manage_bar else "",
            "first_manage_date": first_manage_bar.date.isoformat() if first_manage_bar else "",
            "first_manage_price": round(first_manage_price, 4) if first_manage_price else "",
            "first_manage_touch": bool(first_manage_touch_bar),
            "first_manage_touch_date": bar_date(first_manage_touch_bar),
            "first_manage_touch_time": bar_time(first_manage_touch_bar),
            "hard_stop_price": round(hard_stop_price, 4),
            "hard_stop_date": bar_date(hard_stop_bar),
            "hard_stop_time": bar_time(hard_stop_bar),
            "trailing_activation_pct": round(trailing_activation_pct * 100, 4),
            "trailing_activated": bool(trailing_activation_bar),
            "trailing_activation_date": bar_date(trailing_activation_bar),
            "trailing_activation_time": bar_time(trailing_activation_bar),
            "trailing_stop_price": round(trailing_stop_price, 4) if trailing_stop_price else "",
            "trailing_trigger_date": bar_date(trailing_trigger_bar),
            "trailing_trigger_time": bar_time(trailing_trigger_bar),
            "trailing_reference_high_at_exit": round(trailing_reference_high_at_exit, 4) if trailing_reference_high_at_exit else "",
            "vwap_fail_date": bar_date(vwap_fail_bar),
            "vwap_fail_time": bar_time(vwap_fail_bar),
            "vwap_fail_vwap": round(vwap_fail_vwap, 4) if vwap_fail_vwap else "",
            "vwap_fail_close": round(vwap_fail_close, 4) if vwap_fail_close else "",
            "vwap_fail_required_bars": effective_vwap_fail_bars if vwap_fail_bar else "",
            "max_below_vwap_count": max_below_vwap_count,
            "min_vwap_distance_pct": round(min_vwap_distance_pct, 4),
            "max_runup_pct": round(max_runup_pct, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "sellable_max_runup_pct": round(sellable_max_runup_pct, 4),
            "sellable_max_drawdown_pct": round(sellable_max_drawdown_pct, 4),
            "exit_signal_path": ";".join(exit_path_flags),
            "execution_interval_minutes": 10,
            "slippage_bps": args.slippage_bps,
            "trailing_reference_policy": args.trailing_reference_policy,
            "limit_threshold_pct": round(limit_threshold * 100, 4),
            "limit_down_blocked_exits": limit_down_blocked_exits,
        }
    )
    return replace(
        planned,
        entry_price=entry_price,
        exit_date=exit_bar.date,
        exit_price=exit_price,
        return_pct=(exit_price / entry_price - 1) * 100 if entry_price else 0.0,
        exit_reason=exit_reason,
        features=features,
    )


def build_planned_by_entry(
    signal_rows: list[Any],
    price_map: dict[str, list[PriceBar]],
    end_date: dt.date,
    args: argparse.Namespace,
) -> dict[dt.date, list[PlannedTrade]]:
    setup_allow = {item.strip() for item in args.setups.split(",") if item.strip()}
    planned_by_entry: dict[dt.date, list[PlannedTrade]] = {}
    for row in signal_rows:
        if not passes_strategy(
            row,
            args.min_score,
            setup_allow,
            args.ma5_mode,
            args.ma5_pullback_limit,
            args.ma5_extension_limit,
            args.sector_mode,
            args.min_sector_momentum_5d,
            args.min_sector_above_ma20_ratio,
        ):
            continue
        planned = plan_trade_base(row, price_map.get(row.ticker, []), end_date, args)
        if planned:
            planned_by_entry.setdefault(planned.entry_date, []).append(planned)
    for date_value in planned_by_entry:
        planned_by_entry[date_value].sort(key=lambda item: selection_key(item, args.selection_mode), reverse=True)
    return planned_by_entry


def required_dates_for_planned(planned: dict[dt.date, list[PlannedTrade]], price_map: dict[str, list[PriceBar]], end_date: dt.date, horizon: int) -> dict[str, set[dt.date]]:
    required: dict[str, set[dt.date]] = {}
    bar_maps = build_bar_maps(price_map)
    for candidates in planned.values():
        for item in candidates:
            dates = sorted(date for date in bar_maps.get(item.ticker, {}) if item.entry_date <= date <= end_date)
            needed = dates[: max(1, horizon)]
            required.setdefault(item.ticker, set()).update(needed)
    return required


def prefetch_intraday(required: dict[str, set[dt.date]], args: argparse.Namespace) -> dict[str, list[IntradayBar]]:
    intraday: dict[str, list[IntradayBar]] = {}
    with BaoStock5mClient(cache_dir=Path(args.baostock_cache_dir), sleep_seconds=args.baostock_sleep_seconds) as client:
        total = len(required)
        for index, (ticker, dates) in enumerate(sorted(required.items()), 1):
            try:
                intraday[ticker] = client.fetch_5m_for_dates(ticker, dates)
            except Exception as exc:
                print(f"warning: intraday fetch failed {index}/{total} {ticker}: {type(exc).__name__}: {exc}", flush=True)
                intraday[ticker] = []
            if index == 1 or index % 10 == 0 or index == total:
                print(f"loaded intraday {index}/{total}", flush=True)
    return intraday


def marked_equity(cash: float, open_positions: list[OpenPosition], bar_maps: dict[str, dict[dt.date, PriceBar]], date_value: dt.date, args: argparse.Namespace) -> float:
    total = cash
    for position in open_positions:
        planned = position.planned
        bar = bar_maps.get(planned.ticker, {}).get(date_value)
        mark_price = bar.close if bar else planned.entry_price
        amount = position.remaining_shares * mark_price
        total += amount - trade_fee(amount, "SELL", args)
    return total


def append_ledger_row(
    rows: list[dict[str, Any]],
    period: str,
    date_value: dt.date,
    action: str,
    planned: PlannedTrade,
    shares: int,
    price: float,
    fee: float,
    cash: float,
    equity: float,
    note: str,
    realized_pnl: float = 0.0,
) -> None:
    features = planned.features
    entry_vwap = float(features.get("entry_vwap") or 0.0)
    entry_vwap_distance_pct = (
        (planned.entry_price / entry_vwap - 1) * 100 if planned.entry_price > 0 and entry_vwap > 0 else 0.0
    )
    rows.append(
        {
            "period": period,
            "date": date_value.isoformat(),
            "time": planned.features.get("entry_time" if action == "BUY" else "first_manage_time" if action == "PARTIAL_SELL" else "exit_time", ""),
            "action": action,
            "ticker": planned.ticker,
            "name": planned.name,
            "shares": shares,
            "price": round(price, 2),
            "amount": round(shares * price, 2),
            "fee": round(fee, 2),
            "cash_after": round(cash, 2),
            "total_equity_after": round(equity, 2),
            "realized_pnl": round(realized_pnl, 2),
            "return_pct": round(planned.return_pct, 4) if action in {"SELL", "PARTIAL_SELL"} else "",
            "reason": planned.exit_reason if action == "SELL" else "first_manage_take_profit_10m" if action == "PARTIAL_SELL" else planned.setup_type,
            "note": note,
            "market_state": features.get("market_state", ""),
            "score": round(planned.score, 4),
            "setup_type": planned.setup_type,
            "entry_time": features.get("entry_time", ""),
            "exit_time": features.get("exit_time", ""),
            "entry_gap_pct": features.get("entry_gap_pct", ""),
            "entry_vwap": features.get("entry_vwap", ""),
            "entry_vwap_distance_pct": round(entry_vwap_distance_pct, 4),
            "target_upper_price": features.get("target_upper_price", ""),
            "target_upper_touch": features.get("target_upper_touch", ""),
            "target_upper_touch_date": features.get("target_upper_touch_date", ""),
            "target_upper_touch_time": features.get("target_upper_touch_time", ""),
            "target_upper_sellable_hit": features.get("target_upper_sellable_hit", ""),
            "target_upper_sellable_date": features.get("target_upper_sellable_date", ""),
            "target_upper_sellable_time": features.get("target_upper_sellable_time", ""),
            "target_upper_gap_at_exit_pct": features.get("target_upper_gap_at_exit_pct", ""),
            "first_manage_pct": features.get("first_manage_pct", ""),
            "first_manage_hit": features.get("first_manage_hit", ""),
            "first_manage_time": features.get("first_manage_time", ""),
            "first_manage_date": features.get("first_manage_date", ""),
            "first_manage_price": features.get("first_manage_price", ""),
            "first_manage_touch": features.get("first_manage_touch", ""),
            "first_manage_touch_date": features.get("first_manage_touch_date", ""),
            "first_manage_touch_time": features.get("first_manage_touch_time", ""),
            "hard_stop_price": features.get("hard_stop_price", ""),
            "hard_stop_date": features.get("hard_stop_date", ""),
            "hard_stop_time": features.get("hard_stop_time", ""),
            "trailing_activation_pct": features.get("trailing_activation_pct", ""),
            "trailing_activated": features.get("trailing_activated", ""),
            "trailing_activation_date": features.get("trailing_activation_date", ""),
            "trailing_activation_time": features.get("trailing_activation_time", ""),
            "trailing_stop_price": features.get("trailing_stop_price", ""),
            "trailing_trigger_date": features.get("trailing_trigger_date", ""),
            "trailing_trigger_time": features.get("trailing_trigger_time", ""),
            "trailing_reference_high_at_exit": features.get("trailing_reference_high_at_exit", ""),
            "vwap_fail_date": features.get("vwap_fail_date", ""),
            "vwap_fail_time": features.get("vwap_fail_time", ""),
            "vwap_fail_vwap": features.get("vwap_fail_vwap", ""),
            "vwap_fail_close": features.get("vwap_fail_close", ""),
            "vwap_fail_required_bars": features.get("vwap_fail_required_bars", ""),
            "max_below_vwap_count": features.get("max_below_vwap_count", ""),
            "min_vwap_distance_pct": features.get("min_vwap_distance_pct", ""),
            "max_runup_pct": features.get("max_runup_pct", ""),
            "max_drawdown_pct": features.get("max_drawdown_pct", ""),
            "sellable_max_runup_pct": features.get("sellable_max_runup_pct", ""),
            "sellable_max_drawdown_pct": features.get("sellable_max_drawdown_pct", ""),
            "exit_signal_path": features.get("exit_signal_path", ""),
            "partial_sell_ratio": features.get("partial_sell_ratio", ""),
            "partial_realized_pnl": features.get("partial_realized_pnl", ""),
            "remaining_realized_pnl": features.get("remaining_realized_pnl", ""),
            "trade_total_realized_pnl": features.get("trade_total_realized_pnl", ""),
            "traded_value_ratio": features.get("traded_value_ratio", ""),
            "atr_pct": features.get("atr_pct", ""),
            "max_5d_range_pct": features.get("max_5d_range_pct", ""),
            "momentum_3d_pct": features.get("momentum_3d_pct", ""),
            "momentum_10d_pct": features.get("momentum_10d_pct", ""),
            "value_ratio_3d": features.get("value_ratio_3d", ""),
            "distance_to_ma5_pct": features.get("distance_to_ma5_pct", ""),
            "close_position_20d_pct": features.get("close_position_20d_pct", ""),
            "sector_group": features.get("sector_group", ""),
            "sector_momentum_5d_pct": features.get("sector_momentum_5d_pct", ""),
            "sector_above_ma20_ratio": features.get("sector_above_ma20_ratio", ""),
            "dynamic_min_score": features.get("dynamic_min_score", ""),
            "dynamic_max_5d_range_pct": features.get("dynamic_max_5d_range_pct", ""),
            "dynamic_max_momentum_10d_pct": features.get("dynamic_max_momentum_10d_pct", ""),
            "dynamic_max_close_position_20d_pct": features.get("dynamic_max_close_position_20d_pct", ""),
            "limit_down_blocked_exits": features.get("limit_down_blocked_exits", ""),
        }
    )


def simulate_period(period: str, planned_by_entry: dict[dt.date, list[PlannedTrade]], price_map: dict[str, list[PriceBar]], intraday_map: dict[str, list[IntradayBar]], start_date: dt.date, end_date: dt.date, args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    trading_dates = sorted({bar.date for bars in price_map.values() for bar in bars if start_date <= bar.date <= end_date})
    bar_maps = build_bar_maps(price_map)
    cash = args.initial_cash
    open_positions: list[OpenPosition] = []
    ledger: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    closed_returns: list[float] = []
    closed_pnls: list[float] = []
    cooldown_until: dt.date | None = None
    skipped_candidates = 0
    skipped_no_lot = 0

    for date_value in trading_dates:
        temperature = market_temperature(price_map, date_value)
        overrides = dynamic_param_overrides(str(temperature["state"]), args)
        regime, cooldown_until = evaluate_regime(
            date_value,
            [],
            daily,
            args.initial_cash,
            cooldown_until,
            args.regime_filter,
            args.regime_mode,
            args.regime_lookback_trades,
            args.regime_min_trades,
            args.regime_min_win_rate,
            args.regime_max_hard_stop_rate,
            args.regime_max_drawdown,
            [],
            args.market_ma_days,
            args.market_lookback_days,
            args.market_min_return,
            0,
        )
        for raw_planned in planned_by_entry.get(date_value, []):
            if regime.action == "skip":
                break
            if len(open_positions) >= args.max_positions:
                break
            if any(position.planned.ticker == raw_planned.ticker for position in open_positions):
                continue
            if not planned_passes_dynamic_filters(raw_planned, overrides):
                continue
            if not passes_market_quality(raw_planned, str(temperature["state"]), args):
                skipped_candidates += 1
                continue
            planned = refine_trade_strict_10m(raw_planned, price_map.get(raw_planned.ticker, []), intraday_map.get(raw_planned.ticker, []), end_date, args)
            if not planned:
                skipped_candidates += 1
                continue
            entry_time = parse_clock(str(planned.features.get("entry_time") or "09:50"))
            normal_entry_end = parse_optional_clock(args.normal_entry_end_time)
            if str(temperature["state"]) == "normal" and normal_entry_end is not None and entry_time > normal_entry_end:
                skipped_candidates += 1
                continue
            cold_first_end = parse_optional_clock(args.cold_first_entry_end_time)
            if (
                str(temperature["state"]) == "cold"
                and args.cold_first_entry_min_score > 0
                and cold_first_end is not None
                and entry_time <= cold_first_end
                and planned.score < args.cold_first_entry_min_score
            ):
                skipped_candidates += 1
                continue
            planned = replace(
                planned,
                features={
                    **planned.features,
                    "market_state": temperature["state"],
                    "dynamic_min_score": overrides.get("min_score", args.min_score),
                    "dynamic_max_5d_range_pct": overrides.get("max_5d_range_pct", getattr(args, "max_5d_range_pct", 0.0)),
                    "dynamic_max_momentum_10d_pct": overrides.get("max_momentum_10d_pct", getattr(args, "max_momentum_10d_pct", 999.0)),
                    "dynamic_max_close_position_20d_pct": overrides.get("max_close_position_20d_pct", getattr(args, "max_close_position_20d_pct", 100.0)),
                },
            )
            slots = max(1, args.max_positions - len(open_positions))
            capital = cash / slots
            if regime.action == "reduce":
                capital *= max(0.0, min(1.0, args.regime_risk_factor))
            if capital <= 0:
                continue
            capital *= market_capital_factor(str(temperature["state"]), args)
            if capital <= 0:
                continue
            shares, total_cost, buy_fee = max_affordable_lot_shares(capital, planned.entry_price, args)
            if shares <= 0:
                skipped_no_lot += 1
                continue
            cash -= total_cost
            open_positions.append(
                OpenPosition(
                    planned=planned,
                    shares=shares,
                    remaining_shares=shares,
                    cost_basis=total_cost,
                    remaining_cost_basis=total_cost,
                    buy_fee=buy_fee,
                )
            )
            equity = marked_equity(cash, open_positions, bar_maps, date_value, args)
            append_ledger_row(ledger, period, date_value, "BUY", planned, shares, planned.entry_price, buy_fee, cash, equity, f"score={planned.score:.1f}; market={temperature['state']}")

        still_open: list[OpenPosition] = []
        for position in open_positions:
            planned = position.planned
            current_position = position
            first_manage_date = parse_date(str(planned.features.get("first_manage_date") or ""))
            first_manage_price = float(planned.features.get("first_manage_price") or 0.0)
            if (
                args.partial_take_profit
                and not current_position.first_manage_done
                and first_manage_date is not None
                and first_manage_date <= date_value
                and first_manage_price > 0
            ):
                market_state = str(planned.features.get("market_state") or "")
                partial_ratio = partial_sell_ratio_for_state(market_state, args)
                sell_shares = partial_sell_shares(current_position.remaining_shares, partial_ratio, args)
                if sell_shares > 0:
                    amount = sell_shares * first_manage_price
                    sell_fee = trade_fee(amount, "SELL", args)
                    proceeds = amount - sell_fee
                    partial_cost = current_position.remaining_cost_basis * sell_shares / current_position.remaining_shares
                    pnl = proceeds - partial_cost
                    cash += proceeds
                    partial_return_pct = pnl / partial_cost * 100 if partial_cost else 0.0
                    features = {
                        **planned.features,
                        "partial_sell_ratio": round(partial_ratio, 4),
                        "partial_realized_pnl": round(pnl, 2),
                    }
                    partial_planned = replace(
                        planned,
                        return_pct=partial_return_pct,
                        exit_reason="first_manage_take_profit_10m",
                        features=features,
                    )
                    current_position = replace(
                        current_position,
                        planned=replace(planned, features=features),
                        remaining_shares=current_position.remaining_shares - sell_shares,
                        remaining_cost_basis=current_position.remaining_cost_basis - partial_cost,
                        first_manage_done=True,
                        realized_partial_pnl=current_position.realized_partial_pnl + pnl,
                    )
                    equity = marked_equity(cash, [*still_open, current_position], bar_maps, date_value, args)
                    append_ledger_row(ledger, period, date_value, "PARTIAL_SELL", partial_planned, sell_shares, first_manage_price, sell_fee, cash, equity, f"ratio={partial_ratio:.2f}", pnl)
                    planned = current_position.planned
            if planned.exit_date <= date_value:
                amount = current_position.remaining_shares * planned.exit_price
                sell_fee = trade_fee(amount, "SELL", args)
                proceeds = amount - sell_fee
                cash += proceeds
                remaining_pnl = proceeds - current_position.remaining_cost_basis
                total_pnl = current_position.realized_partial_pnl + remaining_pnl
                net_return_pct = total_pnl / current_position.cost_basis * 100 if current_position.cost_basis else 0.0
                closed_returns.append(net_return_pct)
                closed_pnls.append(total_pnl)
                sell_planned = replace(
                    planned,
                    return_pct=net_return_pct,
                    features={
                        **planned.features,
                        "remaining_realized_pnl": round(remaining_pnl, 2),
                        "trade_total_realized_pnl": round(total_pnl, 2),
                    },
                )
                equity = marked_equity(cash, still_open, bar_maps, date_value, args)
                append_ledger_row(ledger, period, date_value, "SELL", sell_planned, current_position.remaining_shares, planned.exit_price, sell_fee, cash, equity, "", remaining_pnl)
            else:
                still_open.append(current_position)
        open_positions = still_open

        equity = marked_equity(cash, open_positions, bar_maps, date_value, args)
        daily.append(
            {
                "period": period,
                "date": date_value.isoformat(),
                "cash": round(cash, 2),
                "equity": round(equity, 2),
                "total_equity": round(equity, 2),
                "open_positions": len(open_positions),
                "market_state": temperature["state"],
            }
        )

    final_equity = float(daily[-1]["total_equity"]) if daily else args.initial_cash
    wins = sum(1 for value in closed_returns if value > 0)
    closed = len(closed_returns)
    gross_profit = sum(value for value in closed_pnls if value > 0)
    gross_loss = -sum(value for value in closed_pnls if value < 0)
    partial_rows = [row for row in ledger if row.get("action") == "PARTIAL_SELL"]
    partial_pnl = sum(float(row.get("realized_pnl") or 0.0) for row in partial_rows)
    remaining_pnl = sum(float(row.get("realized_pnl") or 0.0) for row in ledger if row.get("action") == "SELL")
    summary = {
        "period": period,
        "start_date": trading_dates[0].isoformat() if trading_dates else start_date.isoformat(),
        "end_date": trading_dates[-1].isoformat() if trading_dates else end_date.isoformat(),
        "initial_cash": round(args.initial_cash, 2),
        "final_cash": round(cash, 2),
        "final_equity": round(final_equity, 2),
        "return_pct": round((final_equity / args.initial_cash - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown(daily, args.initial_cash), 4),
        "closed_trades": closed,
        "win_rate_pct": round(wins / closed * 100, 4) if closed else 0.0,
        "open_positions_end": len(open_positions),
        "partial_sells": len(partial_rows),
        "partial_realized_pnl": round(partial_pnl, 2),
        "remaining_realized_pnl": round(remaining_pnl, 2),
        "skipped_after_intraday_filters": skipped_candidates,
        "skipped_no_lot": skipped_no_lot,
        "interval_minutes": 10,
        "slippage_bps": args.slippage_bps,
        "lot_size": args.lot_size,
        "price_tick": args.price_tick,
        "commission_bps": args.commission_bps,
        "min_commission": args.min_commission,
        "stamp_tax_bps": args.stamp_tax_bps,
        "transfer_fee_bps": args.transfer_fee_bps,
        "hot_capital_factor": args.hot_capital_factor,
        "normal_capital_factor": args.normal_capital_factor,
        "cold_capital_factor": args.cold_capital_factor,
        "partial_take_profit": args.partial_take_profit,
        "first_manage_ratio": args.first_manage_ratio,
        "first_manage_min": args.first_manage_min,
        "partial_sell_ratio_normal": args.partial_sell_ratio_normal,
        "partial_sell_ratio_cold": args.partial_sell_ratio_cold,
        "partial_sell_ratio_narrow_rally": args.partial_sell_ratio_narrow_rally,
        "trailing_reference_policy": args.trailing_reference_policy,
        "vwap_fail_bars": args.vwap_fail_bars,
        "vwap_fail_after_first_manage_bars": args.vwap_fail_after_first_manage_bars,
        "cold_min_traded_value_ratio": args.cold_min_traded_value_ratio,
        "cold_min_atr_pct": args.cold_min_atr_pct,
        "cold_min_momentum_10d_pct": args.cold_min_momentum_10d_pct,
        "cold_min_sector_momentum_5d_pct": args.cold_min_sector_momentum_5d_pct,
        "normal_min_5d_range_pct": args.normal_min_5d_range_pct,
        "normal_min_atr_pct": args.normal_min_atr_pct,
        "avg_trade_return_pct": round(sum(closed_returns) / closed, 4) if closed else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else 0.0,
        "event_file": args.events or "disabled",
        "event_weight": "official_score_adjustment" if args.events else 0,
    }
    return ledger, daily, summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strict 10-minute execution backtest ledger.")
    parser.add_argument("--watchlist", default="config/watchlist.mainboard_liquid.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES))
    parser.add_argument("--events", default="", help="official event score JSON; disabled by default")
    parser.add_argument("--end-date", default=default_end_date().isoformat())
    parser.add_argument("--period-months", default="1,3,6")
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--min-score", type=float, default=90.0)
    parser.add_argument("--selection-mode", choices=["score", "score_quality", "quality", "score_low_heat"], default="score")
    parser.add_argument("--setups", default="VOLUME_BREAKOUT,HIGH_VOLATILITY")
    parser.add_argument("--horizon", type=int, default=3)
    parser.add_argument("--take-profit", type=float, default=0.10)
    parser.add_argument("--hard-stop", type=float, default=0.04)
    parser.add_argument("--trailing-stop", type=float, default=0.035)
    parser.add_argument("--dynamic-exit", action="store_true", default=True)
    parser.add_argument("--target-atr-mult", type=float, default=0.9)
    parser.add_argument("--target-range-mult", type=float, default=0.35)
    parser.add_argument("--target-min", type=float, default=0.05)
    parser.add_argument("--target-max", type=float, default=0.18)
    parser.add_argument("--stop-atr-mult", type=float, default=0.45)
    parser.add_argument("--stop-min", type=float, default=0.015)
    parser.add_argument("--stop-max", type=float, default=0.07)
    parser.add_argument("--trail-atr-mult", type=float, default=0.25)
    parser.add_argument("--trail-min", type=float, default=0.025)
    parser.add_argument("--trail-max", type=float, default=0.06)
    parser.add_argument(
        "--trailing-reference-policy",
        choices=["previous_high", "same_bar_high"],
        default="previous_high",
        help="Use previous bars' high for trailing-stop triggers; same_bar_high reproduces the older optimistic 10m OHLC assumption.",
    )
    parser.add_argument("--partial-take-profit", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--first-manage-ratio", type=float, default=0.4)
    parser.add_argument("--first-manage-min", type=float, default=0.04)
    parser.add_argument("--partial-sell-ratio-normal", type=float, default=0.4)
    parser.add_argument("--partial-sell-ratio-cold", type=float, default=0.5)
    parser.add_argument("--partial-sell-ratio-narrow-rally", type=float, default=0.3)
    parser.add_argument("--cold-min-traded-value-ratio", type=float, default=0.0)
    parser.add_argument("--cold-min-atr-pct", type=float, default=4.1)
    parser.add_argument("--cold-min-momentum-10d-pct", type=float, default=7.5)
    parser.add_argument("--cold-min-sector-momentum-5d-pct", type=float, default=-999.0)
    parser.add_argument("--normal-min-5d-range-pct", type=float, default=0.0)
    parser.add_argument("--normal-min-atr-pct", type=float, default=4.1)
    parser.add_argument("--fee-bps", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--commission-bps", type=float, default=3.0)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--stamp-tax-bps", type=float, default=5.0)
    parser.add_argument("--transfer-fee-bps", type=float, default=0.1)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--price-tick", type=float, default=0.01)
    parser.add_argument("--lot-size", type=int, default=100)
    parser.add_argument("--limit-threshold", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--mainboard-limit-threshold", type=float, default=0.10)
    parser.add_argument("--growth-limit-threshold", type=float, default=0.20)
    parser.add_argument("--reject-limit-open", action="store_true", default=True)
    parser.add_argument("--reject-limit-entry", action="store_true", default=True)
    parser.add_argument("--reject-limit-exit", action="store_true", default=True)
    parser.add_argument("--min-traded-value", type=float, default=200_000_000)
    parser.add_argument("--regime-filter", action="store_true", default=True)
    parser.add_argument("--regime-mode", choices=["skip", "reduce"], default="skip")
    parser.add_argument("--regime-lookback-trades", type=int, default=12)
    parser.add_argument("--regime-min-trades", type=int, default=8)
    parser.add_argument("--regime-min-win-rate", type=float, default=0.30)
    parser.add_argument("--regime-max-hard-stop-rate", type=float, default=0.45)
    parser.add_argument("--regime-max-drawdown", type=float, default=0.06)
    parser.add_argument("--regime-risk-factor", type=float, default=0.35)
    parser.add_argument("--market-ma-days", type=int, default=20)
    parser.add_argument("--market-lookback-days", type=int, default=5)
    parser.add_argument("--market-min-return", type=float, default=-0.04)
    parser.add_argument("--ma5-mode", choices=["ignore", "filter", "pullback"], default="ignore")
    parser.add_argument("--ma5-pullback-limit", type=float, default=0.025)
    parser.add_argument("--ma5-extension-limit", type=float, default=0.04)
    parser.add_argument("--sector-mode", choices=["ignore", "filter", "strong"], default="ignore")
    parser.add_argument("--min-sector-momentum-5d", type=float, default=-0.03)
    parser.add_argument("--min-sector-above-ma20-ratio", type=float, default=0.35)
    parser.add_argument("--entry-start-time", default="09:50")
    parser.add_argument("--entry-end-time", default="11:20")
    parser.add_argument("--normal-entry-end-time", default="10:40", help="optional stricter latest entry time for normal markets")
    parser.add_argument("--cold-first-entry-min-score", type=float, default=0.0, help="optional min score for cold-market first-window entries")
    parser.add_argument("--cold-first-entry-end-time", default="09:50")
    parser.add_argument("--max-gap-up", type=float, default=0.02)
    parser.add_argument("--max-gap-down", type=float, default=0.03)
    parser.add_argument("--gap-volume-threshold", type=float, default=0.0)
    parser.add_argument("--gap-volume-min-ratio", type=float, default=1.5)
    parser.add_argument("--confirm-buffer", type=float, default=0.0)
    parser.add_argument("--vwap-buffer", type=float, default=0.003)
    parser.add_argument("--max-entry-extension", type=float, default=0.04)
    parser.add_argument("--vwap-fail-bars", type=int, default=1)
    parser.add_argument("--vwap-fail-after-first-manage-bars", type=int, default=2)
    parser.add_argument("--vwap-fail-buffer", type=float, default=0.0)
    parser.add_argument("--dynamic-params", action="store_true", default=True)
    parser.add_argument("--hot-capital-factor", type=float, default=0.0)
    parser.add_argument("--normal-capital-factor", type=float, default=1.0)
    parser.add_argument("--cold-capital-factor", type=float, default=1.0)
    parser.add_argument("--hot-min-score", type=float, default=90.0)
    parser.add_argument("--hot-max-gap-up", type=float, default=0.02)
    parser.add_argument("--hot-gap-volume-min-ratio", type=float, default=1.5)
    parser.add_argument("--hot-max-5d-range-pct", type=float, default=32.0)
    parser.add_argument("--hot-max-momentum-10d-pct", type=float, default=26.0)
    parser.add_argument("--hot-max-close-position-20d-pct", type=float, default=85.0)
    parser.add_argument("--normal-min-score", type=float, default=90.0)
    parser.add_argument("--normal-max-gap-up", type=float, default=0.02)
    parser.add_argument("--normal-gap-volume-min-ratio", type=float, default=1.5)
    parser.add_argument("--normal-max-5d-range-pct", type=float, default=32.0)
    parser.add_argument("--normal-max-momentum-10d-pct", type=float, default=26.0)
    parser.add_argument("--normal-max-close-position-20d-pct", type=float, default=85.0)
    parser.add_argument("--narrow-rally-min-score", type=float, default=90.0)
    parser.add_argument("--narrow-rally-max-gap-up", type=float, default=0.01)
    parser.add_argument("--narrow-rally-gap-volume-min-ratio", type=float, default=1.5)
    parser.add_argument("--narrow-rally-max-5d-range-pct", type=float, default=25.0)
    parser.add_argument("--narrow-rally-max-momentum-10d-pct", type=float, default=20.0)
    parser.add_argument("--narrow-rally-max-close-position-20d-pct", type=float, default=80.0)
    parser.add_argument("--cold-min-score", type=float, default=90.0)
    parser.add_argument("--cold-max-gap-up", type=float, default=0.01)
    parser.add_argument("--cold-gap-volume-min-ratio", type=float, default=1.5)
    parser.add_argument("--cold-max-5d-range-pct", type=float, default=25.0)
    parser.add_argument("--cold-max-momentum-10d-pct", type=float, default=20.0)
    parser.add_argument("--cold-max-close-position-20d-pct", type=float, default=80.0)
    parser.add_argument("--history-timeout", type=float, default=5.0)
    parser.add_argument("--daily-cache-dir", default="output/backtest_daily_cache")
    parser.add_argument("--disable-baostock-daily-fallback", action="store_true")
    parser.add_argument("--baostock-cache-dir", default="output/baostock_5m_cache")
    parser.add_argument("--baostock-sleep-seconds", type=float, default=0.15)
    parser.add_argument("--out-dir", default="output/backtest_strict_10m")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    end_date = parse_date(args.end_date)
    if not end_date:
        raise SystemExit("end date must be YYYY-MM-DD")
    periods = sorted({int(item.strip()) for item in args.period_months.split(",") if item.strip()})
    earliest_start = subtract_months(end_date, max(periods))
    fetch_start = earliest_start - dt.timedelta(days=170)
    fetch_end = end_date + dt.timedelta(days=args.horizon * 3 + 10)
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    event_scores = official_event_score_by_symbol(Path(args.events)) if args.events else {}
    if args.events:
        print(f"official event scores={len(event_scores)} source={args.events}", flush=True)
    price_map: dict[str, list[PriceBar]] = {}
    daily_cache_dir = Path(args.daily_cache_dir)
    baostock_daily_client: BaoStockDailyClient | None = None
    try:
        for index, symbol in enumerate(symbols, 1):
            bars: list[PriceBar] = []
            try:
                bars = cached_history(session, symbol.yahoo_symbol or symbol.ticker, symbol.ticker, fetch_start, fetch_end, daily_cache_dir, args.history_timeout)
                last_date = max((bar.date for bar in bars), default=None)
                if not args.disable_baostock_daily_fallback and (last_date is None or last_date < end_date):
                    if baostock_daily_client is None:
                        baostock_daily_client = BaoStockDailyClient()
                        baostock_daily_client.login()
                    fallback_bars = baostock_daily_client.fetch_history(symbol.ticker, fetch_start, end_date)
                    fallback_last = max((bar.date for bar in fallback_bars), default=None)
                    if fallback_bars and (last_date is None or (fallback_last is not None and fallback_last >= last_date)):
                        bars = fallback_bars
                        write_daily_cache(daily_cache_dir, symbol.ticker, fetch_start, fetch_end, bars)
            except Exception as exc:
                print(f"warning: daily history failed {index}/{len(symbols)} {symbol.ticker}: {type(exc).__name__}: {exc}", flush=True)
            price_map[symbol.ticker] = bars
            if index == 1 or index % 50 == 0 or index == len(symbols):
                print(f"loaded daily {index}/{len(symbols)}", flush=True)
    finally:
        if baostock_daily_client is not None:
            baostock_daily_client.logout()

    all_planned: dict[dt.date, list[PlannedTrade]] = {}
    period_planned: dict[int, dict[dt.date, list[PlannedTrade]]] = {}
    for months in periods:
        start_date = subtract_months(end_date, months)
        rows = build_signal_rows(symbols, price_map, start_date, end_date, args.horizon, event_scores, args.min_traded_value, args.take_profit, args.hard_stop, args.trailing_stop)
        apply_official_event_adjustments(rows, event_scores)
        planned = build_planned_by_entry(rows, price_map, end_date, args)
        period_planned[months] = planned
        for date_value, items in planned.items():
            all_planned.setdefault(date_value, []).extend(items)
        print(f"planned {months}M candidates={sum(len(items) for items in planned.values())}", flush=True)

    required = required_dates_for_planned(all_planned, price_map, end_date, args.horizon)
    print(f"intraday required tickers={len(required)}", flush=True)
    intraday_map = prefetch_intraday(required, args)

    all_ledger: list[dict[str, Any]] = []
    all_daily: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for months in periods:
        start_date = subtract_months(end_date, months)
        label = f"{months}M"
        print(f"simulating {label}: {start_date} to {end_date}", flush=True)
        ledger, daily, summary = simulate_period(label, period_planned[months], price_map, intraday_map, start_date, end_date, args)
        all_ledger.extend(ledger)
        all_daily.extend(daily)
        summaries.append(summary)
        print(f"{label}: trades={summary['closed_trades']} final_equity={summary['final_equity']} return={summary['return_pct']}% skipped={summary['skipped_after_intraday_filters']}", flush=True)

    prefix = f"strict_10m_no_events_{min(periods)}M_{max(periods)}M_to_{end_date:%Y%m%d}"
    outputs = write_outputs(Path(args.out_dir), prefix, all_ledger, all_daily, summaries)
    outputs.update(write_distribution_outputs(Path(args.out_dir), prefix, all_ledger))
    outputs.update(write_condition_diagnostic_outputs(Path(args.out_dir), prefix, all_ledger))
    outputs.update(write_sell_path_outputs(Path(args.out_dir), prefix, all_ledger))
    outputs.update(write_hit_rate_calibration_outputs(Path(args.out_dir), prefix, all_ledger))
    for name, path in outputs.items():
        print(f"{name}={path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
