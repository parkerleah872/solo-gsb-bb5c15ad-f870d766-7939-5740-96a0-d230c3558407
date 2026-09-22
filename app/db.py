"""SQLite persistence: schema creation (on startup) and CRUD helpers."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    wave_count INTEGER NOT NULL,
    optimal INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db(database_path: str) -> None:
    directory = os.path.dirname(os.path.abspath(database_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _connect(database_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db(request: Request) -> sqlite3.Connection:
    """FastAPI dependency yielding a per-request connection."""
    conn = _connect(request.app.state.settings.database_path)
    try:
        yield conn
    finally:
        conn.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_plan(
    conn: sqlite3.Connection,
    name: str | None,
    request_payload: dict[str, Any],
    result_payload: dict[str, Any],
    wave_count: int,
    optimal: bool,
) -> int:
    cur = conn.execute(
        "INSERT INTO plans (name, request_json, result_json, wave_count, "
        "optimal, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            name,
            json.dumps(request_payload, ensure_ascii=False),
            json.dumps(result_payload, ensure_ascii=False),
            wave_count,
            int(optimal),
            _utcnow(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_plan(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()


def list_plans(
    conn: sqlite3.Connection, limit: int, offset: int
) -> tuple[int, list[sqlite3.Row]]:
    total = int(conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0])
    rows = conn.execute(
        "SELECT * FROM plans ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return total, rows


def delete_plan(conn: sqlite3.Connection, plan_id: int) -> bool:
    cur = conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    conn.commit()
    return cur.rowcount > 0
