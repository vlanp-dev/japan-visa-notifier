from __future__ import annotations

import tempfile
import unittest
from email.parser import BytesParser
from pathlib import Path

from app.alerting import UNSET, UNCONFIGURED_ERROR, AlertService
from app.db import Database
from app.mailer import EmailDeliveryError, OutgoingEmail
from app.repositories import DeliveryAttemptRepository, EpisodeRepository
from tests.support import null_logger, temporary_config


class FailingProvider:
    name = "fake"

    def __init__(self, message: str = "quota dépassé") -> None:
        self.message = message
        self.calls = 0

    def send(self, message: OutgoingEmail) -> None:
        self.calls += 1
        raise EmailDeliveryError(self.message)


class AlertServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._holder = tempfile.TemporaryDirectory()
        self.addCleanup(self._holder.cleanup)
        self.outbox = Path(self._holder.name) / "outbox"
        self._config_holder, self.config = temporary_config(
            service={
                "name": "Alertes de test",
                "environment": "test",
                "base_url": "https://alerts.example.org",
            },
            email={
                "provider": "outbox",
                "outbox_path": str(self.outbox),
                "sender": "alerts@example.org",
                "recipient": "operator@example.org",
            },
        )
        self.addCleanup(self._config_holder.cleanup)
        self.database = Database(str(Path(self._holder.name) / "alerts.sqlite3"))
        self.database.initialize()
        self.episodes = EpisodeRepository(self.database)
        self.deliveries = DeliveryAttemptRepository(self.database)

    def _service(self, *, provider=UNSET) -> AlertService:
        return AlertService(
            self.config,
            self.episodes,
            self.deliveries,
            provider=provider,
            logger=null_logger(),
        )

    def test_create_episode_sends_and_records_email_alert(self) -> None:
        service = self._service()

        episode = service.create_episode(
            change_nature="Ouverture de la pêche",
            page_url="https://example.org/reglement",
        )

        self.assertIsNotNone(service.get_episode(episode.id))
        attempts = self.deliveries.list_for_episode(episode.id)
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(attempt.channel, "email")
        self.assertEqual(attempt.kind, "alert")
        self.assertEqual(attempt.status, "sent")
        self.assertEqual(attempt.provider, "outbox")
        self.assertEqual(attempt.recipient_masked, "o***@example.org")
        files = list(self.outbox.glob("*.eml"))
        self.assertEqual(len(files), 1)
        parsed = BytesParser().parsebytes(files[0].read_bytes())
        content = parsed.get_payload(decode=True).decode("utf-8")
        self.assertIn("Ouverture de la p\u00eache", content)
        self.assertIn("https://example.org/reglement", content)

    def test_email_failure_keeps_episode_usable_and_resend_works(self) -> None:
        failing = FailingProvider("SMTP indisponible")
        service = self._service(provider=failing)

        episode = service.create_episode(
            change_nature="Nouvelle règle",
            page_url="https://example.org/page",
        )

        attempts = self.deliveries.list_for_episode(episode.id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "failed")
        self.assertIn("SMTP indisponible", attempts[0].error or "")
        self.assertIsNotNone(service.get_episode(episode.id))

        acknowledged = service.acknowledge(episode.id)
        self.assertIsNotNone(acknowledged)
        self.assertIsNotNone(acknowledged.acknowledged_at)

        result = service.resend(episode)
        self.assertEqual(result.status, "failed")
        self.assertEqual(failing.calls, 2)
        attempts = self.deliveries.list_for_episode(episode.id)
        self.assertEqual([attempt.kind for attempt in attempts], ["alert", "resend"])
        self.assertTrue(all(attempt.channel == "email" for attempt in attempts))

    def test_resend_is_email_only_and_records_success(self) -> None:
        service = self._service()
        episode = service.create_episode(
            change_nature="Mise à jour", page_url="https://example.org/page"
        )

        result = service.resend(episode)

        self.assertEqual(result.status, "sent")
        attempts = self.deliveries.list_for_episode(episode.id)
        self.assertEqual([attempt.kind for attempt in attempts], ["alert", "resend"])
        self.assertTrue(all(attempt.channel == "email" for attempt in attempts))

    def test_unconfigured_email_is_explicit_and_does_not_raise(self) -> None:
        holder, config = temporary_config()
        self.addCleanup(holder.cleanup)
        database = Database(":memory:")
        database.initialize()
        episodes = EpisodeRepository(database)
        deliveries = DeliveryAttemptRepository(database)
        service = AlertService(
            config, episodes, deliveries, logger=null_logger()
        )

        episode = service.create_episode(
            change_nature="Nature", page_url="https://example.org/page"
        )

        attempts = deliveries.list_for_episode(episode.id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "failed")
        self.assertEqual(attempts[0].error, UNCONFIGURED_ERROR)
        self.assertIsNotNone(service.acknowledge(episode.id))


if __name__ == "__main__":
    unittest.main()
