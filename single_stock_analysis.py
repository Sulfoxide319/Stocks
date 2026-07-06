#!/usr/bin/env python3
"""Single-stock buy/sell diagnostic using the live strategy rules."""

from __future__ import annotations

import csv
import datetime as dt
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import requests

from local_trading_assistant import MONITOR_DEFAULT_ARGS, build_buy_advice, build_sell_advice, parse_float
from market_universe import DEFAULT_BUYABLE_PREFIXES, is_buyable_ticker
from position_sizing import position_sizing_for_signal
from short_term_live_monitor import (
    build_arg_parser as build_monitor_arg_parser,
    dynamic_exit_params,
    edge_score,
    first_manage_pct_from_target,
    historical_hit_rates,
    latest_intraday_state,
    load_hit_rate_calibration,
    resolve_event_scores,
    signal_row_for_latest,
    state_trail_atr_mult,
    strategy_reject_reason,
)
from short_term_strategy_backtest import infer_sector_group
from tech_event_backtest import BaoStockDailyClient, PriceBar, fetch_yahoo_history
from tech_event_radar import DEFAULT_HEADERS, WatchSymbol, load_watchlist
from intraday_vwap_backtest import dynamic_param_overrides
from baostock_intraday import BaoStock5mClient


REJECT_LABELS = {
    "score_below_min": "分数低于当前市场状态最低线",
    "setup_not_allowed": "形态不在允许买入集合内",
    "below_ma20": "未站上20日均线",
    "too_far_above_ma5": "离5日线过远，追高风险偏大",
    "too_far_from_20d_high": "离20日高点过远，短线强度不足",
    "traded_value_below_200m": "成交额低于2亿元，流动性不足",
    "range_too_wide": "5日振幅过大，波动不稳",
    "atr_too_low": "ATR不足，短线弹性不够",
    "momentum10_too_low": "10日动量不足",
    "momentum10_too_high": "10日动量过热",
    "close_position_too_high": "20日区间位置过高",
    "out_of_buy_universe": "不在默认可买前缀内，仅做观察和持仓管理",
    "hot_market": "热市状态暂停新增开仓",
}


def normalize_ticker(value: object) -> str:
    digits = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    return digits[:6]


def ticker_market(ticker: str) -> str:
    if ticker.startswith(("6", "9")):
        return "SH"
    if ticker.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def yahoo_symbol_for_ticker(ticker: str) -> str:
    return f"{ticker}.SS" if ticker.startswith(("6", "9")) else f"{ticker}.SZ"


def xueqiu_symbol_for_ticker(ticker: str) -> str:
    market = ticker_market(ticker)
    return f"{market}{ticker}"


def latest_plan_market_state(root: Path) -> str:
    paths = [
        root / "output" / "trading_assistant" / "latest_plan.json",
        Path.home() / "AppData" / "Local" / "StocksTradingAssistant" / "output" / "trading_assistant" / "latest_plan.json",
    ]
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for group in ("buy", "sell"):
            rows = payload.get(group)
            if not isinstance(rows, list):
                continue
            for item in rows:
                if isinstance(item, dict) and item.get("market_state"):
                    return str(item.get("market_state") or "").strip()
    return ""


def load_symbol(root: Path, ticker: str) -> WatchSymbol:
    watchlists = [
        root / "config" / "watchlist.mainboard_liquid.csv",
        root / "config" / "watchlist.a_share_expanded.csv",
        root / "config" / "watchlist.a_share.csv",
    ]
    for path in watchlists:
        if not path.exists():
            continue
        for symbol in load_watchlist(path):
            if symbol.ticker == ticker:
                return symbol
    return WatchSymbol(
        market=ticker_market(ticker),
        ticker=ticker,
        name="",
        yahoo_symbol=yahoo_symbol_for_ticker(ticker),
        xueqiu_symbol=xueqiu_symbol_for_ticker(ticker),
        cninfo_plate="sh" if ticker_market(ticker) == "SH" else "sz",
    )


def fetch_daily_bars(session: requests.Session, symbol: WatchSymbol, start: dt.date, end: dt.date) -> tuple[list[PriceBar], str, str]:
    yahoo_symbol = symbol.yahoo_symbol or yahoo_symbol_for_ticker(symbol.ticker)
    try:
        bars = fetch_yahoo_history(session, yahoo_symbol, start, end, timeout_seconds=6)
        return [bar for bar in bars if bar.date <= end], "Yahoo", ""
    except Exception as exc:
        yahoo_error = f"{type(exc).__name__}: {exc}"
    try:
        with BaoStockDailyClient() as client:
            bars = client.fetch_history(symbol.ticker, start, end)
        return [bar for bar in bars if bar.date <= end], "BaoStock", yahoo_error
    except Exception as exc:
        raise RuntimeError(f"日线行情不可用：Yahoo {yahoo_error}; BaoStock {type(exc).__name__}: {exc}") from exc


def active_market_filters(args: Any, market_state: str) -> dict[str, float]:
    overrides = dynamic_param_overrides(market_state, args)
    return {
        "min_score": float(overrides.get("min_score", args.min_score)),
        "max_gap_up": float(overrides.get("max_gap_up", args.max_gap_up)),
        "gap_volume_min_ratio": float(overrides.get("gap_volume_min_ratio", args.gap_volume_min_ratio)),
        "max_5d_range_pct": float(overrides.get("max_5d_range_pct", args.max_5d_range_pct)),
        "max_momentum_10d_pct": float(overrides.get("max_momentum_10d_pct", args.max_momentum_10d_pct)),
        "max_close_position_20d_pct": float(overrides.get("max_close_position_20d_pct", args.max_close_position_20d_pct)),
        "min_atr_pct": float(args.normal_min_atr_pct)
        if market_state == "normal"
        else float(args.cold_min_atr_pct)
        if market_state == "cold"
        else 0.0,
        "min_momentum_10d_pct": float(args.cold_min_momentum_10d_pct) if market_state == "cold" else -999.0,
    }


def reject_reasons(row: Any, args: Any, filters: dict[str, float]) -> list[str]:
    reasons: list[str] = []
    if filters["max_5d_range_pct"] > 0 and row.max_5d_range_pct > filters["max_5d_range_pct"]:
        reasons.append("range_too_wide")
    if filters["min_atr_pct"] > 0 and row.atr_pct < filters["min_atr_pct"]:
        reasons.append("atr_too_low")
    if filters["min_momentum_10d_pct"] > -999 and row.momentum_10d_pct < filters["min_momentum_10d_pct"]:
        reasons.append("momentum10_too_low")
    if filters["max_momentum_10d_pct"] < 999 and row.momentum_10d_pct > filters["max_momentum_10d_pct"]:
        reasons.append("momentum10_too_high")
    if filters["max_close_position_20d_pct"] < 100 and row.close_position_20d_pct > filters["max_close_position_20d_pct"]:
        reasons.append("close_position_too_high")
    strategy_reason = strategy_reject_reason(row, filters["min_score"], args.ma5_extension_limit)
    if strategy_reason:
        reasons.append(strategy_reason)
    return list(dict.fromkeys(reasons))


def parse_buy_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return dt.date.today().isoformat()
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return dt.date.today().isoformat()


def analyze_single_stock(
    root: Path,
    ticker_value: object,
    *,
    buy_price: float = 0.0,
    shares: float = 0.0,
    buy_date: str = "",
    target_price: float = 0.0,
    hard_stop_price: float = 0.0,
    trailing_stop_pct: float = 3.0,
    highest_price: float = 0.0,
    market_state_hint: str = "",
) -> dict[str, Any]:
    ticker = normalize_ticker(ticker_value)
    if len(ticker) != 6:
        raise ValueError("股票代码必须是 6 位数字。")

    today = dt.date.today()
    args = build_monitor_arg_parser().parse_args(["--today", today.isoformat(), *MONITOR_DEFAULT_ARGS])
    symbol = load_symbol(root, ticker)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    fetch_start = today - dt.timedelta(days=args.lookback_days)
    bars, price_source, price_warning = fetch_daily_bars(session, symbol, fetch_start, today)
    if len(bars) < 30:
        raise RuntimeError(f"{ticker} 日线历史不足，无法形成短线诊断。")
    latest_bar = bars[-1]

    event_context = resolve_event_scores(str(args.events or ""), today, args.max_event_age_days)
    event_scores = event_context.scores
    sector = infer_sector_group(symbol.notes, symbol.name)
    sector_momentum = (latest_bar.close / bars[-6].close - 1) * 100 if len(bars) >= 6 and bars[-6].close else 0.0
    sector_above = 1.0 if latest_bar.close >= sum(bar.close for bar in bars[-20:]) / min(20, len(bars)) else 0.0
    row = signal_row_for_latest(
        symbol,
        bars,
        event_scores,
        args.min_traded_value,
        sector,
        sector_momentum,
        sector_above,
    )
    if row is None:
        raise RuntimeError(f"{ticker} 有日线数据，但不足以计算当前短线形态。")

    market_state = (market_state_hint or latest_plan_market_state(root) or "normal").strip() or "normal"
    if market_state not in {"normal", "cold", "narrow_rally", "hot"}:
        market_state = "normal"
    filters = active_market_filters(args, market_state)
    blocked_reasons = reject_reasons(row, args, filters)
    buyable = is_buyable_ticker(ticker, DEFAULT_BUYABLE_PREFIXES)
    if not buyable:
        blocked_reasons.append("out_of_buy_universe")
    if args.skip_hot_entries and market_state == "hot":
        blocked_reasons.append("hot_market")

    trail_atr_mult = state_trail_atr_mult(args, market_state)
    target_pct, stop_pct, trail_pct = dynamic_exit_params(row, 0.9, 0.35, 0.45, trail_atr_mult)
    hit_rates = historical_hit_rates(
        market_state,
        load_hit_rate_calibration(Path(args.hit_rate_calibration)),
        setup_type=row.setup_type,
        score=row.score,
        traded_value_ratio=row.traded_value_ratio,
        atr_pct=row.atr_pct,
        momentum_10d_pct=row.momentum_10d_pct,
        sector_group=row.sector_group,
    )
    edge_value = round(edge_score(row, target_pct, stop_pct), 4)
    sizing = position_sizing_for_signal(
        mode=str(args.position_sizing_mode),
        score=row.score,
        setup_type=row.setup_type,
        target_pct=target_pct,
        hard_stop_pct=stop_pct,
        traded_value_ratio=row.traded_value_ratio,
        atr_pct=row.atr_pct,
        momentum_3d_pct=row.momentum_3d_pct,
        momentum_10d_pct=row.momentum_10d_pct,
        distance_to_ma5_pct=row.distance_to_ma5_pct,
        close_position_20d_pct=row.close_position_20d_pct,
        sector_momentum_5d_pct=row.sector_momentum_5d_pct,
        edge_score_value=edge_value,
        first_manage_hit_rate_pct=hit_rates.first_manage_hit_rate_pct,
        target_upper_hit_rate_pct=hit_rates.target_upper_hit_rate_pct,
        target_upper_touch_rate_pct=hit_rates.target_upper_touch_rate_pct,
        hit_rate_sample_size=hit_rates.sample_size,
        max_positions=args.max_positions,
        market_capital_factor=0.0 if market_state == "hot" else 1.0,
        min_factor=args.quality_capital_min_factor,
        max_factor=args.quality_capital_max_factor,
        max_single_position_pct=args.max_single_position_pct,
    )

    now = dt.datetime.now()
    trading_time = now.weekday() < 5 and dt.time(9, 30) <= now.time() <= dt.time(15, 5)
    action = "WATCH_NEXT_SESSION"
    latest_price = 0.0
    vwap = 0.0
    intraday_time = ""
    entry_trigger = row.close * (1 + args.confirm_buffer)
    action_reasons: list[str] = []
    action_risks: list[str] = []
    if not blocked_reasons and row.score < args.buy_min_score:
        action = "WATCH_SCORE_ONLY"
        action_risks.append(f"score={row.score:.1f}<buy_min_score={args.buy_min_score:.1f}")
    elif not blocked_reasons and trading_time:
        with BaoStock5mClient() as client:
            action, latest_price, vwap, intraday_time, entry_trigger, action_reasons, action_risks = latest_intraday_state(
                ticker,
                today,
                row.close,
                client,
                entry_end=dt.time.fromisoformat(str(args.normal_entry_end_time if market_state == "normal" else args.entry_end_time)),
                max_gap_up=filters["max_gap_up"],
                max_gap_down=args.max_gap_down,
                gap_volume_threshold=args.gap_volume_threshold,
                gap_volume_min_ratio=filters["gap_volume_min_ratio"],
                value_ratio=row.traded_value_ratio,
                confirm_buffer=args.confirm_buffer,
                vwap_buffer=args.vwap_buffer,
                max_entry_extension=args.max_entry_extension,
                quote_session=session,
                quote_fallback=args.quote_fallback,
            )
    elif blocked_reasons:
        action = "BLOCKED_BY_FILTERS"

    ref_price = latest_price if latest_price > 0 else row.close
    first_manage_pct = first_manage_pct_from_target(target_pct)
    can_allocate = (
        buyable
        and not blocked_reasons
        and action not in {"DATA_UNAVAILABLE", "QUOTE_ONLY", "NO_NEW_ENTRY", "WATCH_SCORE_ONLY", "BLOCKED_BY_FILTERS"}
        and market_state != "hot"
        and row.score >= args.buy_min_score
    )
    suggested_capital_pct = sizing.suggested_capital_pct if can_allocate else 0.0
    capital_reason = sizing.reason if can_allocate else "不满足买入启用条件，不提示投入资金"
    monitor_row = {
        "ticker": ticker,
        "name": symbol.name,
        "action": action,
        "close": row.close,
        "latest_price": latest_price,
        "entry_trigger": entry_trigger,
        "intraday_vwap": vwap,
        "target_pct": round(target_pct * 100, 2),
        "first_manage_pct": round(first_manage_pct * 100, 2),
        "hard_stop_pct": round(stop_pct * 100, 2),
        "target_upper_hit_rate_pct": "" if hit_rates.target_upper_hit_rate_pct is None else hit_rates.target_upper_hit_rate_pct,
        "target_upper_touch_rate_pct": "" if hit_rates.target_upper_touch_rate_pct is None else hit_rates.target_upper_touch_rate_pct,
        "first_manage_hit_rate_pct": "" if hit_rates.first_manage_hit_rate_pct is None else hit_rates.first_manage_hit_rate_pct,
        "hit_rate_sample_size": hit_rates.sample_size,
        "hit_rate_source": hit_rates.source,
        "hit_rate_bucket": hit_rates.bucket,
        "hit_rate_warning": hit_rates.warning,
        "position_quality_score": sizing.quality_score,
        "position_quality_grade": sizing.quality_grade,
        "capital_factor": sizing.capital_factor,
        "suggested_capital_pct": suggested_capital_pct,
        "capital_reason": capital_reason,
        "score": row.score,
        "edge_score": edge_value,
        "market_state": market_state,
        "risks": ",".join([REJECT_LABELS.get(item, item) for item in blocked_reasons] + action_risks),
    }
    buy_advice = build_buy_advice([monitor_row], "intraday" if trading_time else "opening")[0]

    resolved_target = target_price if target_price > 0 else (buy_price * (1 + target_pct) if buy_price > 0 else ref_price * (1 + target_pct))
    resolved_stop = hard_stop_price if hard_stop_price > 0 else (buy_price * (1 - stop_pct) if buy_price > 0 else ref_price * (1 - stop_pct))
    resolved_highest = highest_price if highest_price > 0 else max(buy_price, ref_price)
    sell_advice = None
    if buy_price > 0:
        position_row = {
            "ticker": ticker,
            "name": symbol.name,
            "buy_date": parse_buy_date(buy_date),
            "buy_price": f"{buy_price:.4f}",
            "shares": f"{shares:.4f}" if shares > 0 else "0",
            "target_price": f"{resolved_target:.4f}",
            "first_manage_price": "",
            "hard_stop_price": f"{resolved_stop:.4f}",
            "trailing_stop_pct": f"{max(0.0, trailing_stop_pct):.4f}",
            "highest_price": f"{resolved_highest:.4f}",
            "management_state": "OPEN",
            "status": "open",
        }
        dummy_path = Path(tempfile.gettempdir()) / "single_stock_position_diagnostic.csv"
        sell_items = build_sell_advice([position_row], today, dummy_path, write_back=False)
        sell_advice = asdict(sell_items[0]) if sell_items else None

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "ticker": ticker,
        "name": symbol.name,
        "xueqiu_symbol": symbol.xueqiu_symbol or xueqiu_symbol_for_ticker(ticker),
        "buyable": buyable,
        "price_source": price_source,
        "price_warning": price_warning,
        "daily_date": latest_bar.date.isoformat(),
        "market_state": market_state,
        "event_status": event_context.status,
        "event_warning": event_context.warning,
        "event_score": int(event_scores.get(ticker, 0)),
        "filters": filters,
        "blocked_reasons": blocked_reasons,
        "blocked_reason_labels": [REJECT_LABELS.get(item, item) for item in blocked_reasons],
        "features": {
            "setup_type": row.setup_type,
            "score": row.score,
            "edge_score": edge_value,
            "quality_score": sizing.quality_score,
            "quality_grade": sizing.quality_grade,
            "traded_value_ratio": row.traded_value_ratio,
            "atr_pct": row.atr_pct,
            "max_5d_range_pct": row.max_5d_range_pct,
            "momentum_3d_pct": row.momentum_3d_pct,
            "momentum_10d_pct": row.momentum_10d_pct,
            "distance_to_ma5_pct": row.distance_to_ma5_pct,
            "distance_to_20d_high_pct": row.distance_to_20d_high_pct,
            "close_position_20d_pct": row.close_position_20d_pct,
            "above_ma20": row.above_ma20,
            "sector_group": row.sector_group,
            "sector_momentum_5d_pct": row.sector_momentum_5d_pct,
        },
        "buy": asdict(buy_advice),
        "sell": sell_advice,
        "reference_plan": {
            "reference_price": round(ref_price, 4),
            "target_price": round(ref_price * (1 + target_pct), 4),
            "first_manage_price": round(ref_price * (1 + first_manage_pct), 4),
            "hard_stop_price": round(ref_price * (1 - stop_pct), 4),
            "trailing_stop_pct": round(trail_pct * 100, 2),
            "target_pct": round(target_pct * 100, 2),
            "first_manage_pct": round(first_manage_pct * 100, 2),
            "hard_stop_pct": round(stop_pct * 100, 2),
        },
        "hit_rates": {
            "target_upper_hit_rate_pct": hit_rates.target_upper_hit_rate_pct,
            "target_upper_touch_rate_pct": hit_rates.target_upper_touch_rate_pct,
            "first_manage_hit_rate_pct": hit_rates.first_manage_hit_rate_pct,
            "sample_size": hit_rates.sample_size,
            "source": hit_rates.source,
            "bucket": hit_rates.bucket,
            "warning": hit_rates.warning,
        },
        "intraday": {
            "action": action,
            "latest_price": round(latest_price, 4),
            "vwap": round(vwap, 4),
            "time": intraday_time,
            "trigger": round(entry_trigger, 4),
            "reasons": action_reasons,
            "risks": action_risks,
        },
    }


def write_single_stock_analysis_csv(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "generated_at": result.get("generated_at", ""),
        "ticker": result.get("ticker", ""),
        "name": result.get("name", ""),
        "buy_action": (result.get("buy") or {}).get("action", ""),
        "sell_action": (result.get("sell") or {}).get("action", ""),
        "latest_price": (result.get("buy") or {}).get("latest_price", ""),
        "trigger_price": (result.get("buy") or {}).get("trigger_price", ""),
        "target_price": (result.get("reference_plan") or {}).get("target_price", ""),
        "first_manage_price": (result.get("reference_plan") or {}).get("first_manage_price", ""),
        "hard_stop_price": (result.get("reference_plan") or {}).get("hard_stop_price", ""),
        "score": (result.get("features") or {}).get("score", ""),
        "quality": (result.get("features") or {}).get("quality_grade", ""),
        "suggested_capital_pct": (result.get("buy") or {}).get("suggested_capital_pct", ""),
        "blocked_reasons": ";".join(result.get("blocked_reason_labels") or []),
    }
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
