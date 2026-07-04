#!/usr/bin/env python3
"""Backtest and optimize the tech event radar over a short horizon.

The backtest uses only information available on or before each signal date:
event filing/news dates plus historical price bars up to that date. Signals
enter on the next trading day's open and exit by stop, take-profit, or a fixed
three-trading-day time stop.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import json
import math
import textwrap
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dependency_bootstrap import ensure_project_dependencies

ensure_project_dependencies()

import requests

from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols
from tech_event_radar import (
    DEFAULT_HEADERS,
    Event,
    PriceSignal,
    dedupe_events,
    fetch_cninfo_events,
    fetch_rss_events,
    fetch_sec_events,
    load_watchlist,
    parse_date,
    resolve_sec_ciks,
    score_event,
)


@dataclass(frozen=True)
class PriceBar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Signal:
    ticker: str
    market: str
    name: str
    signal_date: dt.date
    score: int
    grade: str
    title: str
    source: str
    url: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Rule:
    min_score: int
    take_profit_pct: float
    stop_loss_pct: float
    max_positions: int
    hold_days: int

    def label(self) -> str:
        return (
            f"score>={self.min_score}, TP={self.take_profit_pct:.1%}, "
            f"SL={self.stop_loss_pct:.1%}, max_pos={self.max_positions}, hold={self.hold_days}d"
        )


@dataclass
class OpenPosition:
    ticker: str
    shares: float
    entry_date: dt.date
    entry_price: float
    signal: Signal
    planned_exit_date: dt.date
    planned_exit_price: float
    planned_exit_reason: str
    planned_holding_bars: int


@dataclass
class Trade:
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_pct: float
    holding_days: int
    exit_reason: str
    score: int
    title: str
    source: str
    url: str


@dataclass
class BacktestResult:
    rule: Rule
    objective_score: float
    total_return_pct: float
    time_weighted_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    trades_count: int
    avg_trade_return_pct: float
    avg_holding_days: float
    exposure_pct: float
    final_equity: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)


def parse_float_list(raw: str) -> list[float]:
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def parse_int_list(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def date_to_unix(date_value: dt.date) -> int:
    moment = dt.datetime.combine(date_value, dt.time.min, tzinfo=dt.timezone.utc)
    return int(moment.timestamp())


def fetch_yahoo_history(
    session: requests.Session,
    yahoo_symbol: str,
    start_date: dt.date,
    end_date: dt.date,
    timeout_seconds: float = 30,
) -> list[PriceBar]:
    period1 = date_to_unix(start_date - dt.timedelta(days=90))
    period2 = date_to_unix(end_date + dt.timedelta(days=10))
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    response = session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    quote = result["indicators"]["quote"][0]
    bars: list[PriceBar] = []
    for index, stamp in enumerate(timestamps):
        try:
            raw_open = quote["open"][index]
            raw_high = quote["high"][index]
            raw_low = quote["low"][index]
            raw_close = quote["close"][index]
            raw_volume = quote["volume"][index]
        except (KeyError, IndexError):
            continue
        if not all(isinstance(value, (int, float)) for value in (raw_open, raw_high, raw_low, raw_close)):
            continue
        bars.append(
            PriceBar(
                date=dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc).date(),
                open=float(raw_open),
                high=float(raw_high),
                low=float(raw_low),
                close=float(raw_close),
                volume=int(raw_volume or 0),
            )
        )
    return bars


def ticker_to_baostock_code(ticker: str) -> str:
    raw = ticker.strip().lower().replace(".ss", "").replace(".sz", "")
    if raw.startswith(("sh.", "sz.")):
        return raw
    if raw.startswith(("sh", "sz")) and len(raw) >= 8:
        return f"{raw[:2]}.{raw[2:8]}"
    if raw.startswith(("6", "9")):
        return f"sh.{raw[:6]}"
    return f"sz.{raw[:6]}"


class BaoStockDailyClient:
    def __init__(self) -> None:
        self._bs = None
        self._logged_in = False

    def __enter__(self) -> "BaoStockDailyClient":
        self.login()
        return self

    def __exit__(self, *_: object) -> None:
        self.logout()

    def login(self) -> None:
        if self._logged_in:
            return
        ensure_project_dependencies()
        import baostock as bs  # type: ignore

        result = bs.login()
        if result.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {result.error_code} {result.error_msg}")
        self._bs = bs
        self._logged_in = True

    def logout(self) -> None:
        if self._logged_in and self._bs is not None:
            self._bs.logout()
        self._logged_in = False

    def fetch_history(self, ticker: str, start_date: dt.date, end_date: dt.date) -> list[PriceBar]:
        self.login()
        code = ticker_to_baostock_code(ticker)
        fields = "date,open,high,low,close,volume"
        result = self._bs.query_history_k_data_plus(  # type: ignore[union-attr]
            code,
            fields,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            frequency="d",
            adjustflag="3",
        )
        if result.error_code != "0":
            raise RuntimeError(f"BaoStock daily query failed for {code}: {result.error_code} {result.error_msg}")
        bars: list[PriceBar] = []
        while result.next():
            row = dict(zip(result.fields, result.get_row_data()))
            try:
                if not row.get("open"):
                    continue
                bars.append(
                    PriceBar(
                        date=dt.date.fromisoformat(row["date"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row.get("volume") or 0)),
                    )
                )
            except (KeyError, ValueError):
                continue
        return bars


def price_signal_as_of(bars: list[PriceBar], signal_date: dt.date) -> PriceSignal | None:
    index = last_bar_index_on_or_before(bars, signal_date)
    if index is None:
        return None
    signal = PriceSignal(symbol="")
    if index < 19:
        signal.warnings.append("not_enough_price_history")
        return signal

    close = bars[index].close
    signal.close = round(close, 4)
    signal.traded_value = round(close * bars[index].volume, 2)
    previous = bars[index - 1].close if index > 0 else None
    if previous:
        signal.change_pct = round((close / previous - 1) * 100, 2)
    signal.ma5 = round(sum(bar.close for bar in bars[index - 4 : index + 1]) / 5, 4)
    signal.ma10 = round(sum(bar.close for bar in bars[index - 9 : index + 1]) / 10, 4)
    signal.ma20 = round(sum(bar.close for bar in bars[index - 19 : index + 1]) / 20, 4)
    previous_volumes = [bar.volume for bar in bars[max(0, index - 20) : index] if bar.volume > 0]
    if previous_volumes:
        signal.volume_ratio = round(bars[index].volume / (sum(previous_volumes) / len(previous_volumes)), 2)
        previous_values = [bar.close * bar.volume for bar in bars[max(0, index - 20) : index] if bar.volume > 0]
        avg_value = sum(previous_values) / len(previous_values) if previous_values else 0
        if avg_value > 0:
            signal.traded_value_ratio = round(signal.traded_value / avg_value, 2)
    if signal.close > signal.ma5:
        signal.confirms.append("close>MA5")
    if signal.close > signal.ma10:
        signal.confirms.append("close>MA10")
    if signal.close > signal.ma20:
        signal.confirms.append("close>MA20")
    if signal.change_pct is not None and signal.change_pct > 0:
        signal.confirms.append("positive_day")
    if signal.volume_ratio is not None and signal.volume_ratio >= 1.5:
        signal.confirms.append("volume_expansion")
    if signal.traded_value_ratio is not None and signal.traded_value_ratio >= 1.5:
        signal.confirms.append("traded_value_expansion")
    if signal.close < signal.ma20:
        signal.warnings.append("below_MA20")
    return signal


def last_bar_index_on_or_before(bars: list[PriceBar], date_value: dt.date) -> int | None:
    result = None
    for index, bar in enumerate(bars):
        if bar.date <= date_value:
            result = index
        else:
            break
    return result


def first_bar_index_after(bars: list[PriceBar], date_value: dt.date) -> int | None:
    for index, bar in enumerate(bars):
        if bar.date > date_value:
            return index
    return None


def bar_by_date(price_map: dict[str, list[PriceBar]], ticker: str, date_value: dt.date) -> PriceBar | None:
    bars = price_map.get(ticker, [])
    index = last_bar_index_on_or_before(bars, date_value)
    if index is None:
        return None
    return bars[index]


def collect_events(
    session: requests.Session,
    watchlist_path: Path,
    start_date: dt.date,
    end_date: dt.date,
    lookback_days: int,
    fetch_sec_docs: bool,
    rss_urls: list[str],
    allowed_prefixes: str,
) -> tuple[list[Any], list[Event]]:
    symbols = filter_symbols(load_watchlist(watchlist_path), allowed_prefixes)
    sec_ciks = resolve_sec_ciks(session, symbols)
    fetch_window = (end_date - start_date).days + lookback_days + 5
    events: list[Event] = []
    for symbol in symbols:
        if symbol.market == "US":
            events.extend(
                fetch_sec_events(
                    session,
                    symbol,
                    sec_ciks.get(symbol.ticker, symbol.cik),
                    end_date,
                    fetch_window,
                    fetch_docs=fetch_sec_docs,
                )
            )
        if symbol.market in {"CN", "SH", "SZ", "BJ"}:
            events.extend(fetch_cninfo_events(session, symbol, end_date, fetch_window))
        symbol_rss = list(symbol.rss_urls) + rss_urls
        events.extend(fetch_rss_events(session, symbol, symbol_rss, end_date, fetch_window, 5))
        time.sleep(0.15)
    return symbols, dedupe_events(events)


def build_price_map(
    session: requests.Session,
    symbols: list[Any],
    start_date: dt.date,
    end_date: dt.date,
) -> dict[str, list[PriceBar]]:
    price_map: dict[str, list[PriceBar]] = {}
    for symbol in symbols:
        yahoo_symbol = symbol.yahoo_symbol or symbol.ticker
        if not yahoo_symbol:
            continue
        try:
            bars = fetch_yahoo_history(session, yahoo_symbol, start_date, end_date)
            price_map[symbol.ticker] = bars
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
            price_map[symbol.ticker] = []
        time.sleep(0.1)
    return price_map


def build_signals(
    events: list[Event],
    price_map: dict[str, list[PriceBar]],
    start_date: dt.date,
    end_date: dt.date,
    lookback_days: int,
) -> list[Signal]:
    signals: list[Signal] = []
    seen: set[tuple[str, str, str]] = set()
    for base_event in events:
        published = parse_date(base_event.published_date)
        if not published:
            continue
        signal_date = max(published, start_date)
        if signal_date < start_date or signal_date > end_date:
            continue
        if published < start_date - dt.timedelta(days=lookback_days):
            continue

        event = copy.deepcopy(base_event)
        event.price = price_signal_as_of(price_map.get(event.ticker, []), signal_date)
        if event.price:
            event.price.symbol = event.ticker
        scored = score_event(event, signal_date)
        has_business_risk = any(
            not flag.startswith("price_") and flag != "data_fetch_failed"
            for flag in scored.risk_flags
        )
        if has_business_risk:
            continue
        key = (scored.ticker, scored.published_date, scored.title)
        if key in seen:
            continue
        seen.add(key)
        signals.append(
            Signal(
                ticker=scored.ticker,
                market=scored.market,
                name=scored.name,
                signal_date=signal_date,
                score=scored.raw_score,
                grade=scored.grade,
                title=scored.title,
                source=scored.source,
                url=scored.url,
                reasons=tuple(scored.reasons),
            )
        )
    signals.sort(key=lambda item: (item.signal_date, item.score), reverse=False)
    return signals


def plan_trade(
    signal: Signal,
    bars: list[PriceBar],
    rule: Rule,
    end_date: dt.date,
) -> tuple[dt.date, float, dt.date, float, str, int] | None:
    entry_index = first_bar_index_after(bars, signal.signal_date)
    if entry_index is None:
        return None
    entry_bar = bars[entry_index]
    if entry_bar.date > end_date:
        return None
    entry_price = entry_bar.open
    stop_price = entry_price * (1 - rule.stop_loss_pct)
    target_price = entry_price * (1 + rule.take_profit_pct)
    final_index = min(entry_index + rule.hold_days - 1, len(bars) - 1)
    exit_bar = bars[final_index]
    exit_price = exit_bar.close
    exit_reason = "time_exit"

    for index in range(entry_index, final_index + 1):
        bar = bars[index]
        if bar.low <= stop_price:
            exit_bar = bar
            exit_price = stop_price
            exit_reason = "stop_loss"
            break
        if bar.high >= target_price:
            exit_bar = bar
            exit_price = target_price
            exit_reason = "take_profit"
            break

    if exit_bar.date > end_date:
        end_bar_index = last_bar_index_on_or_before(bars, end_date)
        if end_bar_index is None or end_bar_index < entry_index:
            return None
        exit_bar = bars[end_bar_index]
        exit_price = exit_bar.close
        exit_reason = "period_end"

    holding_bars = sum(1 for index in range(entry_index, final_index + 1) if bars[index].date <= exit_bar.date)
    return entry_bar.date, entry_price, exit_bar.date, exit_price, exit_reason, max(1, holding_bars)


def simulate_rule(
    rule: Rule,
    signals: list[Signal],
    price_map: dict[str, list[PriceBar]],
    start_date: dt.date,
    end_date: dt.date,
    initial_cash: float,
    fee_bps: float,
    objective: str,
    drawdown_penalty: float,
) -> BacktestResult:
    candidate_signals = [signal for signal in signals if signal.score >= rule.min_score]
    candidate_signals.sort(key=lambda item: (item.signal_date, -item.score))
    entry_signals_by_date: dict[dt.date, list[Signal]] = {}
    planned: dict[tuple[str, dt.date, str], tuple[dt.date, float, dt.date, float, str, int]] = {}
    for signal in candidate_signals:
        plan = plan_trade(signal, price_map.get(signal.ticker, []), rule, end_date)
        if not plan:
            continue
        entry_date = plan[0]
        key = (signal.ticker, signal.signal_date, signal.title)
        planned[key] = plan
        entry_signals_by_date.setdefault(entry_date, []).append(signal)

    trading_dates = sorted(
        {
            bar.date
            for bars in price_map.values()
            for bar in bars
            if start_date <= bar.date <= end_date
        }
    )
    cash = initial_cash
    positions: list[OpenPosition] = []
    trades: list[Trade] = []
    equity_curve: list[dict[str, Any]] = []
    exposure_sum = 0.0
    fee_rate = fee_bps / 10000

    for trade_date in trading_dates:
        equity_at_open = cash + sum(
            position.shares * (bar_by_date(price_map, position.ticker, trade_date).open if bar_by_date(price_map, position.ticker, trade_date) else position.entry_price)
            for position in positions
        )

        todays_signals = sorted(entry_signals_by_date.get(trade_date, []), key=lambda item: item.score, reverse=True)
        for signal in todays_signals:
            if len(positions) >= rule.max_positions:
                break
            if any(position.ticker == signal.ticker for position in positions):
                continue
            key = (signal.ticker, signal.signal_date, signal.title)
            plan = planned.get(key)
            if not plan:
                continue
            entry_date, entry_price, exit_date, exit_price, exit_reason, holding_bars = plan
            if entry_date != trade_date or cash <= 0:
                continue
            open_slots = max(1, rule.max_positions - len(positions))
            target_value = min(cash, equity_at_open / open_slots)
            if target_value <= 0:
                continue
            shares = target_value * (1 - fee_rate) / entry_price
            cash -= target_value
            positions.append(
                OpenPosition(
                    ticker=signal.ticker,
                    shares=shares,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    signal=signal,
                    planned_exit_date=exit_date,
                    planned_exit_price=exit_price,
                    planned_exit_reason=exit_reason,
                    planned_holding_bars=holding_bars,
                )
            )

        remaining: list[OpenPosition] = []
        for position in positions:
            if position.planned_exit_date == trade_date:
                exit_cash = position.shares * position.planned_exit_price * (1 - fee_rate)
                cash += exit_cash
                raw_return = (position.planned_exit_price / position.entry_price - 1) - (2 * fee_rate)
                trades.append(
                    Trade(
                        ticker=position.ticker,
                        entry_date=position.entry_date.isoformat(),
                        exit_date=trade_date.isoformat(),
                        entry_price=round(position.entry_price, 4),
                        exit_price=round(position.planned_exit_price, 4),
                        return_pct=round(raw_return * 100, 4),
                        holding_days=position.planned_holding_bars,
                        exit_reason=position.planned_exit_reason,
                        score=position.signal.score,
                        title=position.signal.title,
                        source=position.signal.source,
                        url=position.signal.url,
                    )
                )
            else:
                remaining.append(position)
        positions = remaining

        close_value = cash
        invested_value = 0.0
        for position in positions:
            bar = bar_by_date(price_map, position.ticker, trade_date)
            mark_price = bar.close if bar else position.entry_price
            value = position.shares * mark_price
            close_value += value
            invested_value += value
        exposure_sum += invested_value / close_value if close_value > 0 else 0
        equity_curve.append(
            {
                "date": trade_date.isoformat(),
                "equity": round(close_value, 2),
                "cash": round(cash, 2),
                "open_positions": len(positions),
                "exposure_pct": round((invested_value / close_value * 100) if close_value else 0, 2),
            }
        )

    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_cash
    total_return = final_equity / initial_cash - 1
    daily_returns = []
    previous_equity = initial_cash
    peak = initial_cash
    max_drawdown = 0.0
    for point in equity_curve:
        equity = float(point["equity"])
        if previous_equity > 0:
            daily_returns.append(equity / previous_equity - 1)
        previous_equity = equity
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, 1 - equity / peak)
    twr = math.prod(1 + value for value in daily_returns) - 1 if daily_returns else 0.0

    winning = [trade for trade in trades if trade.return_pct > 0]
    avg_trade_return = sum(trade.return_pct for trade in trades) / len(trades) if trades else 0.0
    avg_holding_days = sum(trade.holding_days for trade in trades) / len(trades) if trades else 0.0
    exposure_pct = exposure_sum / len(equity_curve) * 100 if equity_curve else 0.0

    if objective == "total_return":
        objective_score = total_return
    elif objective == "return_per_exposure":
        objective_score = total_return / max(exposure_pct / 100, 0.05)
    else:
        time_factor = rule.hold_days / max(avg_holding_days, 1) if trades else 0
        objective_score = twr * time_factor - drawdown_penalty * max_drawdown

    return BacktestResult(
        rule=rule,
        objective_score=objective_score,
        total_return_pct=total_return * 100,
        time_weighted_return_pct=twr * 100,
        max_drawdown_pct=max_drawdown * 100,
        win_rate_pct=(len(winning) / len(trades) * 100) if trades else 0.0,
        trades_count=len(trades),
        avg_trade_return_pct=avg_trade_return,
        avg_holding_days=avg_holding_days,
        exposure_pct=exposure_pct,
        final_equity=final_equity,
        trades=trades,
        equity_curve=equity_curve,
    )


def result_to_json(result: BacktestResult, include_details: bool = True) -> dict[str, Any]:
    data = {
        "rule": asdict(result.rule),
        "rule_label": result.rule.label(),
        "objective_score": result.objective_score,
        "total_return_pct": result.total_return_pct,
        "time_weighted_return_pct": result.time_weighted_return_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
        "win_rate_pct": result.win_rate_pct,
        "trades_count": result.trades_count,
        "avg_trade_return_pct": result.avg_trade_return_pct,
        "avg_holding_days": result.avg_holding_days,
        "exposure_pct": result.exposure_pct,
        "final_equity": result.final_equity,
    }
    if include_details:
        data["trades"] = [asdict(trade) for trade in result.trades]
        data["equity_curve"] = result.equity_curve
    return data


def write_markdown_report(
    path: Path,
    start_date: dt.date,
    end_date: dt.date,
    signals: list[Signal],
    results: list[BacktestResult],
    objective: str,
    initial_cash: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    best = results[0] if results else None
    lines = [
        f"# Tech Event Backtest - {start_date.isoformat()} to {end_date.isoformat()}",
        "",
        "This is a historical simulation, not a prediction or trading recommendation.",
        f"Initial cash: {initial_cash:,.2f}",
        f"Objective: `{objective}`",
        f"Signals scored: {len(signals)}",
        "",
    ]
    if best:
        lines.extend(
            [
                "## Best Rule",
                "",
                f"- Rule: `{best.rule.label()}`",
                f"- Final equity: `{best.final_equity:,.2f}`",
                f"- Total return: `{best.total_return_pct:.2f}%`",
                f"- Time-weighted return: `{best.time_weighted_return_pct:.2f}%`",
                f"- Max drawdown: `{best.max_drawdown_pct:.2f}%`",
                f"- Trades: `{best.trades_count}`",
                f"- Win rate: `{best.win_rate_pct:.2f}%`",
                f"- Avg holding trading days: `{best.avg_holding_days:.2f}`",
                f"- Avg exposure: `{best.exposure_pct:.2f}%`",
                "",
                "## Top Rules",
                "",
                "| Rank | Objective | Rule | Return | TWR | Max DD | Trades | Win | Avg Hold (trading days) | Exposure |",
                "|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for rank, result in enumerate(results[:10], start=1):
            lines.append(
                "| {rank} | {objective:.4f} | {rule} | {ret:.2f}% | {twr:.2f}% | {dd:.2f}% | {trades} | {win:.1f}% | {hold:.2f} | {exposure:.1f}% |".format(
                    rank=rank,
                    objective=result.objective_score,
                    rule=result.rule.label().replace("|", "\\|"),
                    ret=result.total_return_pct,
                    twr=result.time_weighted_return_pct,
                    dd=result.max_drawdown_pct,
                    trades=result.trades_count,
                    win=result.win_rate_pct,
                    hold=result.avg_holding_days,
                    exposure=result.exposure_pct,
                )
            )
        lines.extend(["", "## Best Rule Trades", "", "| Ticker | Entry | Exit | Return | Reason | Score | Title |", "|---|---|---|---:|---|---:|---|"])
        for trade in best.trades:
            lines.append(
                "| {ticker} | {entry} @ {entry_price:.2f} | {exit} @ {exit_price:.2f} | {ret:.2f}% | {reason} | {score} | {title} |".format(
                    ticker=trade.ticker,
                    entry=trade.entry_date,
                    entry_price=trade.entry_price,
                    exit=trade.exit_date,
                    exit_price=trade.exit_price,
                    ret=trade.return_pct,
                    reason=trade.exit_reason,
                    score=trade.score,
                    title=trade.title.replace("|", "\\|")[:100],
                )
            )
    else:
        lines.append("No backtest results were produced.")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Signals enter on the next trading day's open after the event date.",
            "- If stop and take-profit both touch on the same day, the stop is assumed first.",
            "- The optimizer is intentionally small; one month of data can overfit easily.",
            "- Time-weighted objective uses TWR, average holding trading days, and a drawdown penalty.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_trades_csv(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "ticker",
                "entry_date",
                "exit_date",
                "entry_price",
                "exit_price",
                "return_pct",
                "holding_days",
                "exit_reason",
                "score",
                "title",
                "source",
                "url",
            ],
        )
        writer.writeheader()
        for trade in trades:
            writer.writerow(asdict(trade))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtest and optimize the tech event radar short-term rules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python tech_event_backtest.py --end-date 2026-07-02 --days 30
              python tech_event_backtest.py --min-scores 35,45,55 --take-profits 0.03,0.05 --stop-losses 0.02,0.03
            """
        ),
    )
    parser.add_argument("--watchlist", default="config/watchlist.example.csv")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES), help="buyable ticker prefixes; use 'all' to disable")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD; defaults to today")
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD; overrides --days")
    parser.add_argument("--days", type=int, default=30, help="calendar-day backtest window")
    parser.add_argument("--lookback-days", type=int, default=7, help="event lookback on the first day")
    parser.add_argument("--hold-days", type=int, default=3, help="fixed short-term cycle in trading days")
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--fee-bps", type=float, default=5.0, help="one-way trading cost in basis points")
    parser.add_argument("--min-scores", default="35,45,55,65,75,85")
    parser.add_argument("--take-profits", default="0.03,0.05,0.08,0.10")
    parser.add_argument("--stop-losses", default="0.01,0.02,0.03,0.05")
    parser.add_argument("--max-positions", default="1,2,3")
    parser.add_argument(
        "--objective",
        choices=("time_weighted", "total_return", "return_per_exposure"),
        default="time_weighted",
    )
    parser.add_argument("--drawdown-penalty", type=float, default=0.5)
    parser.add_argument("--skip-sec-docs", action="store_true", help="faster, but misses text catalysts")
    parser.add_argument("--rss-url", action="append", default=[])
    parser.add_argument("--out", default="")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--trades-out", default="")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    end_date = parse_date(args.end_date) if args.end_date else dt.date.today()
    if not end_date:
        raise SystemExit("--end-date must be YYYY-MM-DD")
    start_date = parse_date(args.start_date) if args.start_date else end_date - dt.timedelta(days=args.days)
    if not start_date:
        raise SystemExit("--start-date must be YYYY-MM-DD")
    if start_date >= end_date:
        raise SystemExit("start date must be before end date")

    watchlist_path = Path(args.watchlist)
    if not watchlist_path.exists():
        raise SystemExit(f"Watchlist not found: {watchlist_path}")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    symbols, events = collect_events(
        session,
        watchlist_path,
        start_date,
        end_date,
        args.lookback_days,
        fetch_sec_docs=not args.skip_sec_docs,
        rss_urls=args.rss_url,
        allowed_prefixes=args.allowed_prefixes,
    )
    price_map = build_price_map(session, symbols, start_date, end_date)
    signals = build_signals(events, price_map, start_date, end_date, args.lookback_days)

    rules = [
        Rule(
            min_score=min_score,
            take_profit_pct=take_profit,
            stop_loss_pct=stop_loss,
            max_positions=max_positions,
            hold_days=args.hold_days,
        )
        for min_score in parse_int_list(args.min_scores)
        for take_profit in parse_float_list(args.take_profits)
        for stop_loss in parse_float_list(args.stop_losses)
        for max_positions in parse_int_list(args.max_positions)
    ]
    results = [
        simulate_rule(
            rule,
            signals,
            price_map,
            start_date,
            end_date,
            args.initial_cash,
            args.fee_bps,
            args.objective,
            args.drawdown_penalty,
        )
        for rule in rules
    ]
    results.sort(key=lambda result: (result.objective_score, result.total_return_pct, -result.max_drawdown_pct), reverse=True)

    default_name = f"tech_event_backtest_{start_date:%Y%m%d}_{end_date:%Y%m%d}"
    markdown_path = Path(args.out or f"output/{default_name}.md")
    json_path = Path(args.json_out or f"output/{default_name}.json")
    trades_path = Path(args.trades_out or f"output/{default_name}_trades.csv")

    write_markdown_report(markdown_path, start_date, end_date, signals, results, args.objective, args.initial_cash)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "signals": [asdict(signal) for signal in signals],
                "best": result_to_json(results[0], include_details=True) if results else None,
                "top_results": [result_to_json(result, include_details=False) for result in results[:25]],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    if results:
        write_trades_csv(trades_path, results[0].trades)

    print(f"symbols={len(symbols)} events={len(events)} signals={len(signals)} rules={len(rules)}")
    print(f"markdown={markdown_path}")
    print(f"json={json_path}")
    print(f"trades={trades_path}")
    if results:
        best = results[0]
        print(f"best={best.rule.label()}")
        print(
            "return={:.2f}% twr={:.2f}% max_dd={:.2f}% trades={} objective={:.4f}".format(
                best.total_return_pct,
                best.time_weighted_return_pct,
                best.max_drawdown_pct,
                best.trades_count,
                best.objective_score,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
