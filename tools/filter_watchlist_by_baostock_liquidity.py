#!/usr/bin/env python3
"""Filter a watchlist by recent BaoStock daily traded amount."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dependency_bootstrap import ensure_project_dependencies  # noqa: E402


def baostock_code(market: str, ticker: str) -> str:
    return f"{market.lower()}.{ticker}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter watchlist by BaoStock recent traded amount.")
    parser.add_argument("--watchlist", default="config/watchlist.buyable_600_300_301_expanded.csv")
    parser.add_argument("--out", default="config/watchlist.buyable_600_300_301_liquid.csv")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--lookback-days", type=int, default=45)
    parser.add_argument("--min-avg-amount", type=float, default=200_000_000)
    parser.add_argument("--min-last-amount", type=float, default=150_000_000)
    parser.add_argument("--top", type=int, default=180)
    args = parser.parse_args()

    end_date = dt.date.fromisoformat(args.end_date) if args.end_date else dt.date.today()
    start_date = end_date - dt.timedelta(days=args.lookback_days)
    with Path(args.watchlist).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    ensure_project_dependencies()
    import baostock as bs  # type: ignore

    login = bs.login()
    if login.error_code != "0":
        raise SystemExit(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    kept: list[dict[str, str]] = []
    for index, row in enumerate(rows, 1):
        code = baostock_code(row["market"], row["ticker"])
        rs = bs.query_history_k_data_plus(
            code,
            "date,code,close,volume,amount",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            frequency="d",
            adjustflag="3",
        )
        amounts: list[float] = []
        while rs.error_code == "0" and rs.next():
            data = dict(zip(rs.fields, rs.get_row_data()))
            try:
                amount = float(data.get("amount") or 0)
            except ValueError:
                amount = 0.0
            if amount > 0:
                amounts.append(amount)
        if amounts:
            avg_amount = sum(amounts[-20:]) / min(20, len(amounts))
            last_amount = amounts[-1]
            if avg_amount >= args.min_avg_amount and last_amount >= args.min_last_amount:
                enriched = dict(row)
                enriched["notes"] = f"{row.get('notes', '')}; avg20_amount={avg_amount:.0f}; last_amount={last_amount:.0f}"
                enriched["_avg_amount"] = f"{avg_amount:.2f}"
                enriched["_last_amount"] = f"{last_amount:.2f}"
                kept.append(enriched)
        if index % 50 == 0:
            print(f"checked {index}/{len(rows)} kept={len(kept)}")
        time.sleep(0.02)
    bs.logout()

    kept.sort(key=lambda item: float(item["_avg_amount"]), reverse=True)
    if args.top > 0:
        kept = kept[: args.top]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["market", "ticker", "name", "cik", "yahoo_symbol", "xueqiu_symbol", "cninfo_plate", "rss_urls", "notes"]
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in kept:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    print(f"wrote={out_path} rows={len(kept)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
