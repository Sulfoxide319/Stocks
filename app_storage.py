#!/usr/bin/env python3
"""Application-owned SQLite storage for the desktop trading assistant."""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


APP_DIR_NAME = "StocksTradingAssistant"
POSITION_FIELDS = [
    "ticker",
    "name",
    "buy_date",
    "buy_time",
    "buy_price",
    "shares",
    "target_price",
    "hard_stop_price",
    "trailing_stop_pct",
    "highest_price",
    "status",
    "notes",
]


@dataclass
class Position:
    ticker: str
    name: str = ""
    buy_date: str = ""
    buy_time: str = ""
    buy_price: float = 0.0
    shares: float = 0.0
    target_price: float = 0.0
    hard_stop_price: float = 0.0
    trailing_stop_pct: float = 3.0
    highest_price: float = 0.0
    status: str = "open"
    notes: str = ""
    id: int | None = None


def app_data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / APP_DIR_NAME / "data"
    return Path.home() / f".{APP_DIR_NAME}" / "data"


def default_db_path() -> Path:
    return app_data_dir() / "assistant.sqlite"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            buy_date TEXT NOT NULL DEFAULT '',
            buy_time TEXT NOT NULL DEFAULT '',
            buy_price REAL NOT NULL DEFAULT 0,
            shares REAL NOT NULL DEFAULT 0,
            target_price REAL NOT NULL DEFAULT 0,
            hard_stop_price REAL NOT NULL DEFAULT 0,
            trailing_stop_pct REAL NOT NULL DEFAULT 3,
            highest_price REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_positions_status_ticker
            ON positions(status, ticker);

        CREATE TABLE IF NOT EXISTS scan_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generated_at TEXT NOT NULL,
            trade_date TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS latest_snapshot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            generated_at TEXT NOT NULL,
            trade_date TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value),
    )
    conn.commit()


def _float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def position_from_row(row: dict[str, object]) -> Position:
    buy_price = _float(row.get("buy_price"))
    highest = _float(row.get("highest_price"), buy_price)
    return Position(
        ticker=_clean_text(row.get("ticker")),
        name=_clean_text(row.get("name")),
        buy_date=_clean_text(row.get("buy_date")),
        buy_time=_clean_text(row.get("buy_time")),
        buy_price=buy_price,
        shares=_float(row.get("shares")),
        target_price=_float(row.get("target_price")),
        hard_stop_price=_float(row.get("hard_stop_price")),
        trailing_stop_pct=_float(row.get("trailing_stop_pct"), 3.0),
        highest_price=highest,
        status=_clean_text(row.get("status")) or "open",
        notes=_clean_text(row.get("notes")),
        id=int(row["id"]) if row.get("id") not in (None, "") else None,
    )


def validate_position(position: Position) -> list[str]:
    errors: list[str] = []
    if not position.ticker:
        errors.append("代码不能为空。")
    if position.buy_date:
        try:
            dt.date.fromisoformat(position.buy_date)
        except ValueError:
            errors.append("买入日期必须是 YYYY-MM-DD。")
    if position.buy_price <= 0:
        errors.append("买入价格必须大于 0。")
    if position.shares <= 0:
        errors.append("数量必须大于 0。")
    if position.target_price < 0 or position.hard_stop_price < 0:
        errors.append("目标价和止损价不能为负。")
    if position.trailing_stop_pct < 0:
        errors.append("移动止盈回撤不能为负。")
    if position.status not in {"open", "closed"}:
        errors.append("状态只能是 open 或 closed。")
    return errors


def list_positions(conn: sqlite3.Connection, open_only: bool = False) -> list[Position]:
    query = "SELECT * FROM positions"
    params: tuple[object, ...] = ()
    if open_only:
        query += " WHERE status = ?"
        params = ("open",)
    query += " ORDER BY status, ticker, buy_date"
    return [position_from_row(dict(row)) for row in conn.execute(query, params)]


def save_position(conn: sqlite3.Connection, position: Position) -> int:
    errors = validate_position(position)
    if errors:
        raise ValueError("\n".join(errors))
    values = asdict(position)
    values.pop("id", None)
    if position.id:
        conn.execute(
            """
            UPDATE positions SET
                ticker = :ticker,
                name = :name,
                buy_date = :buy_date,
                buy_time = :buy_time,
                buy_price = :buy_price,
                shares = :shares,
                target_price = :target_price,
                hard_stop_price = :hard_stop_price,
                trailing_stop_pct = :trailing_stop_pct,
                highest_price = :highest_price,
                status = :status,
                notes = :notes,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :id
            """,
            {**values, "id": position.id},
        )
        conn.commit()
        return int(position.id)
    cursor = conn.execute(
        """
        INSERT INTO positions (
            ticker, name, buy_date, buy_time, buy_price, shares, target_price,
            hard_stop_price, trailing_stop_pct, highest_price, status, notes
        )
        VALUES (
            :ticker, :name, :buy_date, :buy_time, :buy_price, :shares,
            :target_price, :hard_stop_price, :trailing_stop_pct,
            :highest_price, :status, :notes
        )
        """,
        values,
    )
    conn.commit()
    return int(cursor.lastrowid)


def delete_position(conn: sqlite3.Connection, position_id: int) -> None:
    conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
    conn.commit()


def import_positions_csv(conn: sqlite3.Connection, path: Path, replace_open: bool = False) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if replace_open:
        conn.execute("DELETE FROM positions WHERE status = 'open'")
    count = 0
    for row in rows:
        position = position_from_row(row)
        if not position.ticker:
            continue
        if validate_position(position):
            continue
        save_position(conn, position)
        count += 1
    return count


def export_open_positions_csv(conn: sqlite3.Connection, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    positions = list_positions(conn, open_only=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=POSITION_FIELDS)
        writer.writeheader()
        for position in positions:
            row = asdict(position)
            row.pop("id", None)
            writer.writerow({field: row.get(field, "") for field in POSITION_FIELDS})


def update_positions_from_csv(conn: sqlite3.Connection, path: Path) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = _clean_text(row.get("ticker"))
            if not ticker:
                continue
            conn.execute(
                """
                UPDATE positions SET
                    highest_price = ?,
                    status = ?,
                    notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE ticker = ? AND status = 'open'
                """,
                (
                    _float(row.get("highest_price")),
                    _clean_text(row.get("status")) or "open",
                    _clean_text(row.get("notes")),
                    ticker,
                ),
            )
    conn.commit()


def save_latest_snapshot(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    generated = _clean_text(payload.get("generated_at")) or dt.datetime.now().isoformat(timespec="seconds")
    trade_date = _clean_text(payload.get("date"))
    phase = _clean_text(payload.get("phase"))
    mode = _clean_text(payload.get("mode"))
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    conn.execute(
        """
        INSERT INTO scan_snapshots(generated_at, trade_date, phase, mode, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (generated, trade_date, phase, mode, payload_json),
    )
    conn.execute(
        """
        INSERT INTO latest_snapshot(id, generated_at, trade_date, phase, mode, payload_json, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            generated_at = excluded.generated_at,
            trade_date = excluded.trade_date,
            phase = excluded.phase,
            mode = excluded.mode,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (generated, trade_date, phase, mode, payload_json),
    )
    conn.commit()


def load_latest_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT payload_json FROM latest_snapshot WHERE id = 1").fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_latest_snapshot_excluding(conn: sqlite3.Connection, excluded_phases: set[str]) -> dict[str, Any]:
    if not excluded_phases:
        return load_latest_snapshot(conn)
    placeholders = ",".join("?" for _ in excluded_phases)
    rows = conn.execute(
        f"""
        SELECT payload_json
        FROM scan_snapshots
        WHERE phase NOT IN ({placeholders})
        ORDER BY generated_at DESC, id DESC
        LIMIT 20
        """,
        tuple(sorted(excluded_phases)),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def migrate_legacy_files(conn: sqlite3.Connection, legacy_root: Path) -> list[str]:
    notes: list[str] = []
    if get_setting(conn, "legacy_positions_imported") != "1":
        count = import_positions_csv(conn, legacy_root / "config" / "live_positions.csv")
        set_setting(conn, "legacy_positions_imported", "1")
        if count:
            notes.append(f"已导入旧持仓 {count} 条。")
    if get_setting(conn, "legacy_latest_snapshot_imported") != "1":
        latest_json = legacy_root / "output" / "trading_assistant" / "latest_plan.json"
        if latest_json.exists():
            try:
                payload = json.loads(latest_json.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    save_latest_snapshot(conn, payload)
                    notes.append("已导入上次扫描结果。")
            except (OSError, json.JSONDecodeError):
                notes.append("旧扫描结果无法导入。")
        set_setting(conn, "legacy_latest_snapshot_imported", "1")
    return notes
