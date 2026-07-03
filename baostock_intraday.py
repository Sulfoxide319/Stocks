#!/usr/bin/env python3
"""BaoStock 5-minute data helpers for A-share intraday execution tests."""

from __future__ import annotations

import csv
import datetime as dt
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dependency_bootstrap import ensure_project_dependencies


@dataclass(frozen=True)
class IntradayBar:
    date: dt.date
    time: dt.time
    code: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float

    @property
    def moment(self) -> dt.datetime:
        return dt.datetime.combine(self.date, self.time)


def ticker_to_baostock_code(ticker: str) -> str:
    raw = ticker.strip().lower()
    if raw.startswith(("sh.", "sz.")):
        return raw
    raw = raw.replace(".ss", "").replace(".sz", "")
    if raw.startswith(("6", "9")):
        return f"sh.{raw[:6]}"
    return f"sz.{raw[:6]}"


def parse_baostock_time(raw: str) -> tuple[dt.date, dt.time]:
    # BaoStock returns values like 20260701093500000.
    stamp = raw.strip()
    if len(stamp) >= 12 and stamp[:12].isdigit():
        return (
            dt.date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8])),
            dt.time(int(stamp[8:10]), int(stamp[10:12])),
        )
    raise ValueError(f"unsupported baostock time: {raw!r}")


def cache_path_for(cache_dir: Path, code: str, start_date: dt.date, end_date: dt.date) -> Path:
    return cache_dir / f"{code.replace('.', '')}_{start_date:%Y%m%d}_{end_date:%Y%m%d}_5m.csv"


def cache_dates_from_path(path: Path) -> tuple[dt.date, dt.date] | None:
    parts = path.stem.split("_")
    if len(parts) < 3:
        return None
    try:
        return (
            dt.datetime.strptime(parts[1], "%Y%m%d").date(),
            dt.datetime.strptime(parts[2], "%Y%m%d").date(),
        )
    except ValueError:
        return None


def find_covering_cache(cache_dir: Path, code: str, start_date: dt.date, end_date: dt.date) -> Path | None:
    prefix = code.replace(".", "")
    for path in sorted(cache_dir.glob(f"{prefix}_*_5m.csv")):
        dates = cache_dates_from_path(path)
        if not dates:
            continue
        cached_start, cached_end = dates
        if cached_start <= start_date and cached_end >= end_date:
            return path
    return None


def find_overlapping_caches(cache_dir: Path, code: str, start_date: dt.date, end_date: dt.date) -> list[Path]:
    prefix = code.replace(".", "")
    paths: list[Path] = []
    for path in sorted(cache_dir.glob(f"{prefix}_*_5m.csv")):
        dates = cache_dates_from_path(path)
        if not dates:
            continue
        cached_start, cached_end = dates
        if cached_start <= end_date and cached_end >= start_date:
            paths.append(path)
    return paths


def read_intraday_cache(path: Path) -> list[IntradayBar]:
    if not path.exists():
        return []
    bars: list[IntradayBar] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                bars.append(
                    IntradayBar(
                        date=dt.date.fromisoformat(row["date"]),
                        time=dt.time.fromisoformat(row["clock"]),
                        code=row["code"],
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        amount=float(row["amount"]),
                    )
                )
            except (KeyError, ValueError):
                continue
    return bars


def merge_intraday_bars(bars: Iterable[IntradayBar]) -> list[IntradayBar]:
    merged: dict[tuple[dt.date, dt.time, str], IntradayBar] = {}
    for bar in bars:
        merged[(bar.date, bar.time, bar.code)] = bar
    return sorted(merged.values(), key=lambda item: item.moment)


def iter_weekdays(start_date: dt.date, end_date: dt.date) -> Iterable[dt.date]:
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            yield current
        current += dt.timedelta(days=1)


def compact_date_ranges(dates: Iterable[dt.date], max_calendar_days: int = 45) -> list[tuple[dt.date, dt.date]]:
    sorted_dates = sorted(set(dates))
    if not sorted_dates:
        return []
    ranges: list[tuple[dt.date, dt.date]] = []
    start = previous = sorted_dates[0]
    for date_value in sorted_dates[1:]:
        keeps_calendar_gap = (date_value - previous).days <= 3
        keeps_chunk_small = (date_value - start).days < max_calendar_days
        if keeps_calendar_gap and keeps_chunk_small:
            previous = date_value
            continue
        ranges.append((start, previous))
        start = previous = date_value
    ranges.append((start, previous))
    return ranges


def bars_cover_requested_dates(
    bars: list[IntradayBar],
    start_date: dt.date,
    end_date: dt.date,
    *,
    as_of: dt.date | None = None,
) -> bool:
    as_of = as_of or dt.date.today()
    effective_end = min(end_date, as_of)
    if effective_end < start_date:
        return True
    expected_dates = set(iter_weekdays(start_date, effective_end))
    if not expected_dates:
        return True
    if not bars:
        return False
    dates = {bar.date for bar in bars}
    return expected_dates.issubset(dates)


def missing_requested_dates(
    bars: list[IntradayBar],
    start_date: dt.date,
    end_date: dt.date,
    *,
    as_of: dt.date | None = None,
) -> list[dt.date]:
    as_of = as_of or dt.date.today()
    effective_end = min(end_date, as_of)
    if effective_end < start_date:
        return []
    available = {bar.date for bar in bars}
    return [date_value for date_value in iter_weekdays(start_date, effective_end) if date_value not in available]


def missing_specific_dates(
    bars: list[IntradayBar],
    required_dates: Iterable[dt.date],
    *,
    as_of: dt.date | None = None,
) -> list[dt.date]:
    as_of = as_of or dt.date.today()
    available = {bar.date for bar in bars}
    return sorted({date_value for date_value in required_dates if date_value <= as_of and date_value not in available})


def write_intraday_cache(path: Path, bars: Iterable[IntradayBar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = ["date", "clock", "code", "open", "high", "low", "close", "volume", "amount"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for bar in bars:
            writer.writerow(
                {
                    "date": bar.date.isoformat(),
                    "clock": bar.time.isoformat(timespec="minutes"),
                    "code": bar.code,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "amount": bar.amount,
                }
            )


class BaoStock5mClient:
    def __init__(self, cache_dir: Path = Path("output/baostock_5m_cache"), sleep_seconds: float = 0.15) -> None:
        self.cache_dir = cache_dir
        self.sleep_seconds = sleep_seconds
        self._logged_in = False
        self._bs = None

    def __enter__(self) -> "BaoStock5mClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.logout()

    def login(self) -> None:
        if self._logged_in:
            return
        ensure_project_dependencies()
        import baostock as bs  # type: ignore

        last_error = ""
        for attempt in range(2):
            result = bs.login()
            if result.error_code == "0":
                self._bs = bs
                self._logged_in = True
                return
            last_error = f"{result.error_code} {result.error_msg}"
            if attempt == 0:
                time.sleep(self.sleep_seconds)
        raise RuntimeError(f"BaoStock login failed: {last_error}")

    def logout(self) -> None:
        if self._logged_in and self._bs is not None:
            self._bs.logout()
        self._logged_in = False

    def query_5m_range(self, code: str, start_date: dt.date, end_date: dt.date) -> list[IntradayBar]:
        self.login()
        fields = "date,time,code,open,high,low,close,volume,amount,adjustflag"
        last_error = ""
        for attempt in range(2):
            rs = self._bs.query_history_k_data_plus(  # type: ignore[union-attr]
                code,
                fields,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                frequency="5",
                adjustflag="3",
            )
            if rs.error_code == "0":
                bars: list[IntradayBar] = []
                while rs.next():
                    row = dict(zip(rs.fields, rs.get_row_data()))
                    if not row.get("open") or not row.get("time"):
                        continue
                    bar_date, bar_time = parse_baostock_time(row["time"])
                    bars.append(
                        IntradayBar(
                            date=bar_date,
                            time=bar_time,
                            code=row["code"],
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"] or 0),
                            amount=float(row["amount"] or 0),
                        )
                    )
                time.sleep(self.sleep_seconds)
                return bars

            last_error = f"{rs.error_code} {rs.error_msg}"
            if attempt == 0 and ("未登录" in rs.error_msg or "not login" in rs.error_msg.lower()):
                self.logout()
                self.login()
                continue
            break
        raise RuntimeError(f"BaoStock query failed for {code}: {last_error}")

    def fetch_5m(self, ticker: str, start_date: dt.date, end_date: dt.date) -> list[IntradayBar]:
        code = ticker_to_baostock_code(ticker)
        as_of = dt.date.today()
        effective_end = min(end_date, as_of)
        if effective_end < start_date:
            return []

        cached_bars: list[IntradayBar] = []
        for cache_path in find_overlapping_caches(self.cache_dir, code, start_date, effective_end):
            cached_bars.extend(
                bar
                for bar in read_intraday_cache(cache_path)
                if start_date <= bar.date <= effective_end
            )
        bars = merge_intraday_bars(cached_bars)
        if bars_cover_requested_dates(bars, start_date, effective_end, as_of=as_of):
            return bars

        missing_dates = missing_requested_dates(bars, start_date, effective_end, as_of=as_of)
        for range_start, range_end in compact_date_ranges(missing_dates):
            fetched = self.query_5m_range(code, range_start, range_end)
            write_intraday_cache(cache_path_for(self.cache_dir, code, range_start, range_end), fetched)
            bars = merge_intraday_bars([*bars, *fetched])
        return [bar for bar in bars if start_date <= bar.date <= effective_end]

    def fetch_5m_for_dates(self, ticker: str, required_dates: Iterable[dt.date]) -> list[IntradayBar]:
        code = ticker_to_baostock_code(ticker)
        as_of = dt.date.today()
        dates = sorted({date_value for date_value in required_dates if date_value <= as_of})
        if not dates:
            return []
        start_date = dates[0]
        end_date = dates[-1]

        cached_bars: list[IntradayBar] = []
        for cache_path in find_overlapping_caches(self.cache_dir, code, start_date, end_date):
            cached_bars.extend(
                bar
                for bar in read_intraday_cache(cache_path)
                if start_date <= bar.date <= end_date
            )
        bars = merge_intraday_bars(cached_bars)
        missing_dates = missing_specific_dates(bars, dates, as_of=as_of)
        for range_start, range_end in compact_date_ranges(missing_dates):
            fetched = self.query_5m_range(code, range_start, range_end)
            write_intraday_cache(cache_path_for(self.cache_dir, code, range_start, range_end), fetched)
            bars = merge_intraday_bars([*bars, *fetched])
        required = set(dates)
        return [bar for bar in bars if bar.date in required]


def group_by_date(bars: Iterable[IntradayBar]) -> dict[dt.date, list[IntradayBar]]:
    grouped: dict[dt.date, list[IntradayBar]] = {}
    for bar in sorted(bars, key=lambda item: item.moment):
        grouped.setdefault(bar.date, []).append(bar)
    return grouped


def cumulative_vwap(bars: Iterable[IntradayBar]) -> dict[dt.datetime, float]:
    total_amount = 0.0
    total_volume = 0.0
    values: dict[dt.datetime, float] = {}
    for bar in sorted(bars, key=lambda item: item.moment):
        total_amount += bar.amount
        total_volume += bar.volume
        values[bar.moment] = total_amount / total_volume if total_volume > 0 else bar.close
    return values
