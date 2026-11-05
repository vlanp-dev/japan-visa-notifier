"""SQLite persistence helpers.

The detection schema is created with ``CREATE ... IF NOT EXISTS`` statements so
applying it repeatedly is safe, then versioned through a detection-owned
migration marker.  This module deliberately never writes ``PRAGMA
user_version``: a sibling service that shares the database keeps ownership of
that marker, and this service can neither reset nor misinterpret it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

#: Detection-owned migration marker.  It is intentionally separate from
#: ``PRAGMA user_version`` so two services can share one SQLite file safely.
MIGRATION_TABLE = "detection_schema_migrations"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS page_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    normalized_snapshot TEXT NOT NULL,
    http_status INTEGER,
    etag TEXT,
    last_modified TEXT,
    content_type TEXT,
    captured_at TEXT NOT NULL,
    operator_label TEXT NOT NULL,
    previous_reference_id INTEGER REFERENCES page_references(id),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_page_references_current
    ON page_references (is_current)
    WHERE is_current = 1;

CREATE INDEX IF NOT EXISTS idx_page_references_captured_at
    ON page_references (captured_at);

CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    parameters TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('unchanged', 'changed', 'control_failure')),
    reason TEXT,
    target_url TEXT NOT NULL,
    http_status INTEGER,
    etag TEXT,
    last_modified TEXT,
    reference_id INTEGER REFERENCES page_references(id),
    failure_category TEXT,
    failure_message TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checks_started_at
    ON checks (started_at);

CREATE TABLE IF NOT EXISTS rule_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL REFERENCES checks(id) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN (
            'triggered', 'not_triggered', 'disabled', 'not_evaluated'
        )),
    current_excerpt TEXT,
    reference_excerpt TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_results_check
    ON rule_results (check_id);

CREATE TABLE IF NOT EXISTS refreeze_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    previous_reference_id INTEGER REFERENCES page_references(id),
    new_reference_id INTEGER REFERENCES page_references(id),
    operator_label TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refreeze_events_occurred_at
    ON refreeze_events (occurred_at);
"""


def _migration_initial(connection: sqlite3.Connection) -> None:
    """Create the detection tables and indexes."""

    connection.executescript(SCHEMA)


def _migration_reference_content_type(connection: sqlite3.Connection) -> None:
    """Add the reference content type to databases created before version 2."""

    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(page_references)")
    }
    if "content_type" not in columns:
        connection.execute(
            "ALTER TABLE page_references ADD COLUMN content_type TEXT"
        )


#: Ordered, idempotent detection migrations.  Each entry is applied once and
#: recorded in ``MIGRATION_TABLE``.
_MIGRATIONS = (
    (1, _migration_initial),
    (2, _migration_reference_content_type),
)


def connect(database_path: str) -> sqlite3.Connection:
    """Open a connection with the pragmas the service relies on."""

    if database_path != ":memory:":
        Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if database_path != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Apply pending detection migrations without touching ``user_version``."""

    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row[0])
        for row in connection.execute(
            f"SELECT version FROM {MIGRATION_TABLE}"
        )
    }
    for version, migration in _MIGRATIONS:
        if version in applied:
            continue
        migration(connection)
        connection.execute(
            f"INSERT INTO {MIGRATION_TABLE} (version, applied_at) "
            "VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
    connection.commit()


def initialize_database(database_path: str) -> None:
    """Open, initialize and close the database in a restart-safe way."""

    connection = connect(database_path)
    try:
        initialize_schema(connection)
    finally:
        connection.close()
