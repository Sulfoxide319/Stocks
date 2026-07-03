#!/usr/bin/env python3
"""Probe which Xueqiu stock quote endpoints are script-accessible."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://xueqiu.com",
}


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str
    params: dict[str, str]


def endpoint_suite(symbol: str) -> list[Endpoint]:
    now_ms = str(int(time.time() * 1000))
    return [
        Endpoint(
            "realtime_quotec",
            "https://stock.xueqiu.com/v5/stock/realtime/quotec.json",
            {"symbol": symbol},
        ),
        Endpoint(
            "realtime_pankou",
            "https://stock.xueqiu.com/v5/stock/realtime/pankou.json",
            {"symbol": symbol},
        ),
        Endpoint(
            "quote_detail",
            "https://stock.xueqiu.com/v5/stock/quote.json",
            {"symbol": symbol, "extend": "detail"},
        ),
        Endpoint(
            "history_trade",
            "https://stock.xueqiu.com/v5/stock/history/trade.json",
            {"symbol": symbol, "count": "5"},
        ),
        Endpoint(
            "kline_1m",
            "https://stock.xueqiu.com/v5/stock/chart/kline.json",
            {
                "symbol": symbol,
                "begin": now_ms,
                "period": "1m",
                "type": "before",
                "count": "-5",
                "indicator": "kline,pe,pb,ps,pcf,market_capital,agt,ggt,balance",
            },
        ),
        Endpoint(
            "symbol_status_search",
            "https://xueqiu.com/query/v1/symbol/search/status.json",
            {"symbol": symbol, "count": "10", "comment": "0", "page": "1"},
        ),
        Endpoint(
            "status_search",
            "https://xueqiu.com/statuses/search.json",
            {
                "symbol": symbol,
                "q": symbol,
                "count": "10",
                "page": "1",
                "source": "all",
                "sort": "time",
            },
        ),
    ]


def parse_cookie(raw_cookie: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw_cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            cookies[name] = value
    return cookies


def read_default_cookie() -> str:
    cookie_path = Path("config/xueqiu_cookie.txt")
    if cookie_path.exists():
        return cookie_path.read_text(encoding="utf-8").strip()
    return ""


def format_timestamp(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return dt.datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M:%S")


def summarize_payload(name: str, payload: Any) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data:
        item = data[0]
        return (
            f"current={item.get('current')} percent={item.get('percent')} "
            f"timestamp={format_timestamp(item.get('timestamp'))} "
            f"is_trade={item.get('is_trade')}"
        )
    if isinstance(data, dict):
        if name == "realtime_pankou":
            return (
                f"current={data.get('current')} "
                f"bid1={data.get('bp1')}x{data.get('bc1')} "
                f"ask1={data.get('sp1')}x{data.get('sc1')} "
                f"timestamp={format_timestamp(data.get('timestamp'))}"
            )
        if "list" in data and isinstance(data["list"], list):
            sample = data["list"][0] if data["list"] else {}
            return f"items={len(data['list'])} sample_keys={','.join(list(sample.keys())[:8]) if isinstance(sample, dict) else '-'}"
        return f"keys={','.join(list(data.keys())[:8])}"
    if isinstance(payload, dict):
        for key in ("list", "statuses"):
            value = payload.get(key)
            if isinstance(value, list):
                sample = value[0] if value else {}
                return f"items={len(value)} sample_keys={','.join(list(sample.keys())[:8]) if isinstance(sample, dict) else '-'}"
    if isinstance(payload, dict) and payload.get("error_code"):
        return f"error_code={payload.get('error_code')} {payload.get('error_description')}"
    return str(payload)[:160].replace("\n", " ")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether Xueqiu quote data can be fetched by script."
    )
    parser.add_argument("symbol", nargs="?", default="SH600519", help="e.g. SH600519, SZ000001, SH000001")
    parser.add_argument("--rounds", type=int, default=1, help="repeat count for stability checks")
    parser.add_argument(
        "--cookie",
        default=os.getenv("XUEQIU_COOKIE", ""),
        help="optional raw Cookie header; defaults to XUEQIU_COOKIE or config/xueqiu_cookie.txt",
    )
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    session.headers["Referer"] = f"https://xueqiu.com/S/{args.symbol}"

    raw_cookie = args.cookie or read_default_cookie()
    if raw_cookie:
        session.cookies.update(parse_cookie(raw_cookie))
        print("Using provided Xueqiu cookie.")
    else:
        boot = session.get("https://xueqiu.com/", timeout=20)
        print(f"Bootstrapped xueqiu.com: status={boot.status_code} cookies={list(session.cookies.keys())}")

    for round_index in range(1, args.rounds + 1):
        if args.rounds > 1:
            print(f"\nRound {round_index}/{args.rounds}")
        for endpoint in endpoint_suite(args.symbol):
            started = time.perf_counter()
            try:
                resp = session.get(endpoint.url, params=endpoint.params, timeout=15)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                try:
                    payload = resp.json()
                except ValueError:
                    payload = resp.text
                print(
                    f"{endpoint.name}: status={resp.status_code} "
                    f"elapsed_ms={elapsed_ms} {summarize_payload(endpoint.name, payload)}"
                )
            except requests.RequestException as exc:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                print(f"{endpoint.name}: request_failed elapsed_ms={elapsed_ms} {exc}")
        if round_index < args.rounds:
            time.sleep(2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
