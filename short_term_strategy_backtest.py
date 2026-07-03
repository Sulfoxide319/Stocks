#!/usr/bin/env python3
"""Backtest the short-term pattern strategy with active exits.

The strategy:
1. Computes features known at each close.
2. Buys selected candidates on the next trading day's open.
3. Exits using active rules approximated from daily OHLC:
   take-profit, hard stop, trailing stop, or time exit.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import textwrap
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dependency_bootstrap import ensure_project_dependencies

ensure_project_dependencies()

import requests

from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from short_term_pattern_miner import (
    PatternRow,
    build_rows_for_symbol,
    event_score_by_symbol,
)
from tech_event_backtest import PriceBar, fetch_yahoo_history
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date


@dataclass
class PlannedTrade:
    ticker: str
    name: str
    signal_date: dt.date
    entry_date: dt.date
    exit_date: dt.date
    entry_price: float
    exit_price: float
    return_pct: float
    exit_reason: str
    target_pct: float
    hard_stop_pct: float
    trailing_stop_pct: float
    score: float
    setup_type: str
    features: dict[str, Any]


@dataclass
class ExecutedTrade:
    ticker: str
    name: str
    setup_type: str
    signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_pct: float
    exit_reason: str
    target_pct: float
    hard_stop_pct: float
    trailing_stop_pct: float
    score: float
    cash_after: float
    selection_features: str = ""


@dataclass
class RegimeCheck:
    state: str
    action: str
    reasons: list[str]


def build_bar_maps(price_map: dict[str, list[PriceBar]]) -> dict[str, dict[dt.date, PriceBar]]:
    return {ticker: {bar.date: bar for bar in bars} for ticker, bars in price_map.items()}


def infer_sector_group(notes: str, name: str = "") -> str:
    text = f"{notes} {name}".lower()
    if any(word in text for word in ["optical", "communication modules", "data center"]):
        return "optical"
    if any(word in text for word in ["pcb", "copper clad", "substrate"]):
        return "pcb"
    if any(word in text for word in ["semiconductor", "chip", "chips", "mcu", "fpga", "foundry", "memory", "wafer", "packaging"]):
        return "semiconductor"
    if any(word in text for word in ["server", "computing", "supercomputing"]):
        return "compute"
    if any(word in text for word in ["robot", "automation", "reducer", "transmission"]):
        return "robotics"
    if any(word in text for word in ["software", "cybersecurity", "cloud", "ai application", "internet finance"]):
        return "software"
    if any(word in text for word in ["consumer electronics", "smart hardware", "mr hardware"]):
        return "consumer_electronics"
    if any(word in text for word in ["automotive", "smart cockpit", "autonomous driving"]):
        return "auto_tech"
    return "other_tech"


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def first_index_after(bars: list[PriceBar], date_value: dt.date) -> int | None:
    for index, bar in enumerate(bars):
        if bar.date > date_value:
            return index
    return None


def plan_trade_from_signal(
    row: PatternRow,
    bars: list[PriceBar],
    horizon: int,
    take_profit: float,
    hard_stop: float,
    trailing_stop: float,
    dynamic_exit: bool,
    target_atr_mult: float,
    target_range_mult: float,
    event_bonus: float,
    target_min: float,
    target_max: float,
    stop_atr_mult: float,
    stop_min: float,
    stop_max: float,
    trail_atr_mult: float,
    trail_min: float,
    trail_max: float,
    end_date: dt.date,
    execution_model: str = "open",
    max_gap_up: float = 0.04,
    max_gap_down: float = 0.03,
    confirm_buffer: float = 0.003,
    max_entry_extension: float = 0.05,
    intraday_fail_exit: bool = False,
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
    entry_price = entry_bar.open
    signal_close = row.close
    if execution_model == "confirm":
        gap_pct = entry_bar.open / signal_close - 1 if signal_close else 0.0
        if gap_pct > max_gap_up or gap_pct < -max_gap_down:
            return None
        trigger_price = max(entry_bar.open, signal_close * (1 + confirm_buffer))
        if entry_bar.high < trigger_price:
            return None
        if signal_close and trigger_price / signal_close - 1 > max_entry_extension:
            return None
        entry_price = trigger_price
    target_pct = take_profit
    hard_stop_pct = hard_stop
    trailing_stop_pct = trailing_stop
    if dynamic_exit:
        raw_target = (
            row.atr_pct / 100 * target_atr_mult
            + row.max_5d_range_pct / 100 * target_range_mult
            + (event_bonus if row.setup_type == "EVENT_PLUS_VOLATILITY" else 0)
        )
        target_pct = min(target_max, max(target_min, raw_target))
        hard_stop_pct = min(stop_max, max(stop_min, row.atr_pct / 100 * stop_atr_mult))
        trailing_stop_pct = min(trail_max, max(trail_min, row.atr_pct / 100 * trail_atr_mult))

    best_high = entry_price
    final_index = min(entry_index + horizon - 1, len(bars) - 1)
    exit_bar = bars[final_index]
    exit_price = exit_bar.close
    exit_reason = "time_exit"

    for index in range(entry_index, final_index + 1):
        bar = bars[index]
        if bar.date > end_date:
            break
        if bar.low <= entry_price * (1 - hard_stop_pct):
            exit_bar = bar
            exit_price = entry_price * (1 - hard_stop_pct)
            exit_reason = "hard_stop"
            break
        if bar.high > best_high:
            best_high = bar.high
        if bar.high >= entry_price * (1 + target_pct):
            exit_bar = bar
            exit_price = entry_price * (1 + target_pct)
            exit_reason = "take_profit"
            break
        if best_high >= entry_price * (1 + max(0.04, target_pct * 0.4)) and bar.low <= best_high * (1 - trailing_stop_pct):
            exit_bar = bar
            exit_price = best_high * (1 - trailing_stop_pct)
            exit_reason = "trailing_stop"
            break
        if (
            intraday_fail_exit
            and execution_model == "confirm"
            and index == entry_index
            and bar.close < max(signal_close, entry_price * (1 - 0.005))
        ):
            exit_bar = bar
            exit_price = bar.close
            exit_reason = "intraday_fail"
            break

    if exit_bar.date > end_date:
        return None
    return PlannedTrade(
        ticker=row.ticker,
        name=row.name,
        signal_date=signal_date,
        entry_date=entry_bar.date,
        exit_date=exit_bar.date,
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=(exit_price / entry_price - 1) * 100 if entry_price else 0,
        exit_reason=exit_reason,
        target_pct=target_pct,
        hard_stop_pct=hard_stop_pct,
        trailing_stop_pct=trailing_stop_pct,
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


def build_signal_rows(
    symbols: list[Any],
    price_map: dict[str, list[PriceBar]],
    start_date: dt.date,
    end_date: dt.date,
    horizon: int,
    event_scores: dict[str, int],
    min_traded_value: float,
    take_profit: float,
    hard_stop: float,
    trailing_stop: float,
) -> list[PatternRow]:
    rows: list[PatternRow] = []
    sector_by_ticker = {symbol.ticker: infer_sector_group(getattr(symbol, "notes", ""), getattr(symbol, "name", "")) for symbol in symbols}
    ticker_date_stats: dict[str, dict[dt.date, tuple[float, bool]]] = {}
    sector_date_values: dict[tuple[str, dt.date], list[tuple[float, bool]]] = {}
    for symbol in symbols:
        sector = sector_by_ticker.get(symbol.ticker, "other_tech")
        bars = price_map.get(symbol.ticker, [])
        closes = [bar.close for bar in bars]
        stats: dict[dt.date, tuple[float, bool]] = {}
        for index, bar in enumerate(bars):
            if index < 20:
                continue
            close_5d_ago = bars[index - 5].close if index >= 5 else bar.close
            momentum_5d = (bar.close / close_5d_ago - 1) * 100 if close_5d_ago else 0.0
            ma20 = sum(closes[index - 19 : index + 1]) / 20
            above_ma20 = bar.close > ma20
            stats[bar.date] = (momentum_5d, above_ma20)
            sector_date_values.setdefault((sector, bar.date), []).append((momentum_5d, above_ma20))
        ticker_date_stats[symbol.ticker] = stats

    sector_context: dict[tuple[str, dt.date], tuple[float, float]] = {}
    for key, values in sector_date_values.items():
        momentums = [item[0] for item in values]
        above_ratio = sum(1 for item in values if item[1]) / len(values) if values else 0.0
        sector_context[key] = (median(momentums), above_ratio)

    for symbol in symbols:
        symbol_rows = build_rows_for_symbol(
            symbol,
            price_map.get(symbol.ticker, []),
            start_date,
            end_date,
            horizon,
            event_scores,
            min_traded_value,
            take_profit,
            hard_stop,
            trailing_stop,
        )
        sector = sector_by_ticker.get(symbol.ticker, "other_tech")
        for row in symbol_rows:
            row.sector_group = sector
            row_date = parse_date(row.date)
            if row_date:
                sector_momentum, sector_above = sector_context.get((sector, row_date), (0.0, 0.0))
                row.sector_momentum_5d_pct = round(sector_momentum, 2)
                row.sector_above_ma20_ratio = round(sector_above, 4)
        rows.extend(symbol_rows)
    return rows


def passes_strategy(
    row: PatternRow,
    min_score: float,
    setup_allow: set[str],
    ma5_mode: str = "ignore",
    ma5_pullback_limit: float = 0.025,
    ma5_extension_limit: float = 0.0,
    sector_mode: str = "ignore",
    min_sector_momentum_5d: float = -0.03,
    min_sector_above_ma20_ratio: float = 0.35,
) -> bool:
    if row.score < min_score:
        return False
    if row.setup_type not in setup_allow:
        return False
    if not row.above_ma20:
        return False
    if ma5_mode == "filter" and not row.above_ma5:
        return False
    if ma5_mode == "pullback" and not row.above_ma5:
        if row.distance_to_ma5_pct < -ma5_pullback_limit * 100:
            return False
        if row.distance_to_20d_high_pct < -8:
            return False
        if row.traded_value_ratio < 1.2 and row.setup_type != "EVENT_PLUS_VOLATILITY":
            return False
    if ma5_extension_limit > 0 and row.distance_to_ma5_pct > ma5_extension_limit * 100:
        return False
    if sector_mode == "filter":
        if row.sector_momentum_5d_pct < min_sector_momentum_5d * 100:
            return False
        if row.sector_above_ma20_ratio < min_sector_above_ma20_ratio:
            return False
    elif sector_mode == "strong":
        if row.sector_momentum_5d_pct < max(0.0, min_sector_momentum_5d * 100):
            return False
        if row.sector_above_ma20_ratio < max(0.5, min_sector_above_ma20_ratio):
            return False
    if row.distance_to_20d_high_pct < -12:
        return False
    if row.traded_value < 200_000_000:
        return False
    return True


def planned_selection_score(planned: PlannedTrade) -> float:
    features = planned.features
    probability = 0.35 + max(0.0, planned.score - 85) * 0.006
    value_ratio = float(features.get("traded_value_ratio") or 0.0)
    value_ratio_3d = float(features.get("value_ratio_3d") or 0.0)
    momentum_3d = float(features.get("momentum_3d_pct") or 0.0)
    distance_ma5 = float(features.get("distance_to_ma5_pct") or 0.0)
    close_position = float(features.get("close_position_20d_pct") or 0.0)
    distance_high = float(features.get("distance_to_20d_high_pct") or 0.0)
    if value_ratio >= 1.5:
        probability += 0.04
    if value_ratio_3d >= 1.2:
        probability += 0.02
    if 2 <= momentum_3d <= 12:
        probability += 0.03
    if 55 <= close_position <= 85:
        probability += 0.02
    if close_position > 90:
        probability -= 0.04
    if distance_ma5 > 4:
        probability -= 0.08
    elif distance_ma5 < -3:
        probability -= 0.05
    if distance_high > -2:
        probability -= 0.02
    probability = min(0.70, max(0.20, probability))
    expected = (probability * planned.target_pct - (1 - probability) * planned.hard_stop_pct) * 100
    return expected + planned.score * 0.001


def planned_selection_rank(planned: PlannedTrade) -> tuple[float, float]:
    return (planned.score, planned_selection_score(planned))


def recent_trade_regime(
    trades: list[ExecutedTrade],
    lookback: int,
    min_trades: int,
    min_win_rate: float,
    max_hard_stop_rate: float,
) -> list[str]:
    if lookback <= 0:
        return []
    recent = trades[-lookback:]
    if len(recent) < min_trades:
        return []
    win_rate = sum(1 for trade in recent if trade.return_pct > 0) / len(recent)
    hard_stop_rate = sum(1 for trade in recent if trade.exit_reason == "hard_stop") / len(recent)
    reasons: list[str] = []
    if win_rate < min_win_rate:
        reasons.append(f"recent_win_rate={win_rate:.2f}<min={min_win_rate:.2f}")
    if hard_stop_rate > max_hard_stop_rate:
        reasons.append(f"hard_stop_rate={hard_stop_rate:.2f}>max={max_hard_stop_rate:.2f}")
    return reasons


def current_drawdown_fraction(equity_curve: list[dict[str, Any]], initial_cash: float) -> float:
    if not equity_curve:
        return 0.0
    peak = initial_cash
    for point in equity_curve:
        peak = max(peak, float(point["equity"]))
    if not peak:
        return 0.0
    return max(0.0, 1 - float(equity_curve[-1]["equity"]) / peak)


def last_index_at_or_before(bars: list[PriceBar], date_value: dt.date) -> int | None:
    found: int | None = None
    for index, bar in enumerate(bars):
        if bar.date <= date_value:
            found = index
        else:
            break
    return found


def market_regime_reasons(
    market_bars: list[PriceBar],
    date_value: dt.date,
    ma_days: int,
    lookback_days: int,
    min_return: float,
) -> list[str]:
    if not market_bars:
        return []
    index = last_index_at_or_before(market_bars, date_value)
    if index is None:
        return []
    reasons: list[str] = []
    if ma_days > 1 and index + 1 >= ma_days:
        ma = sum(bar.close for bar in market_bars[index - ma_days + 1 : index + 1]) / ma_days
        if ma and market_bars[index].close < ma:
            reasons.append(f"index_close_below_ma{ma_days}")
    if lookback_days > 0 and index >= lookback_days:
        previous = market_bars[index - lookback_days].close
        if previous:
            ret = market_bars[index].close / previous - 1
            if ret < min_return:
                reasons.append(f"index_{lookback_days}d_return={ret:.2%}<min={min_return:.2%}")
    return reasons


def evaluate_regime(
    date_value: dt.date,
    trades: list[ExecutedTrade],
    equity_curve: list[dict[str, Any]],
    initial_cash: float,
    cooldown_until: dt.date | None,
    regime_filter: bool,
    regime_mode: str,
    regime_lookback_trades: int,
    regime_min_trades: int,
    regime_min_win_rate: float,
    regime_max_hard_stop_rate: float,
    regime_max_drawdown: float,
    market_bars: list[PriceBar],
    market_ma_days: int,
    market_lookback_days: int,
    market_min_return: float,
    last_regime_trade_count: int,
) -> tuple[RegimeCheck, dt.date | None]:
    reasons: list[str] = []
    has_new_closed_trades = len(trades) > last_regime_trade_count
    if regime_filter and has_new_closed_trades:
        reasons.extend(
            recent_trade_regime(
                trades,
                regime_lookback_trades,
                regime_min_trades,
                regime_min_win_rate,
                regime_max_hard_stop_rate,
            )
        )
        drawdown = current_drawdown_fraction(equity_curve, initial_cash)
        if drawdown > regime_max_drawdown:
            reasons.append(f"portfolio_drawdown={drawdown:.2%}>max={regime_max_drawdown:.2%}")
    reasons.extend(
        market_regime_reasons(
            market_bars,
            date_value,
            market_ma_days,
            market_lookback_days,
            market_min_return,
        )
    )
    if cooldown_until and date_value <= cooldown_until:
        reasons.append(f"cooldown_until={cooldown_until.isoformat()}")
    state = "risk_off" if reasons else "risk_on"
    action = regime_mode if reasons else "normal"
    if action not in {"skip", "reduce"}:
        action = "normal"
    return RegimeCheck(state=state, action=action, reasons=reasons), cooldown_until


def simulate_portfolio(
    signal_rows: list[PatternRow],
    price_map: dict[str, list[PriceBar]],
    start_date: dt.date,
    end_date: dt.date,
    initial_cash: float,
    max_positions: int,
    min_score: float,
    setup_allow: set[str],
    horizon: int,
    take_profit: float,
    hard_stop: float,
    trailing_stop: float,
    dynamic_exit: bool,
    target_atr_mult: float,
    target_range_mult: float,
    event_bonus: float,
    target_min: float,
    target_max: float,
    stop_atr_mult: float,
    stop_min: float,
    stop_max: float,
    trail_atr_mult: float,
    trail_min: float,
    trail_max: float,
    fee_bps: float,
    allow_add: bool = False,
    max_adds_per_ticker: int = 1,
    add_on_profit: float = 0.04,
    add_size_factor: float = 0.5,
    regime_filter: bool = False,
    regime_mode: str = "skip",
    regime_lookback_trades: int = 12,
    regime_min_trades: int = 8,
    regime_min_win_rate: float = 0.4,
    regime_max_hard_stop_rate: float = 0.5,
    regime_max_drawdown: float = 0.08,
    regime_cooldown_days: int = 5,
    regime_risk_factor: float = 0.35,
    market_bars: list[PriceBar] | None = None,
    market_ma_days: int = 20,
    market_lookback_days: int = 5,
    market_min_return: float = -0.04,
    ma5_mode: str = "ignore",
    ma5_pullback_limit: float = 0.025,
    ma5_extension_limit: float = 0.0,
    sector_mode: str = "ignore",
    min_sector_momentum_5d: float = -0.03,
    min_sector_above_ma20_ratio: float = 0.35,
    execution_model: str = "open",
    max_gap_up: float = 0.04,
    max_gap_down: float = 0.03,
    confirm_buffer: float = 0.003,
    max_entry_extension: float = 0.05,
    intraday_fail_exit: bool = False,
) -> tuple[list[ExecutedTrade], list[dict[str, Any]], float]:
    planned_by_entry: dict[dt.date, list[PlannedTrade]] = {}
    for row in signal_rows:
        if not passes_strategy(
            row,
            min_score,
            setup_allow,
            ma5_mode,
            ma5_pullback_limit,
            ma5_extension_limit,
            sector_mode,
            min_sector_momentum_5d,
            min_sector_above_ma20_ratio,
        ):
            continue
        planned = plan_trade_from_signal(
            row,
            price_map.get(row.ticker, []),
            horizon,
            take_profit,
            hard_stop,
            trailing_stop,
            dynamic_exit,
            target_atr_mult,
            target_range_mult,
            event_bonus,
            target_min,
            target_max,
            stop_atr_mult,
            stop_min,
            stop_max,
            trail_atr_mult,
            trail_min,
            trail_max,
            end_date,
            execution_model,
            max_gap_up,
            max_gap_down,
            confirm_buffer,
            max_entry_extension,
            intraday_fail_exit,
        )
        if planned:
            planned_by_entry.setdefault(planned.entry_date, []).append(planned)

    trading_dates = sorted(
        {
            bar.date
            for bars in price_map.values()
            for bar in bars
            if start_date <= bar.date <= end_date
        }
    )
    cash = initial_cash
    open_positions: list[tuple[PlannedTrade, float]] = []
    trades: list[ExecutedTrade] = []
    equity_curve: list[dict[str, Any]] = []
    fee_rate = fee_bps / 10000
    bar_maps = build_bar_maps(price_map)
    cooldown_until: dt.date | None = None
    last_regime_trade_count = 0
    market_bars = market_bars or []

    for date_value in trading_dates:
        regime, cooldown_until = evaluate_regime(
            date_value,
            trades,
            equity_curve,
            initial_cash,
            cooldown_until,
            regime_filter,
            regime_mode,
            regime_lookback_trades,
            regime_min_trades,
            regime_min_win_rate,
            regime_max_hard_stop_rate,
            regime_max_drawdown,
            market_bars,
            market_ma_days,
            market_lookback_days,
            market_min_return,
            last_regime_trade_count,
        )
        has_fresh_risk = any(not reason.startswith("cooldown_until=") for reason in regime.reasons)
        if regime.state == "risk_off" and regime_filter and regime_cooldown_days > 0 and has_fresh_risk:
            cooldown_until = max(cooldown_until or date_value, date_value + dt.timedelta(days=regime_cooldown_days))
            last_regime_trade_count = len(trades)
        candidates = sorted(planned_by_entry.get(date_value, []), key=lambda item: item.score, reverse=True)
        for planned in candidates:
            if regime.action == "skip":
                break
            if len(open_positions) >= max_positions:
                break
            existing_positions = [position for position in open_positions if position[0].ticker == planned.ticker]
            if existing_positions and not allow_add:
                continue
            if existing_positions and allow_add:
                if len(existing_positions) > max_adds_per_ticker:
                    continue
                current_bar = bar_maps.get(planned.ticker, {}).get(date_value)
                if not current_bar:
                    continue
                best_unrealized = max(
                    current_bar.close / position[0].entry_price - 1
                    for position in existing_positions
                    if position[0].entry_price
                )
                if best_unrealized < add_on_profit:
                    continue
            if cash <= 0:
                break
            slots = max(1, max_positions - len(open_positions))
            capital = cash / slots
            if existing_positions:
                capital *= add_size_factor
            if regime.action == "reduce":
                capital *= max(0.0, min(1.0, regime_risk_factor))
            shares = capital * (1 - fee_rate) / planned.entry_price
            cash -= capital
            open_positions.append((planned, shares))

        still_open: list[tuple[PlannedTrade, float]] = []
        for planned, shares in open_positions:
            if planned.exit_date <= date_value:
                cash += shares * planned.exit_price * (1 - fee_rate)
                trades.append(
                    ExecutedTrade(
                        ticker=planned.ticker,
                        name=planned.name,
                        setup_type=planned.setup_type,
                        signal_date=planned.signal_date.isoformat(),
                        entry_date=planned.entry_date.isoformat(),
                        exit_date=planned.exit_date.isoformat(),
                        entry_price=round(planned.entry_price, 4),
                        exit_price=round(planned.exit_price, 4),
                        return_pct=round(planned.return_pct - 2 * fee_rate * 100, 4),
                        exit_reason=planned.exit_reason,
                        target_pct=round(planned.target_pct * 100, 2),
                        hard_stop_pct=round(planned.hard_stop_pct * 100, 2),
                        trailing_stop_pct=round(planned.trailing_stop_pct * 100, 2),
                        score=planned.score,
                        cash_after=round(cash, 2),
                    )
                )
            else:
                still_open.append((planned, shares))
        open_positions = still_open

        marked = cash
        for planned, shares in open_positions:
            bar = bar_maps.get(planned.ticker, {}).get(date_value)
            marked += shares * (bar.close if bar else planned.entry_price)
        equity_curve.append(
            {
                "date": date_value.isoformat(),
                "equity": round(marked, 2),
                "cash": round(cash, 2),
                "open_positions": len(open_positions),
                "regime_state": regime.state,
                "regime_action": regime.action,
                "regime_reasons": ";".join(regime.reasons),
                "entry_candidates": len(candidates),
            }
        )

    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_cash
    return trades, equity_curve, final_equity


def max_drawdown(equity_curve: list[dict[str, Any]], initial_cash: float) -> float:
    peak = initial_cash
    drawdown = 0.0
    for point in equity_curve:
        equity = float(point["equity"])
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, 1 - equity / peak)
    return drawdown * 100


def write_report(
    out_path: Path,
    csv_path: Path,
    trades: list[ExecutedTrade],
    equity_curve: list[dict[str, Any]],
    final_equity: float,
    initial_cash: float,
    args: argparse.Namespace,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(trades[0]).keys()) if trades else [])
        if trades:
            writer.writeheader()
            for trade in trades:
                writer.writerow(asdict(trade))

    wins = [trade for trade in trades if trade.return_pct > 0]
    total_return = (final_equity / initial_cash - 1) * 100
    avg_trade = sum(trade.return_pct for trade in trades) / len(trades) if trades else 0
    risk_off_days = sum(1 for point in equity_curve if point.get("regime_state") == "risk_off")
    skipped_entry_days = sum(
        1
        for point in equity_curve
        if point.get("regime_action") == "skip" and int(point.get("entry_candidates", 0)) > 0
    )
    reduced_entry_days = sum(
        1
        for point in equity_curve
        if point.get("regime_action") == "reduce" and int(point.get("entry_candidates", 0)) > 0
    )
    lines = [
        f"# Active Short-Term Strategy Backtest - {args.start_date} to {args.end_date}",
        "",
        "This is a simulation using daily OHLC to approximate intraday active exits.",
        "",
        f"- Initial cash: `{initial_cash:,.2f}`",
        f"- Final equity: `{final_equity:,.2f}`",
        f"- Total return: `{total_return:.2f}%`",
        f"- Max drawdown: `{max_drawdown(equity_curve, initial_cash):.2f}%`",
        f"- Trades: `{len(trades)}`",
        f"- Win rate: `{(len(wins) / len(trades) * 100) if trades else 0:.2f}%`",
        f"- Avg trade return: `{avg_trade:.2f}%`",
        "",
        "## Strategy Rules",
        "",
        f"- Min score: `{args.min_score}`",
        f"- Setups: `{args.setups}`",
        f"- Dynamic exit: `{args.dynamic_exit}`",
        f"- Base take profit: `{args.take_profit:.1%}`",
        f"- Base hard stop: `{args.hard_stop:.1%}`",
        f"- Base trailing stop: `{args.trailing_stop:.1%}`",
        f"- Dynamic target bounds: `{args.target_min:.1%}` to `{args.target_max:.1%}`",
        f"- Dynamic stop bounds: `{args.stop_min:.1%}` to `{args.stop_max:.1%}`",
        f"- Max positions: `{args.max_positions}`",
        f"- Allow add: `{args.allow_add}`",
        f"- Add-on profit threshold: `{args.add_on_profit:.1%}`",
        f"- Add size factor: `{args.add_size_factor:.1%}`",
        f"- Regime filter: `{getattr(args, 'regime_filter', False)}`",
        f"- Regime mode: `{getattr(args, 'regime_mode', 'skip')}`",
        f"- Regime lookback trades: `{getattr(args, 'regime_lookback_trades', 12)}`",
        f"- Regime min win rate: `{getattr(args, 'regime_min_win_rate', 0.4):.1%}`",
        f"- Regime max hard-stop rate: `{getattr(args, 'regime_max_hard_stop_rate', 0.5):.1%}`",
        f"- Regime max drawdown: `{getattr(args, 'regime_max_drawdown', 0.08):.1%}`",
        f"- Regime cooldown days: `{getattr(args, 'regime_cooldown_days', 5)}`",
        f"- Regime risk factor: `{getattr(args, 'regime_risk_factor', 0.35):.1%}`",
        f"- Market index yahoo: `{getattr(args, 'market_index_yahoo', '')}`",
        f"- Market MA days: `{getattr(args, 'market_ma_days', 20)}`",
        f"- Market lookback/min return: `{getattr(args, 'market_lookback_days', 5)}` / `{getattr(args, 'market_min_return', -0.04):.1%}`",
        f"- MA5 mode: `{getattr(args, 'ma5_mode', 'ignore')}`",
        f"- MA5 pullback limit: `{getattr(args, 'ma5_pullback_limit', 0.025):.1%}`",
        f"- MA5 extension limit: `{getattr(args, 'ma5_extension_limit', 0.0):.1%}`",
        f"- Sector mode: `{getattr(args, 'sector_mode', 'ignore')}`",
        f"- Min sector 5D momentum: `{getattr(args, 'min_sector_momentum_5d', -0.03):.1%}`",
        f"- Min sector above MA20 ratio: `{getattr(args, 'min_sector_above_ma20_ratio', 0.35):.1%}`",
        f"- Execution model: `{getattr(args, 'execution_model', 'open')}`",
        f"- Max gap up/down: `{getattr(args, 'max_gap_up', 0.04):.1%}` / `{getattr(args, 'max_gap_down', 0.03):.1%}`",
        f"- Confirm buffer: `{getattr(args, 'confirm_buffer', 0.003):.1%}`",
        f"- Max entry extension: `{getattr(args, 'max_entry_extension', 0.05):.1%}`",
        f"- Intraday fail exit: `{getattr(args, 'intraday_fail_exit', False)}`",
        f"- Risk-off days: `{risk_off_days}`",
        f"- Skipped entry days: `{skipped_entry_days}`",
        f"- Reduced entry days: `{reduced_entry_days}`",
        "",
        "## Trades",
        "",
        "| Ticker | Setup | Entry | Exit | Return | Reason | Target | Stop | Trail | Score |",
        "|---|---|---|---|---:|---|---:|---:|---:|---:|",
    ]
    for trade in trades:
        lines.append(
            f"| {trade.ticker} | {trade.setup_type} | {trade.entry_date} @ {trade.entry_price:.2f} | {trade.exit_date} @ {trade.exit_price:.2f} | {trade.return_pct:.2f}% | {trade.exit_reason} | {trade.target_pct:.2f}% | {trade.hard_stop_pct:.2f}% | {trade.trailing_stop_pct:.2f}% | {trade.score:.1f} |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtest active short-term pattern strategy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Example:
              python short_term_strategy_backtest.py --start-date 2026-06-02 --end-date 2026-07-02
            """
        ),
    )
    parser.add_argument("--watchlist", default="config/watchlist.example.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES), help="buyable ticker prefixes; use 'all' to disable")
    parser.add_argument("--events", default="output/tech_event_radar_20260702.json")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--min-score", type=float, default=70.0)
    parser.add_argument("--setups", default="EVENT_PLUS_VOLATILITY,VOLUME_BREAKOUT,HIGH_VOLATILITY")
    parser.add_argument("--horizon", type=int, default=3)
    parser.add_argument("--take-profit", type=float, default=0.10)
    parser.add_argument("--hard-stop", type=float, default=0.04)
    parser.add_argument("--trailing-stop", type=float, default=0.035)
    parser.add_argument("--dynamic-exit", action="store_true")
    parser.add_argument("--target-atr-mult", type=float, default=0.9)
    parser.add_argument("--target-range-mult", type=float, default=0.25)
    parser.add_argument("--event-bonus", type=float, default=0.02)
    parser.add_argument("--target-min", type=float, default=0.06)
    parser.add_argument("--target-max", type=float, default=0.18)
    parser.add_argument("--stop-atr-mult", type=float, default=0.55)
    parser.add_argument("--stop-min", type=float, default=0.025)
    parser.add_argument("--stop-max", type=float, default=0.065)
    parser.add_argument("--trail-atr-mult", type=float, default=0.45)
    parser.add_argument("--trail-min", type=float, default=0.025)
    parser.add_argument("--trail-max", type=float, default=0.055)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--min-traded-value", type=float, default=200_000_000)
    parser.add_argument("--allow-add", action="store_true", help="allow pyramiding only when an existing same-ticker leg is profitable")
    parser.add_argument("--max-adds-per-ticker", type=int, default=1)
    parser.add_argument("--add-on-profit", type=float, default=0.04)
    parser.add_argument("--add-size-factor", type=float, default=0.5)
    parser.add_argument("--regime-filter", action="store_true", help="pause or reduce new entries when recent trades/equity regime turns weak")
    parser.add_argument("--regime-mode", choices=["skip", "reduce"], default="skip")
    parser.add_argument("--regime-lookback-trades", type=int, default=12)
    parser.add_argument("--regime-min-trades", type=int, default=8)
    parser.add_argument("--regime-min-win-rate", type=float, default=0.40)
    parser.add_argument("--regime-max-hard-stop-rate", type=float, default=0.50)
    parser.add_argument("--regime-max-drawdown", type=float, default=0.08)
    parser.add_argument("--regime-cooldown-days", type=int, default=5)
    parser.add_argument("--regime-risk-factor", type=float, default=0.35)
    parser.add_argument("--market-index-yahoo", default="", help="optional market index Yahoo symbol, e.g. 000001.SS or 399006.SZ")
    parser.add_argument("--market-ma-days", type=int, default=20)
    parser.add_argument("--market-lookback-days", type=int, default=5)
    parser.add_argument("--market-min-return", type=float, default=-0.04)
    parser.add_argument("--ma5-mode", choices=["ignore", "filter", "pullback"], default="ignore")
    parser.add_argument("--ma5-pullback-limit", type=float, default=0.025)
    parser.add_argument("--ma5-extension-limit", type=float, default=0.0, help="skip entries if close is this far above MA5; 0 disables")
    parser.add_argument("--sector-mode", choices=["ignore", "filter", "strong"], default="ignore")
    parser.add_argument("--min-sector-momentum-5d", type=float, default=-0.03)
    parser.add_argument("--min-sector-above-ma20-ratio", type=float, default=0.35)
    parser.add_argument("--execution-model", choices=["open", "confirm"], default="open")
    parser.add_argument("--max-gap-up", type=float, default=0.04)
    parser.add_argument("--max-gap-down", type=float, default=0.03)
    parser.add_argument("--confirm-buffer", type=float, default=0.003)
    parser.add_argument("--max-entry-extension", type=float, default=0.05)
    parser.add_argument("--intraday-fail-exit", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--csv-out", default="")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if not start_date or not end_date:
        raise SystemExit("start and end dates must be YYYY-MM-DD")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    event_scores = event_score_by_symbol(Path(args.events))
    fetch_start = start_date - dt.timedelta(days=120)
    fetch_end = end_date + dt.timedelta(days=args.horizon * 3 + 10)
    price_map: dict[str, list[PriceBar]] = {}
    for symbol in symbols:
        try:
            price_map[symbol.ticker] = fetch_yahoo_history(session, symbol.yahoo_symbol or symbol.ticker, fetch_start, fetch_end)
        except Exception:
            price_map[symbol.ticker] = []
        time.sleep(0.05)
    market_bars: list[PriceBar] = []
    if args.market_index_yahoo:
        try:
            market_bars = fetch_yahoo_history(session, args.market_index_yahoo, fetch_start, fetch_end)
        except Exception:
            market_bars = []

    signal_rows = build_signal_rows(
        symbols,
        price_map,
        start_date,
        end_date,
        args.horizon,
        event_scores,
        args.min_traded_value,
        args.take_profit,
        args.hard_stop,
        args.trailing_stop,
    )
    setup_allow = {item.strip() for item in args.setups.split(",") if item.strip()}
    trades, equity_curve, final_equity = simulate_portfolio(
        signal_rows,
        price_map,
        start_date,
        end_date,
        args.initial_cash,
        args.max_positions,
        args.min_score,
        setup_allow,
        args.horizon,
        args.take_profit,
        args.hard_stop,
        args.trailing_stop,
        args.dynamic_exit,
        args.target_atr_mult,
        args.target_range_mult,
        args.event_bonus,
        args.target_min,
        args.target_max,
        args.stop_atr_mult,
        args.stop_min,
        args.stop_max,
        args.trail_atr_mult,
        args.trail_min,
        args.trail_max,
        args.fee_bps,
        args.allow_add,
        args.max_adds_per_ticker,
        args.add_on_profit,
        args.add_size_factor,
        args.regime_filter,
        args.regime_mode,
        args.regime_lookback_trades,
        args.regime_min_trades,
        args.regime_min_win_rate,
        args.regime_max_hard_stop_rate,
        args.regime_max_drawdown,
        args.regime_cooldown_days,
        args.regime_risk_factor,
        market_bars,
        args.market_ma_days,
        args.market_lookback_days,
        args.market_min_return,
        args.ma5_mode,
        args.ma5_pullback_limit,
        args.ma5_extension_limit,
        args.sector_mode,
        args.min_sector_momentum_5d,
        args.min_sector_above_ma20_ratio,
        args.execution_model,
        args.max_gap_up,
        args.max_gap_down,
        args.confirm_buffer,
        args.max_entry_extension,
        args.intraday_fail_exit,
    )
    default_name = f"short_term_strategy_{start_date:%Y%m%d}_{end_date:%Y%m%d}"
    out_path = Path(args.out or f"output/{default_name}.md")
    csv_path = Path(args.csv_out or f"output/{default_name}_trades.csv")
    write_report(out_path, csv_path, trades, equity_curve, final_equity, args.initial_cash, args)
    print(f"trades={len(trades)} final_equity={final_equity:.2f} return={(final_equity / args.initial_cash - 1) * 100:.2f}%")
    print(f"markdown={out_path}")
    print(f"trades_csv={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
