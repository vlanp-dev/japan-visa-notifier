from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.db import Database
from app.repositories import (
    DeliveryAttemptRepository,
    EpisodeRepository,
)


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.database = Database(str(Path(self._temp.name) / "alerts.sqlite3"))
        self.database.initialize()
        self.episodes = EpisodeRepository(self.database)
        self.deliveries = DeliveryAttemptRepository(self.database)

    def test_initialize_is_idempotent_and_health_check_passes(self) -> None:
        self.database.initialize()
        self.assertTrue(self.database.check())
        with self.database.connection() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("episodes", tables)
        self.assertIn("delivery_attempts", tables)
        self.assertIn("sms_channel_state", tables)

    def test_create_get_list_and_acknowledge_episode(self) -> None:
        moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        episode = self.episodes.create(
            change_nature="Ouverture de la pêche",
            page_url="https://example.org/reglement?x=1",
            summary="Le règlement a changé.",
            detected_at=moment,
        )

        self.assertEqual(episode.change_nature, "Ouverture de la pêche")
        self.assertEqual(episode.detected_at, "2026-01-02T03:04:05Z")
        self.assertIsNone(episode.acknowledged_at)
        self.assertEqual(self.episodes.get(episode.id), episode)
        self.assertEqual(self.episodes.count(), 1)
        self.assertEqual(self.episodes.list_recent(), [episode])

        acknowledged = self.episodes.acknowledge(
            episode.id, datetime(2026, 1, 3, tzinfo=timezone.utc)
        )
        self.assertEqual(acknowledged.acknowledged_at, "2026-01-03T00:00:00Z")

        # A second acknowledgement keeps the first timestamp.
        again = self.episodes.acknowledge(
            episode.id, datetime(2026, 1, 4, tzinfo=timezone.utc)
        )
        self.assertEqual(again.acknowledged_at, "2026-01-03T00:00:00Z")

    def test_episode_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.episodes.create(change_nature="", page_url="https://example.org")
        with self.assertRaises(ValueError):
            self.episodes.create(
                change_nature="Nature", page_url="javascript:alert(1)"
            )
        with self.assertRaises(ValueError):
            self.episodes.create(
                change_nature="Nature", page_url="https://example.org/" + "a" * 3000
            )

    def test_delivery_attempts_store_masked_recipients_and_metadata(self) -> None:
        episode = self.episodes.create(
            change_nature="Nouvelle règle",
            page_url="https://example.org/page",
        )

        email = self.deliveries.record(
            channel="email",
            kind="alert",
            status="sent",
            episode_id=episode.id,
            provider="smtp",
            recipient="alice@example.org",
            segment_count=1,
            duration_ms=42,
        )
        sms = self.deliveries.record(
            channel="sms",
            kind="resend",
            status="failed",
            episode_id=episode.id,
            provider="free-provider",
            recipient="+33 6 12 34 56 78",
            error="quota exceeded for token=abcdef",
        )
        test = self.deliveries.record(
            channel="sms",
            kind="test",
            status="sent",
            recipient="+33 6 12 34 56 78",
        )

        self.assertEqual(email.recipient_masked, "a***@example.org")
        self.assertEqual(sms.recipient_masked, "***78")
        self.assertEqual(test.episode_id, None)
        self.assertEqual(test.kind, "test")
        self.assertNotIn("abcdef", sms.error or "")
        self.assertIn("***", sms.error or "")
        self.assertEqual(sms.segment_count, None)

        self.assertEqual(len(self.deliveries.list_for_episode(episode.id)), 2)
        self.assertEqual(self.deliveries.count(), 3)
        self.assertEqual(len(self.deliveries.list_recent(2)), 2)

    def test_delivery_attempt_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            self.deliveries.record(channel="fax", kind="alert", status="sent")
        with self.assertRaises(ValueError):
            self.deliveries.record(channel="email", kind="other", status="sent")
        with self.assertRaises(ValueError):
            self.deliveries.record(channel="email", kind="test", status="unknown")
        with self.assertRaises(ValueError):
            self.deliveries.record(
                channel="sms", kind="test", status="sent", segment_count=-1
            )

    def test_unknown_episode_is_rejected_by_foreign_key(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.deliveries.record(
                channel="email",
                kind="alert",
                status="sent",
                episode_id="does-not-exist",
                recipient="alice@example.org",
            )

    def test_limit_zero_returns_empty(self) -> None:
        self.assertEqual(self.episodes.list_recent(0), [])
        self.assertEqual(self.deliveries.list_recent(0), [])

    def test_memory_database(self) -> None:
        memory = Database(":memory:")
        memory.initialize()
        self.assertTrue(memory.check())
        repository = EpisodeRepository(memory)
        episode = repository.create(
            change_nature="Mémoire", page_url="https://example.org"
        )
        self.assertEqual(repository.get(episode.id), episode)


if __name__ == "__main__":
    unittest.main()
