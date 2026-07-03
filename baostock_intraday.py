#!/usr/bin/env python3
"""BaoStock 5-minute data helpers for A-share intraday execution tests."""

from __future__ import annotations

import csv
import datetime as dt
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


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


def find_covering_cache(cache_dir: Path, code: str, start_date: dt.date, end_date: dt.date) -> Path | None:
    prefix = code.replace(".", "")
    for path in sorted(cache_dir.glob(f"{prefix}_*_5m.csv")):
        parts = path.stem.split("_")
        if len(parts) < 3:
            continue
        try:
            cached_start = dt.datetime.strptime(parts[1], "%Y%m%d").date()
            cached_end = dt.datetime.strptime(parts[2], "%Y%m%d").date()
        except ValueError:
            continue
        if cached_start <= start_date and cached_end >= end_date:
            return path
    return None


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
        self.login()
        return self

    def __exit__(self, *_: object) -> None:
        self.logout()

    def login(self) -> None:
        if self._logged_in:
            return
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

    def fetch_5m(self, ticker: str, start_date: dt.date, end_date: dt.date) -> list[IntradayBar]:
        code = ticker_to_baostock_code(ticker)
        path = cache_path_for(self.cache_dir, code, start_date, end_date)
        cached = read_intraday_cache(path)
        if cached:
            return cached
        covering_path = find_covering_cache(self.cache_dir, code, start_date, end_date)
        if covering_path:
            return [
                bar
                for bar in read_intraday_cache(covering_path)
                if start_date <= bar.date <= end_date
            ]
        self.login()
        fields = "date,time,code,open,high,low,close,volume,amount,adjustflag"
        rs = self._bs.query_history_k_data_plus(  # type: ignore[union-attr]
            code,
            fields,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            frequency="5",
            adjustflag="3",
        )
        if rs.error_code != "0":
            raise RuntimeError(f"BaoStock query failed for {code}: {rs.error_code} {rs.error_msg}")
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
        write_intraday_cache(path, bars)
        time.sleep(self.sleep_seconds)
        return bars


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
