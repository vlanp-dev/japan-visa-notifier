"""SQLite repositories for detection state.

The store owns every persisted concept the check loop needs: rules, current and
superseded references, checks, per-rule results and refreeze events.  Each
operation opens its own short-lived connection so the store is safe to share
between the scheduler thread and request handlers.  Writes that belong together
(one check, its results and an optional reference capture) happen in a single
transaction.

Page content is stored as canonical snapshot JSON in the database.  It is never
logged, and repository methods never interpret it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterator

from ..db import connect, initialize_database
from .canonical import CanonicalSnapshot
from .engine import RuleResult
from .rules import Rule, default_rules, normalize_rule


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class ReferenceSeed:
    """Values needed to persist one immutable page reference."""

    target_url: str
    snapshot: CanonicalSnapshot
    operator_label: str
    captured_at: str
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    previous_reference_id: int | None = None


@dataclass(frozen=True)
class ReferenceRecord:
    """One row of ``page_references``."""

    id: int
    target_url: str
    content_hash: str
    normalized_snapshot: str
    http_status: int | None
    etag: str | None
    last_modified: str | None
    captured_at: str
    operator_label: str
    previous_reference_id: int | None
    is_current: bool
    created_at: str

    def to_snapshot(self) -> CanonicalSnapshot:
        """Decode the stored canonical snapshot."""

        return CanonicalSnapshot.from_json(self.normalized_snapshot)


@dataclass(frozen=True)
class CheckRecord:
    """One row of ``checks``."""

    id: int
    started_at: str
    finished_at: str | None
    outcome: str
    reason: str | None
    target_url: str
    http_status: int | None
    etag: str | None
    last_modified: str | None
    reference_id: int | None
    failure_category: str | None
    failure_message: str | None
    duration_ms: int | None
    created_at: str


@dataclass(frozen=True)
class RefreezeEventRecord:
    """One row of ``refreeze_events``."""

    id: int
    occurred_at: str
    success: bool
    previous_reference_id: int | None
    new_reference_id: int | None
    operator_label: str
    message: str | None
    created_at: str


class Store:
    """Persistence facade over a SQLite database path."""

    def __init__(self, database_path: str):
        self.database_path = database_path
        # Idempotent, so constructing a store is always restart-safe.
        initialize_database(database_path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self.database_path)
        try:
            yield connection
        finally:
            connection.close()

    # -- rules ---------------------------------------------------------

    def seed_default_rules(self) -> int:
        """Insert the three default rules if they are missing.

        ``INSERT OR IGNORE`` keyed on the stable rule id means an operator edit
        is never overwritten and repeated calls insert nothing.  Returns the
        number of rows actually inserted.
        """

        now = utc_now_iso()
        inserted = 0
        with self._connection() as connection, connection:
            for rule in default_rules():
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO rules
                        (id, name, kind, enabled, parameters,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule.id,
                        rule.name,
                        rule.kind,
                        1 if rule.enabled else 0,
                        rule.parameters_json(),
                        now,
                        now,
                    ),
                )
                inserted += max(cursor.rowcount, 0)
        return inserted

    def list_rules(self) -> list[Rule]:
        """Return every persisted rule in insertion order."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM rules ORDER BY rowid"
            ).fetchall()
        return [Rule.from_row(row) for row in rows]

    def get_rule(self, rule_id: str) -> Rule | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM rules WHERE id = ?", (rule_id,)
            ).fetchone()
        return Rule.from_row(row) if row is not None else None

    def add_rule(self, rule: Rule) -> Rule:
        """Persist a new rule, validating it first."""

        normalized = normalize_rule(rule)
        now = utc_now_iso()
        with self._connection() as connection, connection:
            connection.execute(
                """
                INSERT INTO rules
                    (id, name, kind, enabled, parameters, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.id,
                    normalized.name,
                    normalized.kind,
                    1 if normalized.enabled else 0,
                    normalized.parameters_json(),
                    now,
                    now,
                ),
            )
        return normalized

    def update_rule(self, rule: Rule) -> Rule:
        """Replace a persisted rule's name, kind, state and parameters."""

        normalized = normalize_rule(rule)
        with self._connection() as connection, connection:
            cursor = connection.execute(
                """
                UPDATE rules
                   SET name = ?, kind = ?, enabled = ?, parameters = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    normalized.name,
                    normalized.kind,
                    1 if normalized.enabled else 0,
                    normalized.parameters_json(),
                    utc_now_iso(),
                    normalized.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown rule: {normalized.id}")
        return normalized

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> Rule:
        """Enable or disable a rule without touching its parameters."""

        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        rule = self.get_rule(rule_id)
        if rule is None:
            raise KeyError(f"unknown rule: {rule_id}")
        return self.update_rule(replace(rule, enabled=enabled))

    # -- references ----------------------------------------------------

    def current_reference(self) -> ReferenceRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM page_references WHERE is_current = 1"
            ).fetchone()
        return _reference_from_row(row) if row is not None else None

    def get_reference(self, reference_id: int) -> ReferenceRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM page_references WHERE id = ?",
                (reference_id,),
            ).fetchone()
        return _reference_from_row(row) if row is not None else None

    def list_references(self, limit: int = 50) -> list[ReferenceRecord]:
        """Return references newest first, current and superseded alike."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM page_references ORDER BY id DESC LIMIT ?",
                (_positive_limit(limit),),
            ).fetchall()
        return [_reference_from_row(row) for row in rows]

    def save_reference(
        self, seed: ReferenceSeed, *, supersede: bool = True
    ) -> ReferenceRecord:
        """Atomically insert a reference and optionally supersede the current.

        The previous current reference is recorded in
        ``previous_reference_id`` and left in the table with ``is_current = 0``
        so the full history remains available.
        """

        with self._connection() as connection, connection:
            reference_id = _insert_reference(connection, seed, supersede=supersede)
        record = self.get_reference(reference_id)
        assert record is not None
        return record

    def refreeze_reference(
        self,
        seed: ReferenceSeed,
        *,
        operator_label: str,
        message: str | None = None,
        occurred_at: str | None = None,
    ) -> tuple[ReferenceRecord, RefreezeEventRecord]:
        """Replace the current reference and record the success event atomically.

        The previous reference is superseded, the new immutable reference is
        inserted and the success event is written in a single transaction.  Any
        error rolls back all three writes, so a failed refreeze can never leave
        the current reference pointing at a half-recorded state.
        """

        now = utc_now_iso()
        with self._connection() as connection, connection:
            row = connection.execute(
                "SELECT id FROM page_references WHERE is_current = 1"
            ).fetchone()
            previous_id = int(row["id"]) if row is not None else None
            reference_id = _insert_reference(
                connection,
                replace(seed, previous_reference_id=previous_id),
                supersede=True,
            )
            event_id = _insert_refreeze_event(
                connection,
                occurred_at=occurred_at or now,
                success=True,
                previous_reference_id=previous_id,
                new_reference_id=reference_id,
                operator_label=operator_label,
                message=message,
                created_at=now,
            )
        reference = self.get_reference(reference_id)
        assert reference is not None
        event = self.get_refreeze_event(event_id)
        assert event is not None
        return reference, event

    # -- checks and per-rule results ----------------------------------

    def record_check(
        self,
        *,
        started_at: str,
        finished_at: str,
        outcome: str,
        target_url: str,
        reason: str | None = None,
        http_status: int | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        reference_id: int | None = None,
        failure_category: str | None = None,
        failure_message: str | None = None,
        duration_ms: int | None = None,
        results: Iterable[RuleResult] = (),
        new_reference: ReferenceSeed | None = None,
    ) -> CheckRecord:
        """Persist one completed check with its results in one transaction.

        ``new_reference`` captures the initial reference and the check together,
        so a successful first retrieval can never leave a check pointing at a
        missing reference.
        """

        rule_results = tuple(results)
        now = utc_now_iso()
        with self._connection() as connection, connection:
            if new_reference is not None:
                reference_id = _insert_reference(
                    connection, new_reference, supersede=True
                )
            cursor = connection.execute(
                """
                INSERT INTO checks
                    (started_at, finished_at, outcome, reason, target_url,
                     http_status, etag, last_modified, reference_id,
                     failure_category, failure_message, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    finished_at,
                    outcome,
                    reason,
                    target_url,
                    http_status,
                    etag,
                    last_modified,
                    reference_id,
                    failure_category,
                    failure_message,
                    duration_ms,
                    now,
                ),
            )
            check_id = int(cursor.lastrowid)
            for result in rule_results:
                connection.execute(
                    """
                    INSERT INTO rule_results
                        (check_id, rule_id, rule_name, kind, status,
                         current_excerpt, reference_excerpt, detail, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        check_id,
                        result.rule_id,
                        result.rule_name,
                        result.kind,
                        result.status,
                        result.current_excerpt,
                        result.reference_excerpt,
                        result.detail,
                        now,
                    ),
                )
        record = self.get_check(check_id)
        assert record is not None
        return record

    def latest_check(self) -> CheckRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM checks ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _check_from_row(row) if row is not None else None

    def get_check(self, check_id: int) -> CheckRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM checks WHERE id = ?", (check_id,)
            ).fetchone()
        return _check_from_row(row) if row is not None else None

    def list_checks(self, limit: int = 20) -> list[CheckRecord]:
        """Return checks newest first."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM checks ORDER BY id DESC LIMIT ?",
                (_positive_limit(limit),),
            ).fetchall()
        return [_check_from_row(row) for row in rows]

    def results_for_check(self, check_id: int) -> list[RuleResult]:
        """Return the per-rule results of one check in insertion order."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM rule_results WHERE check_id = ? ORDER BY id",
                (check_id,),
            ).fetchall()
        return [
            RuleResult(
                rule_id=row["rule_id"],
                rule_name=row["rule_name"],
                kind=row["kind"],
                status=row["status"],
                current_excerpt=row["current_excerpt"] or "",
                reference_excerpt=row["reference_excerpt"] or "",
                detail=row["detail"] or "",
            )
            for row in rows
        ]

    # -- refreeze history ---------------------------------------------

    def record_refreeze_event(
        self,
        *,
        success: bool,
        operator_label: str,
        previous_reference_id: int | None = None,
        new_reference_id: int | None = None,
        message: str | None = None,
        occurred_at: str | None = None,
    ) -> RefreezeEventRecord:
        now = utc_now_iso()
        with self._connection() as connection, connection:
            event_id = _insert_refreeze_event(
                connection,
                occurred_at=occurred_at or now,
                success=success,
                previous_reference_id=previous_reference_id,
                new_reference_id=new_reference_id,
                operator_label=operator_label,
                message=message,
                created_at=now,
            )
        event = self.get_refreeze_event(event_id)
        assert event is not None
        return event

    def get_refreeze_event(self, event_id: int) -> RefreezeEventRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM refreeze_events WHERE id = ?", (event_id,)
            ).fetchone()
        return _refreeze_event_from_row(row) if row is not None else None

    def list_refreeze_events(self, limit: int = 20) -> list[RefreezeEventRecord]:
        """Return refreeze events newest first."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM refreeze_events ORDER BY id DESC LIMIT ?",
                (_positive_limit(limit),),
            ).fetchall()
        return [_refreeze_event_from_row(row) for row in rows]


# -- row helpers -------------------------------------------------------


def _insert_reference(
    connection: sqlite3.Connection,
    seed: ReferenceSeed,
    *,
    supersede: bool,
) -> int:
    previous_id = seed.previous_reference_id
    if previous_id is None:
        row = connection.execute(
            "SELECT id FROM page_references WHERE is_current = 1"
        ).fetchone()
        previous_id = int(row["id"]) if row is not None else None

    if supersede:
        connection.execute(
            "UPDATE page_references SET is_current = 0 WHERE is_current = 1"
        )

    cursor = connection.execute(
        """
        INSERT INTO page_references
            (target_url, content_hash, normalized_snapshot, http_status, etag,
             last_modified, captured_at, operator_label, previous_reference_id,
             is_current, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            seed.target_url,
            seed.snapshot.digest(),
            seed.snapshot.to_json(),
            seed.http_status,
            seed.etag,
            seed.last_modified,
            seed.captured_at,
            seed.operator_label,
            previous_id,
            1 if supersede else 0,
            seed.captured_at,
        ),
    )
    return int(cursor.lastrowid)


def _insert_refreeze_event(
    connection: sqlite3.Connection,
    *,
    occurred_at: str,
    success: bool,
    previous_reference_id: int | None,
    new_reference_id: int | None,
    operator_label: str,
    message: str | None,
    created_at: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO refreeze_events
            (occurred_at, success, previous_reference_id,
             new_reference_id, operator_label, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            occurred_at,
            1 if success else 0,
            previous_reference_id,
            new_reference_id,
            operator_label,
            message,
            created_at,
        ),
    )
    return int(cursor.lastrowid)


def _reference_from_row(row: Any) -> ReferenceRecord:
    return ReferenceRecord(
        id=int(row["id"]),
        target_url=row["target_url"],
        content_hash=row["content_hash"],
        normalized_snapshot=row["normalized_snapshot"],
        http_status=row["http_status"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        captured_at=row["captured_at"],
        operator_label=row["operator_label"],
        previous_reference_id=row["previous_reference_id"],
        is_current=bool(row["is_current"]),
        created_at=row["created_at"],
    )


def _check_from_row(row: Any) -> CheckRecord:
    return CheckRecord(
        id=int(row["id"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outcome=row["outcome"],
        reason=row["reason"],
        target_url=row["target_url"],
        http_status=row["http_status"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        reference_id=row["reference_id"],
        failure_category=row["failure_category"],
        failure_message=row["failure_message"],
        duration_ms=row["duration_ms"],
        created_at=row["created_at"],
    )


def _refreeze_event_from_row(row: Any) -> RefreezeEventRecord:
    return RefreezeEventRecord(
        id=int(row["id"]),
        occurred_at=row["occurred_at"],
        success=bool(row["success"]),
        previous_reference_id=row["previous_reference_id"],
        new_reference_id=row["new_reference_id"],
        operator_label=row["operator_label"],
        message=row["message"],
        created_at=row["created_at"],
    )


def _positive_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    return limit


__all__ = [
    "CheckRecord",
    "ReferenceRecord",
    "ReferenceSeed",
    "RefreezeEventRecord",
    "Store",
    "utc_now_iso",
]
