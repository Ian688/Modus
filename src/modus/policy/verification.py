from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modus.paths import data_path

_DB_LOCK = threading.Lock()
_DB_PATH = data_path("verification.db")

@dataclass(frozen=True)
class VerificationEvidence:
    command: str
    kind: str
    scope: str
    status: str
    exit_code: int
    cwd: str
    output_summary: str = ""

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            command TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            cwd TEXT NOT NULL,
            output_summary TEXT
        )"""
    )

def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn

def record_evidence(evidence: VerificationEvidence) -> None:
    with _DB_LOCK:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO evidence (timestamp, command, kind, scope, status, exit_code, cwd, output_summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    evidence.command,
                    evidence.kind,
                    evidence.scope,
                    evidence.status,
                    evidence.exit_code,
                    evidence.cwd,
                    evidence.output_summary[:2000],
                ),
            )
            conn.commit()
        finally:
            conn.close()

def recent_evidence(limit: int = 10) -> list[dict[str, Any]]:
    with _DB_LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM evidence ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
