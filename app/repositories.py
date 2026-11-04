"""Repositories for episodes and delivery attempts.

Repositories are the only place that writes to SQLite. They validate their
inputs (including URLs that will later appear in email and SMS messages) and
they only ever persist a masked recipient, never a full address or phone
number.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from urllib.parse import urlsplit

from .db import Database
from .util import format_timestamp, mask_recipient, sanitize_error, utcnow

CHANNELS = frozenset({"email", "sms"})
KINDS = frozenset({"alert", "resend", "test"})
STATUSES = frozenset({"sent", "failed"})

MAX_NATURE_LENGTH = 200
MAX_SUMMARY_LENGTH = 2000
MAX_URL_LENGTH = 2048
MAX_PROVIDER_LENGTH = 100


@dataclass(frozen=True)
class Episode:
    id: str
    change_nature: str
    page_url: str
    summary: str
    detected_at: str
    acknowledged_at: str | None
    created_at: str


@dataclass(frozen=True)
class DeliveryAttempt:
    id: str
    episode_id: str | None
    channel: str
    kind: str
    provider: str
    recipient_masked: str
    status: str
    segment_count: int | None
    duration_ms: int | None
    error: str | None
    created_at: str


def _limit(value: int, maximum: int = 200) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    if value <= 0:
        return 0
    return min(value, maximum)


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return format_timestamp()
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        return text[:40]
    raise ValueError("timestamp must be a datetime or a string")


class EpisodeRepository:
    def __init__(
        self,
        database: Database,
        *,
        now: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self._now = now
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def create(
        self,
        *,
        change_nature: str,
        page_url: str,
        summary: str = "",
        detected_at: datetime | str | None = None,
    ) -> Episode:
        nature = _required_text(change_nature, "change_nature", MAX_NATURE_LENGTH)
        url = _validated_url(page_url)
        description = _optional_text(summary, "summary", MAX_SUMMARY_LENGTH)
        moment = _timestamp(detected_at)
        episode = Episode(
            id=self._id_factory(),
            change_nature=nature,
            page_url=url,
            summary=description,
            detected_at=moment,
            acknowledged_at=None,
            created_at=moment,
        )
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO episodes (
                    id, change_nature, page_url, summary,
                    detected_at, acknowledged_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode.id,
                    episode.change_nature,
                    episode.page_url,
                    episode.summary,
                    episode.detected_at,
                    episode.acknowledged_at,
                    episode.created_at,
                ),
            )
        return episode

    def get(self, episode_id: str) -> Episode | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
        return _episode_from_row(row) if row else None

    def list_recent(self, limit: int = 50) -> list[Episode]:
        count = _limit(limit)
        if count == 0:
            return []
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM episodes
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (count,),
            ).fetchall()
        return [_episode_from_row(row) for row in rows]

    def acknowledge(
        self, episode_id: str, acknowledged_at: datetime | str | None = None
    ) -> Episode | None:
        moment = _timestamp(acknowledged_at or self._now())
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE episodes
                SET acknowledged_at = ?
                WHERE id = ? AND acknowledged_at IS NULL
                """,
                (moment, episode_id),
            )
            row = connection.execute(
                "SELECT * FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
        return _episode_from_row(row) if row else None

    def count(self) -> int:
        with self.database.connection() as connection:
            row = connection.execute("SELECT COUNT(*) FROM episodes").fetchone()
        return int(row[0])


class DeliveryAttemptRepository:
    def __init__(
        self,
        database: Database,
        *,
        now: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self._now = now
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def record(
        self,
        *,
        channel: str,
        kind: str,
        status: str,
        episode_id: str | None = None,
        provider: str = "",
        recipient: str = "",
        segment_count: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
        created_at: datetime | str | None = None,
    ) -> DeliveryAttempt:
        channel = _choice(channel, "channel", CHANNELS)
        kind = _choice(kind, "kind", KINDS)
        status = _choice(status, "status", STATUSES)
        provider = _optional_text(provider, "provider", MAX_PROVIDER_LENGTH)
        if recipient is None:
            recipient = ""
        if not isinstance(recipient, str):
            raise ValueError("recipient must be a string")
        if len(recipient) > 320:
            raise ValueError("recipient is too long (maximum 320)")
        if episode_id is not None:
            episode_id = _required_text(episode_id, "episode_id", 100)
        segment_count = _optional_count(segment_count, "segment_count")
        duration_ms = _optional_count(duration_ms, "duration_ms")
        sanitized_error = sanitize_error(error) or None
        attempt = DeliveryAttempt(
            id=self._id_factory(),
            episode_id=episode_id,
            channel=channel,
            kind=kind,
            provider=provider,
            recipient_masked=mask_recipient(channel, recipient),
            status=status,
            segment_count=segment_count,
            duration_ms=duration_ms,
            error=sanitized_error,
            created_at=_timestamp(created_at or self._now()),
        )
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO delivery_attempts (
                    id, episode_id, channel, kind, provider,
                    recipient_masked, status, segment_count, duration_ms,
                    error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.id,
                    attempt.episode_id,
                    attempt.channel,
                    attempt.kind,
                    attempt.provider,
                    attempt.recipient_masked,
                    attempt.status,
                    attempt.segment_count,
                    attempt.duration_ms,
                    attempt.error,
                    attempt.created_at,
                ),
            )
        return attempt

    def list_recent(self, limit: int = 50) -> list[DeliveryAttempt]:
        count = _limit(limit)
        if count == 0:
            return []
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delivery_attempts
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (count,),
            ).fetchall()
        return [_attempt_from_row(row) for row in rows]

    def list_for_episode(self, episode_id: str) -> list[DeliveryAttempt]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delivery_attempts
                WHERE episode_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (episode_id,),
            ).fetchall()
        return [_attempt_from_row(row) for row in rows]

    def count(self) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM delivery_attempts"
            ).fetchone()
        return int(row[0])


def _episode_from_row(row) -> Episode:
    return Episode(
        id=row["id"],
        change_nature=row["change_nature"],
        page_url=row["page_url"],
        summary=row["summary"],
        detected_at=row["detected_at"],
        acknowledged_at=row["acknowledged_at"],
        created_at=row["created_at"],
    )


def _attempt_from_row(row) -> DeliveryAttempt:
    return DeliveryAttempt(
        id=row["id"],
        episode_id=row["episode_id"],
        channel=row["channel"],
        kind=row["kind"],
        provider=row["provider"],
        recipient_masked=row["recipient_masked"],
        status=row["status"],
        segment_count=row["segment_count"],
        duration_ms=row["duration_ms"],
        error=row["error"],
        created_at=row["created_at"],
    )


def _required_text(value: str, name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    if len(result) > max_length:
        raise ValueError(f"{name} is too long (maximum {max_length})")
    return result


def _optional_text(value: str, name: str, max_length: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    result = value.strip()
    if len(result) > max_length:
        raise ValueError(f"{name} is too long (maximum {max_length})")
    return result


def _choice(value: str, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return value


def _optional_count(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validated_url(value: str) -> str:
    url = _required_text(value, "page_url", MAX_URL_LENGTH)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("page_url must be an absolute http or https URL")
    return url
