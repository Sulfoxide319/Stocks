#!/usr/bin/env python3
"""SQLite journal for local trading-assistant runs and end-of-day archives."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any


URGENT_SELL_ACTIONS = {"SELL_NOW", "TAKE_PROFIT", "TRAIL_SELL", "VWAP_WEAK_SELL", "PRE_CLOSE_REDUCE"}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS assistant_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            phase TEXT NOT NULL,
            mode TEXT NOT NULL,
            monitor_report TEXT NOT NULL,
            monitor_csv TEXT NOT NULL,
            plan_report TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            plan_csv TEXT NOT NULL,
            buy_now_count INTEGER NOT NULL,
            urgent_sell_count INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_assistant_runs_date_phase
            ON assistant_runs(trade_date, phase, generated_at);

        CREATE TABLE IF NOT EXISTS advice_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES assistant_runs(id) ON DELETE CASCADE,
            trade_date TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            phase TEXT NOT NULL,
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            latest_price REAL NOT NULL DEFAULT 0,
            reference_price REAL NOT NULL DEFAULT 0,
            vwap REAL NOT NULL DEFAULT 0,
            target_price REAL NOT NULL DEFAULT 0,
            hard_stop_price REAL NOT NULL DEFAULT 0,
            edge_score REAL NOT NULL DEFAULT 0,
            pnl_pct REAL,
            reason TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_advice_events_lookup
            ON advice_events(trade_date, side, action, ticker);

        CREATE TABLE IF NOT EXISTS daily_archives (
            trade_date TEXT PRIMARY KEY,
            archived_at TEXT NOT NULL,
            latest_plan_md TEXT NOT NULL,
            latest_plan_json TEXT NOT NULL,
            latest_plan_csv TEXT NOT NULL,
            run_count INTEGER NOT NULL,
            buy_now_count INTEGER NOT NULL,
            urgent_sell_count INTEGER NOT NULL,
            notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS actual_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            side TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            price REAL NOT NULL,
            shares REAL NOT NULL DEFAULT 0,
            traded_at TEXT NOT NULL,
            source_advice_id INTEGER REFERENCES advice_events(id),
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()


def _as_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def record_assistant_run(
    db_path: Path,
    payload: dict[str, Any],
    plan_report: Path,
    plan_json: Path,
    plan_csv: Path,
) -> int:
    buy_items = list(payload.get("buy") or [])
    sell_items = list(payload.get("sell") or [])
    buy_now_count = sum(1 for item in buy_items if item.get("action") == "BUY_NOW")
    urgent_sell_count = sum(1 for item in sell_items if item.get("action") in URGENT_SELL_ACTIONS)

    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO assistant_runs (
                trade_date, generated_at, phase, mode, monitor_report, monitor_csv,
                plan_report, plan_json, plan_csv, buy_now_count, urgent_sell_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["date"],
                payload["generated_at"],
                payload["phase"],
                payload["mode"],
                payload["monitor_report"],
                payload["monitor_csv"],
                str(plan_report),
                str(plan_json),
                str(plan_csv),
                buy_now_count,
                urgent_sell_count,
            ),
        )
        run_id = int(cursor.lastrowid)
        for side, items in (("buy", buy_items), ("sell", sell_items)):
            for item in items:
                reference_price = item.get("trigger_price") if side == "buy" else item.get("buy_price")
                conn.execute(
                    """
                    INSERT INTO advice_events (
                        run_id, trade_date, generated_at, phase, side, action, ticker, name,
                        latest_price, reference_price, vwap, target_price, hard_stop_price,
                        edge_score, pnl_pct, reason, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        payload["date"],
                        payload["generated_at"],
                        payload["phase"],
                        side,
                        item.get("action", ""),
                        item.get("ticker", ""),
                        item.get("name", ""),
                        _as_float(item.get("latest_price")),
                        _as_float(reference_price),
                        _as_float(item.get("vwap")),
                        _as_float(item.get("target_price")),
                        _as_float(item.get("hard_stop_price")),
                        _as_float(item.get("edge_score")),
                        _as_float(item.get("pnl_pct")) if side == "sell" else None,
                        item.get("reason", ""),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
        conn.commit()
        return run_id


def archive_trading_day(db_path: Path, trade_date: dt.date, out_dir: Path, notes: str = "") -> None:
    latest_md = out_dir / "latest_plan.md"
    latest_json = out_dir / "latest_plan.json"
    latest_csv = out_dir / "latest_plan.csv"
    archived_at = dt.datetime.now().isoformat(timespec="seconds")
    date_value = trade_date.isoformat()
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(buy_now_count), 0), COALESCE(SUM(urgent_sell_count), 0)
            FROM assistant_runs
            WHERE trade_date = ?
            """,
            (date_value,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO daily_archives (
                trade_date, archived_at, latest_plan_md, latest_plan_json, latest_plan_csv,
                run_count, buy_now_count, urgent_sell_count, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                archived_at = excluded.archived_at,
                latest_plan_md = excluded.latest_plan_md,
                latest_plan_json = excluded.latest_plan_json,
                latest_plan_csv = excluded.latest_plan_csv,
                run_count = excluded.run_count,
                buy_now_count = excluded.buy_now_count,
                urgent_sell_count = excluded.urgent_sell_count,
                notes = excluded.notes
            """,
            (
                date_value,
                archived_at,
                str(latest_md),
                str(latest_json),
                str(latest_csv),
                int(row[0] or 0),
                int(row[1] or 0),
                int(row[2] or 0),
                notes,
            ),
        )
        conn.commit()
