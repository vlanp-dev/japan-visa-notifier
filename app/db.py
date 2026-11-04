"""SQLite connectivity and schema management.

The service uses a small, explicit schema. Both repositories share the same
connection-management code so callers never have to remember transaction
rules.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    change_nature TEXT NOT NULL,
    page_url TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    detected_at TEXT NOT NULL,
    acknowledged_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id TEXT PRIMARY KEY,
    episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'sms')),
    kind TEXT NOT NULL CHECK (kind IN ('alert', 'resend', 'test')),
    provider TEXT NOT NULL DEFAULT '',
    recipient_masked TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
    segment_count INTEGER CHECK (segment_count IS NULL OR segment_count >= 0),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delivery_attempts_episode
    ON delivery_attempts (episode_id, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_attempts_created
    ON delivery_attempts (created_at);
CREATE INDEX IF NOT EXISTS idx_episodes_created
    ON episodes (created_at);
"""


class Database:
    """Owns the SQLite path and creates connections on demand."""

    def __init__(self, path: str) -> None:
        if not path:
            raise ValueError("database path must not be empty")
        self.path = path
        self._memory_connection: sqlite3.Connection | None = None
        self._memory_lock = threading.Lock()
        if self.path == ":memory:":
            self._memory_connection = self._new_connection()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def check(self) -> bool:
        """Return True when the database is reachable and initialized."""

        with self.connection() as connection:
            connection.execute("SELECT 1").fetchone()
            row = connection.execute("PRAGMA user_version").fetchone()
        return bool(row and int(row[0]) >= SCHEMA_VERSION)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            with self._memory_lock:
                try:
                    yield self._memory_connection
                    self._memory_connection.commit()
                except BaseException:
                    self._memory_connection.rollback()
                    raise
            return

        connection = self._new_connection()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _new_connection(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            parent = Path(self.path).expanduser().parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path, timeout=10.0, check_same_thread=self.path != ":memory:"
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            try:
                connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                # Some filesystems (or an older SQLite build) do not support
                # WAL; the default journal remains safe for a single instance.
                pass
        return connection
