#!/usr/bin/env python3
"""Live-style monitor for 1-3 day A-share short-term candidates."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from dependency_bootstrap import ensure_project_dependencies

ensure_project_dependencies()

from typing import Any

import requests

from baostock_intraday import BaoStock5mClient, IntradayBar
from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from short_term_pattern_miner import (
    PatternRow,
    atr_pct_at,
    event_score_by_symbol,
    feature_score,
    max_range_pct_at,
    moving_average,
    setup_type,
)
from short_term_strategy_backtest import infer_sector_group
from tech_event_backtest import BaoStockDailyClient, PriceBar, fetch_yahoo_history
from tech_event_radar import DEFAULT_HEADERS, load_watchlist, parse_date
from intraday_vwap_backtest import dynamic_param_overrides, market_temperature


@dataclass
class MonitorCandidate:
    ticker: str
    name: str
    signal_date: str
    setup_type: str
    score: float
    edge_score: float
    action: str
    close: float
    latest_price: float
    intraday_vwap: float
    intraday_time: str
    entry_trigger: float
    target_pct: float
    hard_stop_pct: float
    trailing_stop_pct: float
    ma5_distance_pct: float
    value_ratio: float
    momentum_3d_pct: float
    sector_group: str
    sector_momentum_5d_pct: float
    reasons: str = ""
    risks: str = ""


@dataclass(frozen=True)
class EventScoreContext:
    path: Path | None
    scores: dict[str, int]
    status: str
    age_days: int | None
    warning: str = ""


@dataclass(frozen=True)
class RealtimeQuote:
    source: str
    price: float
    timestamp: str


def ticker_to_sina_symbol(ticker: str) -> str:
    raw = ticker.strip().lower().replace(".ss", "").replace(".sz", "")
    if raw.startswith(("sh", "sz")) and len(raw) >= 8:
        return raw[:8]
    if raw.startswith(("6", "9")):
        return f"sh{raw[:6]}"
    return f"sz{raw[:6]}"


def fetch_sina_quote(session: requests.Session, ticker: str) -> RealtimeQuote | None:
    symbol = ticker_to_sina_symbol(ticker)
    try:
        response = session.get(
            "https://hq.sinajs.cn/list=" + symbol,
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=5,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None
    text = response.content.decode("gbk", errors="ignore")
    if '="' not in text:
        return None
    payload = text.split('="', 1)[1].split('";', 1)[0]
    fields = payload.split(",")
    if len(fields) < 32:
        return None
    try:
        price = float(fields[3])
    except ValueError:
        return None
    if price <= 0:
        return None
    timestamp = f"{fields[30]} {fields[31]}".strip()
    return RealtimeQuote(source="sina", price=price, timestamp=timestamp)


def event_file_date(path: Path) -> dt.date | None:
    match = re.fullmatch(r"tech_event_radar_(\d{8})\.json", path.name)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def find_latest_event_file(today: dt.date, output_dir: Path = Path("output")) -> Path | None:
    dated: list[tuple[dt.date, Path]] = []
    for path in output_dir.glob("tech_event_radar_*.json"):
        date_value = event_file_date(path)
        if date_value:
            dated.append((date_value, path))
    if not dated:
        return None
    usable = [item for item in dated if item[0] <= today]
    source = usable or dated
    source.sort(key=lambda item: item[0], reverse=True)
    return source[0][1]


def resolve_event_scores(events_arg: str, today: dt.date, max_event_age_days: int) -> EventScoreContext:
    path = Path(events_arg) if events_arg else Path(f"output/tech_event_radar_{today:%Y%m%d}.json")
    if not path.exists() and not events_arg:
        latest = find_latest_event_file(today)
        if latest:
            path = latest
    if not path.exists():
        return EventScoreContext(None, {}, "missing", None, "event_file_missing")

    file_date = event_file_date(path)
    age_days = (today - file_date).days if file_date else None
    if age_days is not None and age_days > max_event_age_days:
        return EventScoreContext(path, {}, "stale_disabled", age_days, f"event_file_age={age_days}d")

    scores = event_score_by_symbol(path)
    status = "ok" if scores else "empty"
    return EventScoreContext(path, scores, status, age_days)


def dynamic_exit_params(row: PatternRow, target_atr: float, target_range: float, stop_atr: float, trail_atr: float) -> tuple[float, float, float]:
    event_bonus = 0.02 if row.setup_type == "EVENT_PLUS_VOLATILITY" else 0.0
    target = min(0.18, max(0.05, row.atr_pct / 100 * target_atr + row.max_5d_range_pct / 100 * target_range + event_bonus))
    stop = min(0.09, max(0.03, row.atr_pct / 100 * stop_atr))
    trail = min(0.06, max(0.025, row.atr_pct / 100 * trail_atr))
    return target, stop, trail


def signal_row_for_latest(
    symbol: Any,
    bars: list[PriceBar],
    event_scores: dict[str, int],
    min_traded_value: float,
    sector_group: str,
    sector_momentum: float,
    sector_above: float,
) -> PatternRow | None:
    if len(bars) < 30:
        return None
    index = len(bars) - 1
    bar = bars[index]
    closes = [item.close for item in bars]
    ma5 = moving_average(closes, index, 5) or bar.close
    ma20 = moving_average(closes, index, 20) or bar.close
    traded_value = bar.close * bar.volume
    previous_value = bars[index - 1].close * bars[index - 1].volume if index > 0 else traded_value
    values_20 = [item.close * item.volume for item in bars[max(0, index - 20) : index] if item.volume > 0]
    values_3 = [item.close * item.volume for item in bars[max(0, index - 3) : index] if item.volume > 0]
    avg_value = sum(values_20) / len(values_20) if values_20 else traded_value
    avg_value_3 = sum(values_3) / len(values_3) if values_3 else traded_value
    value_ratio = traded_value / avg_value if avg_value else 0.0
    value_ratio_3d = traded_value / avg_value_3 if avg_value_3 else 0.0
    atr_pct = atr_pct_at(bars, index)
    max_5d = max_range_pct_at(bars, index, 5)
    change_1d = (bar.close / bars[index - 1].close - 1) * 100 if index > 0 and bars[index - 1].close else 0.0
    momentum_3d = (bar.close / bars[index - 3].close - 1) * 100 if index >= 3 and bars[index - 3].close else 0.0
    momentum_10d = (bar.close / bars[index - 10].close - 1) * 100 if index >= 10 and bars[index - 10].close else 0.0
    high_20 = max(item.high for item in bars[max(0, index - 19) : index + 1])
    low_20 = min(item.low for item in bars[max(0, index - 19) : index + 1])
    distance_ma5 = (bar.close / ma5 - 1) * 100 if ma5 else 0.0
    distance_high = (bar.close / high_20 - 1) * 100 if high_20 else 0.0
    close_position = (bar.close - low_20) / (high_20 - low_20) * 100 if high_20 > low_20 else 50.0
    event_score = event_scores.get(symbol.ticker, 0)
    setup = setup_type(event_score, value_ratio, max_5d, atr_pct)
    score = feature_score(
        traded_value,
        value_ratio,
        atr_pct,
        max_5d,
        change_1d,
        momentum_3d,
        momentum_10d,
        value_ratio_3d,
        distance_high,
        close_position,
        distance_ma5,
        bar.close > ma5,
        bar.close > ma20,
        event_score,
        min_traded_value,
    )
    return PatternRow(
        ticker=symbol.ticker,
        name=symbol.name,
        market=symbol.market,
        date=bar.date.isoformat(),
        close=round(bar.close, 4),
        score=round(score, 2),
        setup_type=setup,
        traded_value=round(traded_value, 2),
        traded_value_ratio=round(value_ratio, 4),
        atr_pct=round(atr_pct, 4),
        max_5d_range_pct=round(max_5d, 4),
        change_1d_pct=round(change_1d, 4),
        momentum_3d_pct=round(momentum_3d, 4),
        momentum_10d_pct=round(momentum_10d, 4),
        value_ratio_3d=round(value_ratio_3d, 4),
        distance_to_ma5_pct=round(distance_ma5, 4),
        distance_to_20d_high_pct=round(distance_high, 4),
        close_position_20d_pct=round(close_position, 4),
        above_ma5=bar.close > ma5,
        above_ma20=bar.close > ma20,
        future_max_return_pct=0.0,
        hit_10pct=False,
        simulated_return_pct=0.0,
        exit_reason="monitor",
        sector_group=sector_group,
        sector_momentum_5d_pct=round(sector_momentum, 4),
        sector_above_ma20_ratio=round(sector_above, 4),
    )


def latest_intraday_state(
    ticker: str,
    trade_date: dt.date,
    signal_close: float,
    client: BaoStock5mClient,
    entry_end: dt.time,
    max_gap_up: float,
    max_gap_down: float,
    gap_volume_threshold: float,
    gap_volume_min_ratio: float,
    value_ratio: float,
    confirm_buffer: float,
    max_entry_extension: float,
    quote_session: requests.Session | None = None,
    quote_fallback: str = "sina",
) -> tuple[str, float, float, str, float, list[str], list[str]]:
    bars = client.fetch_5m(ticker, trade_date, trade_date)
    bars = [bar for bar in bars if dt.time(9, 30) <= bar.time <= dt.time(15, 0)]
    if not bars:
        trigger = signal_close * (1 + confirm_buffer)
        if quote_fallback == "sina" and quote_session is not None and trade_date == dt.date.today():
            quote = fetch_sina_quote(quote_session, ticker)
            if quote:
                return "QUOTE_ONLY", quote.price, 0.0, quote.timestamp, trigger, ["quote_only"], ["no_intraday_bars", "no_vwap", f"quote_source={quote.source}"]
        return "DATA_UNAVAILABLE", 0.0, 0.0, "", trigger, [], ["no_intraday_bars"]
    first = bars[0]
    latest = bars[-1]
    gap = first.open / signal_close - 1 if signal_close else 0.0
    if gap > max_gap_up:
        return "SKIP_GAP_UP", latest.close, latest.close, latest.time.isoformat(timespec="minutes"), signal_close * (1 + confirm_buffer), [], [f"gap_up={gap:.2%}"]
    if gap < -max_gap_down:
        return "SKIP_GAP_DOWN", latest.close, latest.close, latest.time.isoformat(timespec="minutes"), signal_close * (1 + confirm_buffer), [], [f"gap_down={gap:.2%}"]
    if gap_volume_min_ratio > 0 and gap > gap_volume_threshold and value_ratio < gap_volume_min_ratio:
        return "SKIP_WEAK_GAP", latest.close, latest.close, latest.time.isoformat(timespec="minutes"), signal_close * (1 + confirm_buffer), [], [f"gap={gap:.2%}", f"value_ratio={value_ratio:.2f}<{gap_volume_min_ratio:.2f}"]
    amount = sum(bar.amount for bar in bars)
    volume = sum(bar.volume for bar in bars)
    vwap = amount / volume if volume > 0 else latest.close
    trigger = max(signal_close * (1 + confirm_buffer), vwap)
    reasons: list[str] = []
    risks: list[str] = []
    if latest.time < dt.time(9, 45):
        return "WAIT_0945", latest.close, vwap, latest.time.isoformat(timespec="minutes"), trigger, reasons, ["before_entry_window"]
    if latest.time > entry_end:
        risks.append("after_entry_window")
    if latest.close / signal_close - 1 > max_entry_extension:
        risks.append("too_extended_from_signal")
    if latest.close >= trigger:
        reasons.append("above_trigger")
    if latest.close >= vwap:
        reasons.append("above_vwap")
    action = "BUY_TRIGGER" if reasons and not risks else "WATCH"
    if risks and "after_entry_window" in risks:
        action = "NO_NEW_ENTRY"
    return action, latest.close, vwap, latest.time.isoformat(timespec="minutes"), trigger, reasons, risks


def edge_score(row: PatternRow, target_pct: float, stop_pct: float) -> float:
    probability = 0.35 + max(0.0, row.score - 85) * 0.006
    if row.traded_value_ratio >= 1.5:
        probability += 0.04
    if 2 <= row.momentum_3d_pct <= 12:
        probability += 0.03
    if row.distance_to_ma5_pct > 4:
        probability -= 0.08
    if row.distance_to_ma5_pct < -3:
        probability -= 0.05
    probability = min(0.68, max(0.22, probability))
    return (probability * target_pct - (1 - probability) * stop_pct) * 100


def strategy_reject_reason(row: PatternRow, min_score: float, ma5_extension_limit: float) -> str:
    allowed_setups = {"EVENT_PLUS_VOLATILITY", "VOLUME_BREAKOUT", "HIGH_VOLATILITY"}
    if row.score < min_score:
        return "score_below_min"
    if row.setup_type not in allowed_setups:
        return "setup_not_allowed"
    if not row.above_ma20:
        return "below_ma20"
    if ma5_extension_limit > 0 and row.distance_to_ma5_pct > ma5_extension_limit * 100:
        return "too_far_above_ma5"
    if row.distance_to_20d_high_pct < -12:
        return "too_far_from_20d_high"
    if row.traded_value < 200_000_000:
        return "traded_value_below_200m"
    return ""


def monitor_progress(message: str) -> None:
    print(f"MONITOR_PROGRESS|{message}", flush=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor high expected-upside A-share short-term candidates.")
    parser.add_argument("--watchlist", default="config/watchlist.mainboard_liquid.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES))
    parser.add_argument("--events", default="", help="event score JSON; defaults to today's tech_event_radar_YYYYMMDD.json, then latest dated file")
    parser.add_argument("--max-event-age-days", type=int, default=1, help="disable event scores when the dated event file is older than this many days")
    parser.add_argument("--quote-fallback", choices=["sina", "none"], default="sina", help="quote-only fallback when BaoStock 5m bars are unavailable")
    parser.add_argument("--today", default="")
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--history-timeout", type=float, default=8.0, help="seconds per Yahoo history request")
    parser.add_argument("--min-score", type=float, default=90)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--min-traded-value", type=float, default=200_000_000)
    parser.add_argument("--ma5-extension-limit", type=float, default=0.04)
    parser.add_argument("--entry-end-time", default="11:15")
    parser.add_argument("--max-gap-up", type=float, default=0.03)
    parser.add_argument("--max-gap-down", type=float, default=0.03)
    parser.add_argument("--gap-volume-threshold", type=float, default=0.0)
    parser.add_argument("--gap-volume-min-ratio", type=float, default=0.0)
    parser.add_argument("--confirm-buffer", type=float, default=0.0)
    parser.add_argument("--max-entry-extension", type=float, default=0.04)
    parser.add_argument("--max-5d-range-pct", type=float, default=0.0)
    parser.add_argument("--max-momentum-10d-pct", type=float, default=999.0)
    parser.add_argument("--max-close-position-20d-pct", type=float, default=100.0)
    parser.add_argument("--dynamic-params", action="store_true")
    parser.add_argument("--hot-max-gap-up", type=float, default=0.02)
    parser.add_argument("--hot-gap-volume-min-ratio", type=float, default=1.3)
    parser.add_argument("--hot-max-5d-range-pct", type=float, default=32.0)
    parser.add_argument("--hot-max-momentum-10d-pct", type=float, default=26.0)
    parser.add_argument("--hot-max-close-position-20d-pct", type=float, default=85.0)
    parser.add_argument("--normal-max-gap-up", type=float, default=0.02)
    parser.add_argument("--normal-gap-volume-min-ratio", type=float, default=1.3)
    parser.add_argument("--normal-max-5d-range-pct", type=float, default=32.0)
    parser.add_argument("--normal-max-momentum-10d-pct", type=float, default=26.0)
    parser.add_argument("--normal-max-close-position-20d-pct", type=float, default=85.0)
    parser.add_argument("--cold-max-gap-up", type=float, default=-1.0)
    parser.add_argument("--cold-gap-volume-min-ratio", type=float, default=99.0)
    parser.add_argument("--cold-max-5d-range-pct", type=float, default=1.0)
    parser.add_argument("--cold-max-momentum-10d-pct", type=float, default=1.0)
    parser.add_argument("--cold-max-close-position-20d-pct", type=float, default=1.0)
    parser.add_argument("--mode", choices=["daily", "intraday"], default="intraday")
    parser.add_argument("--out", default="")
    parser.add_argument("--csv-out", default="")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    today = parse_date(args.today) if args.today else dt.date.today()
    if not today:
        raise SystemExit("--today must be YYYY-MM-DD")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    monitor_progress("读取股票池")
    symbols = filter_symbols(load_watchlist(Path(args.watchlist)), args.allowed_prefixes)
    monitor_progress(f"股票池读取完成：{len(symbols)} 只")
    monitor_progress("读取事件评分")
    event_context = resolve_event_scores(args.events, today, args.max_event_age_days)
    event_scores = event_context.scores
    fetch_start = today - dt.timedelta(days=args.lookback_days)
    price_map: dict[str, list[PriceBar]] = {}
    total_symbols = len(symbols)
    yahoo_disabled = False
    yahoo_forbidden_streak = 0
    yahoo_error_streak = 0
    baostock_daily_client: BaoStockDailyClient | None = None
    def fetch_baostock_daily_for_symbol(ticker: str) -> list[PriceBar]:
        nonlocal baostock_daily_client
        if baostock_daily_client is None:
            baostock_daily_client = BaoStockDailyClient()
            baostock_daily_client.login()
        return baostock_daily_client.fetch_history(ticker, fetch_start, today)

    try:
        for index, symbol in enumerate(symbols, start=1):
            label = f"{symbol.ticker} {symbol.name}".strip()
            monitor_progress(f"历史行情 {index}/{total_symbols}：{label}")
            started = time.monotonic()
            source = "Yahoo"
            try:
                if yahoo_disabled:
                    raise RuntimeError("Yahoo disabled after repeated errors")
                bars = fetch_yahoo_history(
                    session,
                    symbol.yahoo_symbol or symbol.ticker,
                    fetch_start,
                    today,
                    timeout_seconds=args.history_timeout,
                )
                yahoo_forbidden_streak = 0
                yahoo_error_streak = 0
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                yahoo_error_streak += 1
                if status_code == 403:
                    yahoo_forbidden_streak += 1
                    if yahoo_forbidden_streak >= 3 and not yahoo_disabled:
                        yahoo_disabled = True
                        monitor_progress("Yahoo 日线连续 403，切换到 BaoStock 日线兜底")
                if yahoo_error_streak >= 3 and not yahoo_disabled:
                    yahoo_disabled = True
                    monitor_progress(f"Yahoo 日线连续 {yahoo_error_streak} 次 HTTP/网络失败，切换到 BaoStock 日线兜底")
                source = "BaoStock"
                try:
                    bars = fetch_baostock_daily_for_symbol(symbol.ticker)
                except Exception as fallback_exc:
                    price_map[symbol.ticker] = []
                    monitor_progress(f"历史行情失败：{label} - Yahoo HTTP {status_code}; BaoStock {type(fallback_exc).__name__}: {fallback_exc}")
                    time.sleep(0.02)
                    continue
            except Exception as exc:
                yahoo_error_streak += 1
                if yahoo_error_streak >= 3 and not yahoo_disabled:
                    yahoo_disabled = True
                    monitor_progress(f"Yahoo 日线连续 {yahoo_error_streak} 次网络失败，切换到 BaoStock 日线兜底")
                source = "BaoStock"
                try:
                    bars = fetch_baostock_daily_for_symbol(symbol.ticker)
                except Exception as fallback_exc:
                    price_map[symbol.ticker] = []
                    monitor_progress(f"历史行情失败：{label} - Yahoo {type(exc).__name__}: {exc}; BaoStock {type(fallback_exc).__name__}: {fallback_exc}")
                    time.sleep(0.02)
                    continue
            price_map[symbol.ticker] = [bar for bar in bars if bar.date <= today]
            elapsed = time.monotonic() - started
            if source != "Yahoo":
                monitor_progress(f"历史行情兜底：{label} 使用 {source}，{len(price_map[symbol.ticker])} 条")
            if elapsed >= 2:
                monitor_progress(f"历史行情偏慢：{label} 用时 {elapsed:.1f} 秒")
            time.sleep(0.02)
    finally:
        if baostock_daily_client is not None:
            baostock_daily_client.logout()

    monitor_progress("计算板块和市场温度")
    sector_by_ticker = {symbol.ticker: infer_sector_group(getattr(symbol, "notes", ""), getattr(symbol, "name", "")) for symbol in symbols}
    sector_values: dict[str, list[tuple[float, bool]]] = {}
    for symbol in symbols:
        bars = price_map.get(symbol.ticker, [])
        if len(bars) < 21:
            continue
        closes = [bar.close for bar in bars]
        latest = bars[-1]
        previous = bars[-6].close if len(bars) >= 6 else latest.close
        momentum = (latest.close / previous - 1) * 100 if previous else 0.0
        ma20 = sum(closes[-20:]) / 20
        sector_values.setdefault(sector_by_ticker.get(symbol.ticker, "other_tech"), []).append((momentum, latest.close > ma20))
    sector_context: dict[str, tuple[float, float]] = {}
    for sector, values in sector_values.items():
        momentums = sorted(item[0] for item in values)
        middle = len(momentums) // 2
        median = momentums[middle] if len(momentums) % 2 else (momentums[middle - 1] + momentums[middle]) / 2
        above = sum(1 for _, is_above in values if is_above) / len(values)
        sector_context[sector] = (median, above)

    temperature = market_temperature(price_map, today)
    overrides = dynamic_param_overrides(str(temperature["state"]), args)
    active_max_gap_up = float(overrides.get("max_gap_up", args.max_gap_up))
    active_gap_volume_min_ratio = float(overrides.get("gap_volume_min_ratio", args.gap_volume_min_ratio))
    active_max_5d_range = float(overrides.get("max_5d_range_pct", args.max_5d_range_pct))
    active_max_momentum_10d = float(overrides.get("max_momentum_10d_pct", args.max_momentum_10d_pct))
    active_max_close_position = float(overrides.get("max_close_position_20d_pct", args.max_close_position_20d_pct))

    candidates: list[MonitorCandidate] = []
    filter_counts: dict[str, int] = {
        "universe": total_symbols,
        "no_signal_row": 0,
        "range_too_wide": 0,
        "momentum10_too_high": 0,
        "close_position_too_high": 0,
        "score_below_min": 0,
        "setup_not_allowed": 0,
        "below_ma20": 0,
        "too_far_above_ma5": 0,
        "too_far_from_20d_high": 0,
        "traded_value_below_200m": 0,
        "passed_daily_filters": 0,
    }
    entry_hour, entry_minute = (int(part) for part in args.entry_end_time.split(":", 1))
    entry_end = dt.time(entry_hour, entry_minute)
    monitor_progress("筛选候选股并检查盘中数据")
    with BaoStock5mClient() as client:
        for index, symbol in enumerate(symbols, start=1):
            if index == 1 or index % 10 == 0:
                monitor_progress(f"候选过滤 {index}/{total_symbols}：{symbol.ticker} {symbol.name}")
            bars = price_map.get(symbol.ticker, [])
            sector = sector_by_ticker.get(symbol.ticker, "other_tech")
            sector_momentum, sector_above = sector_context.get(sector, (0.0, 0.0))
            row = signal_row_for_latest(symbol, bars, event_scores, args.min_traded_value, sector, sector_momentum, sector_above)
            if not row:
                filter_counts["no_signal_row"] += 1
                continue
            if active_max_5d_range > 0 and row.max_5d_range_pct > active_max_5d_range:
                filter_counts["range_too_wide"] += 1
                continue
            if active_max_momentum_10d < 999 and row.momentum_10d_pct > active_max_momentum_10d:
                filter_counts["momentum10_too_high"] += 1
                continue
            if active_max_close_position < 100 and row.close_position_20d_pct > active_max_close_position:
                filter_counts["close_position_too_high"] += 1
                continue
            strategy_reason = strategy_reject_reason(row, args.min_score, args.ma5_extension_limit)
            if strategy_reason:
                filter_counts[strategy_reason] += 1
                continue
            filter_counts["passed_daily_filters"] += 1
            target, stop, trail = dynamic_exit_params(row, 0.9, 0.35, 0.7, 0.25)
            action = "WATCH_NEXT_SESSION"
            latest_price = 0.0
            vwap = 0.0
            intraday_time = ""
            trigger = row.close * (1 + args.confirm_buffer)
            reasons: list[str] = []
            risks: list[str] = []
            if args.mode == "intraday":
                monitor_progress(f"盘中数据：{row.ticker} {row.name}")
                action, latest_price, vwap, intraday_time, trigger, reasons, risks = latest_intraday_state(
                    row.ticker,
                    today,
                    row.close,
                    client,
                    entry_end,
                    active_max_gap_up,
                    args.max_gap_down,
                    args.gap_volume_threshold,
                    active_gap_volume_min_ratio,
                    row.traded_value_ratio,
                    args.confirm_buffer,
                    args.max_entry_extension,
                    session,
                    args.quote_fallback,
                )
            has_intraday_data = action != "DATA_UNAVAILABLE"
            candidates.append(
                MonitorCandidate(
                    ticker=row.ticker,
                    name=row.name,
                    signal_date=row.date,
                    setup_type=row.setup_type,
                    score=row.score,
                    edge_score=round(edge_score(row, target, stop), 4) if has_intraday_data else 0.0,
                    action=action,
                    close=row.close,
                    latest_price=round(latest_price, 4),
                    intraday_vwap=round(vwap, 4),
                    intraday_time=intraday_time,
                    entry_trigger=round(trigger, 4),
                    target_pct=round(target * 100, 2) if has_intraday_data else 0.0,
                    hard_stop_pct=round(stop * 100, 2) if has_intraday_data else 0.0,
                    trailing_stop_pct=round(trail * 100, 2) if has_intraday_data else 0.0,
                    ma5_distance_pct=row.distance_to_ma5_pct,
                    value_ratio=row.traded_value_ratio,
                    momentum_3d_pct=row.momentum_3d_pct,
                    sector_group=row.sector_group,
                    sector_momentum_5d_pct=row.sector_momentum_5d_pct,
                    reasons=",".join(reasons),
                    risks=",".join(risks),
                )
            )
    monitor_progress(f"写入候选结果：{len(candidates)} 条")
    funnel_parts = [
        f"股票池 {filter_counts['universe']}",
        f"无有效形态/历史不足 {filter_counts['no_signal_row']}",
        f"5日振幅过大 {filter_counts['range_too_wide']}",
        f"10日涨幅过热 {filter_counts['momentum10_too_high']}",
        f"20日位置过高 {filter_counts['close_position_too_high']}",
        f"分数不足 {filter_counts['score_below_min']}",
        f"形态不允许 {filter_counts['setup_not_allowed']}",
        f"未站上20日线 {filter_counts['below_ma20']}",
        f"离MA5过远 {filter_counts['too_far_above_ma5']}",
        f"离20日高点过远 {filter_counts['too_far_from_20d_high']}",
        f"成交额不足 {filter_counts['traded_value_below_200m']}",
        f"通过日线过滤 {filter_counts['passed_daily_filters']}",
        f"最终候选 {len(candidates)}",
    ]
    monitor_progress("筛选漏斗：" + "；".join(funnel_parts))
    action_rank = {
        "BUY_TRIGGER": 4,
        "WATCH": 3,
        "WAIT_0945": 2,
        "WATCH_NEXT_SESSION": 2,
        "NO_NEW_ENTRY": 1,
        "QUOTE_ONLY": 0,
        "DATA_UNAVAILABLE": 0,
    }
    candidates.sort(key=lambda item: (action_rank.get(item.action, 1), item.edge_score, item.score), reverse=True)
    candidates = candidates[: args.top]
    data_unavailable = args.mode == "intraday" and candidates and all(item.action == "DATA_UNAVAILABLE" for item in candidates)
    quote_only = args.mode == "intraday" and candidates and any(item.action == "QUOTE_ONLY" for item in candidates)
    intraday_status = (
        "not_applicable"
        if args.mode != "intraday"
        else "unavailable"
        if data_unavailable
        else "partial_quote_only"
        if quote_only
        else "ok"
    )
    default_name = f"short_term_live_monitor_{today:%Y%m%d}"
    out_path = Path(args.out or f"output/{default_name}.md")
    csv_path = Path(args.csv_out or f"output/{default_name}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(candidates[0]).keys()) if candidates else [])
        if candidates:
            writer.writeheader()
            for candidate in candidates:
                writer.writerow(asdict(candidate))
    lines = [
        f"# Short-Term Live Monitor - {today.isoformat()}",
        "",
        f"- Universe: `{args.watchlist}`",
        f"- Candidates: `{len(candidates)}`",
        f"- Market state: `{temperature['state']}` breadth_ma20=`{temperature['breadth_ma20']:.2%}` avg5d=`{temperature['avg_5d_return']:.2%}`",
        f"- Event score source: `{event_context.path or '-'}` status=`{event_context.status}` age_days=`{event_context.age_days if event_context.age_days is not None else '-'}`",
        f"- Intraday data status: `{intraday_status}`",
        f"- Quote fallback: `{args.quote_fallback}`",
        f"- Entry window end: `{args.entry_end_time}`",
        f"- Active filters: gap_up<=`{active_max_gap_up:.1%}`, value_ratio>=`{active_gap_volume_min_ratio:.2f}`, range5<=`{active_max_5d_range:.1f}`, momentum10<=`{active_max_momentum_10d:.1f}`, pos20<=`{active_max_close_position:.1f}`",
        "",
        "## Filter Funnel",
        "",
        "| Step | Count |",
        "|---|---:|",
        f"| Stock pool | {filter_counts['universe']} |",
        f"| No signal row / insufficient history | {filter_counts['no_signal_row']} |",
        f"| 5-day range too wide | {filter_counts['range_too_wide']} |",
        f"| 10-day momentum too high | {filter_counts['momentum10_too_high']} |",
        f"| 20-day close position too high | {filter_counts['close_position_too_high']} |",
        f"| Score below min | {filter_counts['score_below_min']} |",
        f"| Setup not allowed | {filter_counts['setup_not_allowed']} |",
        f"| Below MA20 | {filter_counts['below_ma20']} |",
        f"| Too far above MA5 | {filter_counts['too_far_above_ma5']} |",
        f"| Too far from 20-day high | {filter_counts['too_far_from_20d_high']} |",
        f"| Traded value below 200m | {filter_counts['traded_value_below_200m']} |",
        f"| Passed daily filters | {filter_counts['passed_daily_filters']} |",
        f"| Final candidates | {len(candidates)} |",
        "",
        "## Candidates",
        "",
    ]
    if data_unavailable:
        lines.extend(
            [
                "> Intraday 5-minute bars are unavailable for all selected candidates. Buy-side ranking, target, and stop guidance are disabled for this scan.",
                "",
            ]
        )
    elif quote_only:
        lines.extend(
            [
                "> Some candidates only have real-time quote fallback. Quote-only rows cannot confirm VWAP or trigger BUY_NOW; displayed trigger/target/stop prices are reference-only.",
                "",
            ]
        )
    lines.extend(
        [
            "| Action | Ticker | Name | Edge | Score | Trigger | Latest | VWAP | Target | Stop | MA5 Dist | Reasons | Risks |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for item in candidates:
        lines.append(
            f"| {item.action} | {item.ticker} | {item.name} | {item.edge_score:.2f} | {item.score:.1f} | {item.entry_trigger:.2f} | {item.latest_price:.2f} | {item.intraday_vwap:.2f} | {item.target_pct:.2f}% | {item.hard_stop_pct:.2f}% | {item.ma5_distance_pct:.2f}% | {item.reasons or '-'} | {item.risks or '-'} |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"candidates={len(candidates)} markdown={out_path} csv={csv_path}")
    for item in candidates[:10]:
        print(asdict(item))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
