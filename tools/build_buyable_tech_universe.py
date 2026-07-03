#!/usr/bin/env python3
"""Build a buyable 600/300/301 technology watchlist from BaoStock industry data."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


DEFAULT_PREFIXES = ("sh.600", "sz.300", "sz.301")
CORE_TECH_INDUSTRY_PREFIXES = (
    "C39",  # computer, communication, electronics
    "I65",  # software and information technology services
    "I64",  # internet services
    "I63",  # telecom / satellite / broadcast transmission
)
SELECTIVE_INDUSTRY_PREFIXES = (
    "C34",  # general equipment
    "C35",  # special equipment
    "C38",  # electrical machinery
    "C40",  # instrumentation
)
SELECTIVE_NAME_KEYWORDS = (
    "科技",
    "智能",
    "电子",
    "芯",
    "微",
    "光",
    "通信",
    "软件",
    "信息",
    "数据",
    "数码",
    "机器人",
    "自动",
    "仪器",
    "激光",
    "网络",
    "网",
    "云",
    "电",
)


def is_buyable_code(code: str, prefixes: tuple[str, ...]) -> bool:
    return code.startswith(prefixes)


def is_excluded_name(name: str) -> bool:
    upper = name.upper()
    return "ST" in upper or "退" in name


def is_tech_like(industry: str, name: str) -> bool:
    if industry.startswith(CORE_TECH_INDUSTRY_PREFIXES):
        return True
    if industry.startswith(SELECTIVE_INDUSTRY_PREFIXES):
        return any(keyword in name for keyword in SELECTIVE_NAME_KEYWORDS)
    return False


def market_from_code(code: str) -> str:
    return "SH" if code.startswith("sh.") else "SZ"


def ticker_from_code(code: str) -> str:
    return code.split(".", 1)[1]


def yahoo_symbol(market: str, ticker: str) -> str:
    return f"{ticker}.SS" if market == "SH" else f"{ticker}.SZ"


def xueqiu_symbol(market: str, ticker: str) -> str:
    return f"{market}{ticker}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build buyable 600/300/301 tech watchlist from BaoStock.")
    parser.add_argument("--out", default="config/watchlist.buyable_600_300_301_expanded.csv")
    parser.add_argument("--prefixes", default=",".join(DEFAULT_PREFIXES))
    args = parser.parse_args()

    import baostock as bs  # type: ignore

    prefixes = tuple(item.strip() for item in args.prefixes.split(",") if item.strip())
    login = bs.login()
    if login.error_code != "0":
        raise SystemExit(f"BaoStock login failed: {login.error_code} {login.error_msg}")

    basic: dict[str, dict[str, str]] = {}
    rs = bs.query_stock_basic()
    while rs.error_code == "0" and rs.next():
        row = dict(zip(rs.fields, rs.get_row_data()))
        basic[row["code"]] = row

    rows: list[dict[str, str]] = []
    rs = bs.query_stock_industry()
    while rs.error_code == "0" and rs.next():
        row = dict(zip(rs.fields, rs.get_row_data()))
        code = row.get("code", "")
        info = basic.get(code, {})
        name = row.get("code_name", "") or info.get("code_name", "")
        industry = row.get("industry", "")
        if info.get("status") != "1":
            continue
        if not is_buyable_code(code, prefixes):
            continue
        if is_excluded_name(name):
            continue
        if not is_tech_like(industry, name):
            continue
        ticker = ticker_from_code(code)
        market = market_from_code(code)
        rows.append(
            {
                "market": market,
                "ticker": ticker,
                "name": name,
                "cik": "",
                "yahoo_symbol": yahoo_symbol(market, ticker),
                "xueqiu_symbol": xueqiu_symbol(market, ticker),
                "cninfo_plate": market.lower(),
                "rss_urls": "",
                "notes": industry,
            }
        )
    bs.logout()

    rows.sort(key=lambda item: item["ticker"])
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = ["market", "ticker", "name", "cik", "yahoo_symbol", "xueqiu_symbol", "cninfo_plate", "rss_urls", "notes"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote={out_path} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
