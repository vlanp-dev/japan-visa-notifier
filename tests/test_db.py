from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import (
    MIGRATION_TABLE,
    SCHEMA_VERSION,
    connect,
    initialize_database,
    initialize_schema,
)

EXPECTED_TABLES = {
    "page_references",
    "rules",
    "checks",
    "rule_results",
    "refreeze_events",
}


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database_path = str(Path(self._directory.name) / "detection.sqlite3")

    def _tables(self, connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row["name"] for row in rows}

    def test_schema_contains_required_tables(self) -> None:
        initialize_database(self.database_path)
        connection = connect(self.database_path)
        try:
            self.assertTrue(EXPECTED_TABLES.issubset(self._tables(connection)))
            self.assertIn(MIGRATION_TABLE, self._tables(connection))
            versions = [
                row[0]
                for row in connection.execute(
                    f"SELECT version FROM {MIGRATION_TABLE} ORDER BY version"
                )
            ]
            self.assertEqual(versions[-1], SCHEMA_VERSION)
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(page_references)"
                )
            }
            self.assertIn("content_type", columns)
        finally:
            connection.close()

    def test_migration_never_resets_a_foreign_user_version(self) -> None:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            # Simulate a database whose schema is owned by a sibling service
            # that already uses PRAGMA user_version.
            connection.executescript(
                """
                CREATE TABLE page_references (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_url TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    normalized_snapshot TEXT NOT NULL,
                    http_status INTEGER,
                    etag TEXT,
                    last_modified TEXT,
                    captured_at TEXT NOT NULL,
                    operator_label TEXT NOT NULL,
                    previous_reference_id INTEGER,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                PRAGMA user_version = 42;
                """
            )
            connection.commit()
        finally:
            connection.close()

        initialize_database(self.database_path)
        connection = connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 42
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(page_references)"
                )
            }
            self.assertIn("content_type", columns)
            versions = [
                row[0]
                for row in connection.execute(
                    f"SELECT version FROM {MIGRATION_TABLE} ORDER BY version"
                )
            ]
            self.assertEqual(versions[-1], SCHEMA_VERSION)
        finally:
            connection.close()

    def test_initialization_is_idempotent_and_preserves_data(self) -> None:
        initialize_database(self.database_path)
        connection = connect(self.database_path)
        try:
            connection.execute(
                """
                INSERT INTO rules
                    (id, name, kind, enabled, parameters, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "r1",
                    "Annonce de fermeture",
                    "closure_block",
                    1,
                    "{}",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.commit()
            initialize_schema(connection)
            initialize_schema(connection)
            count = connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            connection.close()

    def test_restart_keeps_schema_and_rows(self) -> None:
        initialize_database(self.database_path)
        connection = connect(self.database_path)
        try:
            connection.execute(
                """
                INSERT INTO checks (started_at, outcome, target_url, created_at)
                VALUES (?, 'unchanged', ?, ?)
                """,
                (
                    "2026-01-01T00:00:00+00:00",
                    "https://example.org/",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        initialize_database(self.database_path)
        connection = connect(self.database_path)
        try:
            self.assertTrue(EXPECTED_TABLES.issubset(self._tables(connection)))
            outcome = connection.execute("SELECT outcome FROM checks").fetchone()[0]
            self.assertEqual(outcome, "unchanged")
        finally:
            connection.close()

    def test_only_one_current_reference_is_allowed(self) -> None:
        initialize_database(self.database_path)
        connection = connect(self.database_path)
        try:
            insert = """
                INSERT INTO page_references
                    (target_url, content_hash, normalized_snapshot, captured_at,
                     operator_label, is_current, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """
            values = (
                "https://example.org/",
                "hash",
                "{}",
                "2026-01-01T00:00:00+00:00",
                "opérateur",
                "2026-01-01T00:00:00+00:00",
            )
            connection.execute(insert, values)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    insert,
                    ("https://example.org/", "hash2", "{}", *values[3:]),
                )
            connection.rollback()
        finally:
            connection.close()

    def test_foreign_keys_are_enforced(self) -> None:
        initialize_database(self.database_path)
        connection = connect(self.database_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO rule_results
                        (check_id, rule_id, rule_name, kind, status, created_at)
                    VALUES (999, 'r1', 'Règle', 'closure_block', 'not_triggered',
                            '2026-01-01T00:00:00+00:00')
                    """
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
