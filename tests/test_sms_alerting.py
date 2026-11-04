from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.alerting import AlertService
from app.db import Database
from app.repositories import (
    DeliveryAttemptRepository,
    EpisodeRepository,
    SmsChannelStateRepository,
)
from app.sms_channel import (
    SMS_STATE_FAILING,
    SMS_STATE_UNCONFIGURED,
    SMS_STATE_UNVERIFIED,
    SmsChannelService,
)
from app.sms_provider import (
    HttpJsonSmsProvider,
    HttpResponse,
    SmsProviderError,
    SmsSendResult,
)
from app.web import create_app
from tests.support import null_logger, temporary_config

SMS_SETTINGS = {
    "provider": "free-test",
    "url": "https://sms.example.org/send",
    "method": "POST",
    "auth_mode": "bearer",
    "sender": "Alertes",
    "recipient": "+33612345678",
    "credential": "credential-secret",
    "timeout_seconds": 5.0,
}

PAGE_URL = "https://example.org/reglement"


class FakeSmsProvider:
    name = "free-test"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._failures: list[Exception] = []

    def fail_next(self, error: Exception | None = None) -> None:
        self._failures.append(
            error
            or SmsProviderError("sms_http_error", "HTTP 429 quota exceeded")
        )

    def send(self, message: str) -> SmsSendResult:
        self.calls.append(message)
        if self._failures:
            raise self._failures.pop(0)
        return SmsSendResult(
            provider=self.name,
            status_code=200,
            duration_seconds=0.0,
            response_excerpt="{}",
        )


class SmsAlertingTests(unittest.TestCase):
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
            sms=dict(SMS_SETTINGS),
        )
        self.addCleanup(self._config_holder.cleanup)
        self.database = Database(str(Path(self._holder.name) / "alerts.sqlite3"))
        self.database.initialize()
        self.clock = lambda: datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.episodes = EpisodeRepository(self.database, now=self.clock)
        self.deliveries = DeliveryAttemptRepository(self.database, now=self.clock)
        self.states = SmsChannelStateRepository(self.database, now=self.clock)

    def _service(self, provider, *, config=None) -> AlertService:
        config = config or self.config
        sms = SmsChannelService(
            config,
            self.deliveries,
            self.states,
            provider=provider,
            logger=null_logger(),
            clock=self.clock,
        )
        return AlertService(
            config,
            self.episodes,
            self.deliveries,
            sms=sms,
            logger=null_logger(),
            clock=self.clock,
        )

    def _episode_attempts(self, episode) -> list[tuple[str, str, str]]:
        return [
            (attempt.channel, attempt.kind, attempt.status)
            for attempt in self.deliveries.list_for_episode(episode.id)
        ]

    def test_verified_channel_sends_email_then_sms_for_the_episode(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(provider)
        self.assertEqual(service.sms.send_test().status, "sent")

        episode = service.create_episode(
            change_nature="Ouverture de la pêche",
            page_url=PAGE_URL,
        )

        self.assertEqual(
            self._episode_attempts(episode),
            [("email", "alert", "sent"), ("sms", "alert", "sent")],
        )
        self.assertEqual(len(provider.calls), 2)
        message = provider.calls[-1]
        self.assertIn("Ouverture de la peche", message)
        self.assertIn(PAGE_URL, message)
        self.assertEqual(len(list(self.outbox.glob("*.eml"))), 1)

    def test_unverified_channel_is_silent_and_sends_email_only(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(provider)

        episode = service.create_episode(
            change_nature="Nouvelle règle",
            page_url=PAGE_URL,
        )

        self.assertEqual(
            self._episode_attempts(episode), [("email", "alert", "sent")]
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(service.sms.status().state, SMS_STATE_UNVERIFIED)
        self.assertFalse(service.sms.status().active)

    def test_unconfigured_channel_is_explicit_and_sends_email_only(self) -> None:
        holder, config = temporary_config(
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
        self.addCleanup(holder.cleanup)
        app = create_app(config, logger=null_logger())

        self.assertIsNone(app.alerts.sms.provider)
        self.assertEqual(
            app.alerts.sms.status().state, SMS_STATE_UNCONFIGURED
        )

        episode = app.alerts.create_episode(
            change_nature="Nouvelle règle",
            page_url=PAGE_URL,
        )

        attempts = app.deliveries.list_for_episode(episode.id)
        self.assertEqual(
            [(attempt.channel, attempt.status) for attempt in attempts],
            [("email", "sent")],
        )

    def test_active_but_failing_channel_is_attempted_for_later_episodes(
        self,
    ) -> None:
        provider = FakeSmsProvider()
        service = self._service(provider)
        self.assertEqual(service.sms.send_test().status, "sent")

        first = service.create_episode(
            change_nature="Première règle", page_url=PAGE_URL
        )
        provider.fail_next()
        second = service.create_episode(
            change_nature="Deuxième règle", page_url=PAGE_URL
        )
        self.assertEqual(
            self._episode_attempts(second),
            [("email", "alert", "sent"), ("sms", "alert", "failed")],
        )
        self.assertEqual(service.sms.status().state, SMS_STATE_FAILING)
        self.assertTrue(service.sms.status().active)

        third = service.create_episode(
            change_nature="Troisième règle", page_url=PAGE_URL
        )

        self.assertEqual(
            self._episode_attempts(third),
            [("email", "alert", "sent"), ("sms", "alert", "sent")],
        )
        # One test plus three episode alerts, proving the failing channel was
        # still attempted instead of being silently disabled.
        self.assertEqual(len(provider.calls), 4)
        self.assertEqual(
            self.deliveries.list_for_episode(first.id)[1].channel, "sms"
        )

    def test_sms_failure_does_not_change_email_or_block_resend(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(provider)
        self.assertEqual(service.sms.send_test().status, "sent")
        provider.fail_next()

        episode = service.create_episode(
            change_nature="Nouvelle règle", page_url=PAGE_URL
        )

        self.assertEqual(
            self._episode_attempts(episode),
            [("email", "alert", "sent"), ("sms", "alert", "failed")],
        )
        self.assertEqual(len(list(self.outbox.glob("*.eml"))), 1)
        calls_before_resend = len(provider.calls)

        result = service.resend(episode)

        self.assertEqual(result.status, "sent")
        self.assertEqual(len(provider.calls), calls_before_resend)
        self.assertEqual(
            self._episode_attempts(episode),
            [
                ("email", "alert", "sent"),
                ("sms", "alert", "failed"),
                ("email", "resend", "sent"),
            ],
        )
        self.assertEqual(len(list(self.outbox.glob("*.eml"))), 2)

    def test_sms_composition_failure_keeps_email_and_is_recorded(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(provider)
        self.assertEqual(service.sms.send_test().status, "sent")

        episode = service.create_episode(
            change_nature="Nature " * 25,
            page_url="https://example.org/" + "a" * 200,
        )

        attempts = self.deliveries.list_for_episode(episode.id)
        self.assertEqual(
            [(attempt.channel, attempt.status) for attempt in attempts],
            [("email", "sent"), ("sms", "failed")],
        )
        self.assertIn("single SMS", attempts[1].error or "")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(list(self.outbox.glob("*.eml"))), 1)

    def test_sms_boundary_exception_cannot_break_episode_creation(self) -> None:
        class BrokenSmsChannel:
            def send_episode_alert(self, episode):
                raise RuntimeError("canal SMS indisponible")

        service = AlertService(
            self.config,
            self.episodes,
            self.deliveries,
            sms=BrokenSmsChannel(),  # type: ignore[arg-type]
            logger=null_logger(),
            clock=self.clock,
        )

        episode = service.create_episode(
            change_nature="Nouvelle règle", page_url=PAGE_URL
        )

        self.assertIsNotNone(service.get_episode(episode.id))
        self.assertEqual(
            self._episode_attempts(episode), [("email", "alert", "sent")]
        )
        self.assertEqual(len(list(self.outbox.glob("*.eml"))), 1)
        self.assertEqual(service.resend(episode).status, "sent")

    def test_create_app_loads_the_configured_provider_without_injection(
        self,
    ) -> None:
        app = create_app(self.config, logger=null_logger())
        sms = app.alerts.sms

        self.assertIsNotNone(sms)
        self.assertIsInstance(sms.provider, HttpJsonSmsProvider)
        self.assertEqual(sms.provider.name, "free-test")

        sms.provider._transport = lambda request: HttpResponse(
            status_code=200, body=b'{"status": "queued"}'
        )

        self.assertEqual(sms.send_test().status, "sent")

        episode = app.alerts.create_episode(
            change_nature="Ouverture de la pêche", page_url=PAGE_URL
        )

        attempts = app.deliveries.list_for_episode(episode.id)
        self.assertEqual(
            [(attempt.channel, attempt.status) for attempt in attempts],
            [("email", "sent"), ("sms", "sent")],
        )


if __name__ == "__main__":
    unittest.main()
