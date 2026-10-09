"""Shared location and connection settings for Kompta's local SQLite store."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_database_path(database: str | Path | None = None) -> Path:
    if database is not None:
        return Path(database).expanduser().resolve()

    configured = os.environ.get("KOMPTA_DATABASE_PATH")
    if configured:
        path = Path(configured).expanduser()
        return (PROJECT_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    return PROJECT_ROOT / "kompta.sqlite3"


def connect_database(
    database: str | Path | None = None, *, autocommit: bool = False
) -> sqlite3.Connection:
    path = resolve_database_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path, timeout=30, isolation_level=None if autocommit else ""
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection