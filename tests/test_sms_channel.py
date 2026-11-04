from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app.config import SmsConfig
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
    SMS_STATE_VERIFIED,
    UNCONFIGURED_ERROR,
    SmsChannelService,
    sms_config_fingerprint,
)
from app.sms_provider import SmsProviderError, SmsSendResult
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


class SmsChannelServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._holder = tempfile.TemporaryDirectory()
        self.addCleanup(self._holder.cleanup)
        self.database = Database(str(Path(self._holder.name) / "alerts.sqlite3"))
        self.database.initialize()
        self.clock = lambda: datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.episodes = EpisodeRepository(self.database, now=self.clock)
        self.deliveries = DeliveryAttemptRepository(self.database, now=self.clock)
        self.states = SmsChannelStateRepository(self.database, now=self.clock)

    def _config(self, sms: dict | None = None):
        overrides = {
            "service": {
                "name": "Alertes de test",
                "environment": "test",
                "base_url": "https://alerts.example.org",
            }
        }
        if sms is not None:
            overrides["sms"] = sms
        holder, config = temporary_config(**overrides)
        self.addCleanup(holder.cleanup)
        return config

    def _service(self, config, provider) -> SmsChannelService:
        return SmsChannelService(
            config,
            self.deliveries,
            self.states,
            provider=provider,
            logger=null_logger(),
            clock=self.clock,
        )

    def test_unconfigured_channel_is_explicit_and_refuses_without_provider_call(
        self,
    ) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(), provider)

        status = service.status()
        self.assertEqual(status.state, SMS_STATE_UNCONFIGURED)
        self.assertFalse(status.active)
        self.assertIn("non configuré", status.label)

        result = service.send_test()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, UNCONFIGURED_ERROR)
        self.assertEqual(provider.calls, [])
        attempts = self.deliveries.list_recent()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].channel, "sms")
        self.assertEqual(attempts[0].kind, "test")
        self.assertIsNone(attempts[0].episode_id)
        self.assertEqual(attempts[0].status, "failed")
        self.assertEqual(service.status().state, SMS_STATE_UNCONFIGURED)

    def test_invalid_configuration_is_refused_without_a_provider_call(self) -> None:
        config = self._config(SMS_SETTINGS)
        config = replace(config, sms=replace(config.sms, method="DELETE"))
        service = SmsChannelService(
            config,
            self.deliveries,
            self.states,
            logger=null_logger(),
            clock=self.clock,
        )

        status = service.status()
        self.assertEqual(status.state, SMS_STATE_UNCONFIGURED)
        self.assertIsNone(service.provider)

        result = service.send_test()

        self.assertEqual(result.status, "failed")
        self.assertIn("method", result.error or "")
        attempts = self.deliveries.list_recent()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].kind, "test")
        self.assertEqual(attempts[0].status, "failed")

    def test_successful_test_verifies_the_channel_and_records_attempt(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(SMS_SETTINGS), provider)

        initial = service.status()
        self.assertEqual(initial.state, SMS_STATE_UNVERIFIED)
        self.assertFalse(initial.active)
        self.assertIn("non vérifié", initial.label)

        result = service.send_test()

        self.assertEqual(result.status, "sent")
        self.assertIsNotNone(result.attempt)
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("Test du canal SMS", provider.calls[0])
        self.assertIn("https://alerts.example.org/", provider.calls[0])

        status = service.status()
        self.assertEqual(status.state, SMS_STATE_VERIFIED)
        self.assertTrue(status.active)
        self.assertIn("Actif et vérifié", status.label)
        self.assertIsNotNone(status.verified_at)
        self.assertEqual(status.last_test_status, "sent")

        attempt = self.deliveries.list_recent()[0]
        self.assertEqual(attempt.channel, "sms")
        self.assertEqual(attempt.kind, "test")
        self.assertIsNone(attempt.episode_id)
        self.assertEqual(attempt.status, "sent")
        self.assertEqual(attempt.provider, "free-test")
        self.assertEqual(attempt.recipient_masked, "***78")
        self.assertEqual(attempt.segment_count, 1)

    def test_failed_test_clears_verification_and_a_later_test_restores_it(
        self,
    ) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(SMS_SETTINGS), provider)
        self.assertEqual(service.send_test().status, "sent")
        self.assertEqual(service.status().state, SMS_STATE_VERIFIED)

        provider.fail_next()
        result = service.send_test()

        self.assertEqual(result.status, "failed")
        self.assertIn("429", result.error or "")
        status = service.status()
        self.assertEqual(status.state, SMS_STATE_UNVERIFIED)
        self.assertFalse(status.active)
        self.assertIsNone(status.verified_at)
        self.assertEqual(status.last_test_status, "failed")
        self.assertEqual(status.last_error, result.error)

        self.assertEqual(service.send_test().status, "sent")
        self.assertEqual(service.status().state, SMS_STATE_VERIFIED)

    def test_unverified_channel_skips_real_alert_without_provider_call(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(SMS_SETTINGS), provider)
        episode = self.episodes.create(
            change_nature="Ouverture de la pêche",
            page_url="https://example.org/reglement",
        )

        result = service.send_episode_alert(episode)

        self.assertEqual(result.status, "skipped")
        self.assertIsNone(result.attempt)
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.deliveries.count(), 0)

    def test_failed_real_alert_keeps_channel_active_but_failing(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(SMS_SETTINGS), provider)
        self.assertEqual(service.send_test().status, "sent")
        episode = self.episodes.create(
            change_nature="Nouvelle règle",
            page_url="https://example.org/page",
        )

        provider.fail_next()
        result = service.send_episode_alert(episode)

        self.assertEqual(result.status, "failed")
        status = service.status()
        self.assertEqual(status.state, SMS_STATE_FAILING)
        self.assertTrue(status.active)
        self.assertIn("Actif mais en échec", status.label)
        self.assertIsNotNone(status.verified_at)
        self.assertIsNotNone(status.failing_since)
        self.assertEqual(status.last_alert_status, "failed")
        self.assertEqual(status.last_error, result.error)

        attempts = self.deliveries.list_for_episode(episode.id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].channel, "sms")
        self.assertEqual(attempts[0].kind, "alert")
        self.assertEqual(attempts[0].status, "failed")

    def test_successful_real_alert_restores_a_failing_channel(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(SMS_SETTINGS), provider)
        self.assertEqual(service.send_test().status, "sent")
        episode = self.episodes.create(
            change_nature="Mise à jour",
            page_url="https://example.org/page",
        )
        provider.fail_next()
        service.send_episode_alert(episode)
        self.assertEqual(service.status().state, SMS_STATE_FAILING)

        result = service.send_episode_alert(episode)

        self.assertEqual(result.status, "sent")
        status = service.status()
        self.assertEqual(status.state, SMS_STATE_VERIFIED)
        self.assertTrue(status.active)
        self.assertIsNone(status.failing_since)
        self.assertEqual(status.last_alert_status, "sent")

    def test_real_alert_contains_nature_and_page_and_records_alert(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(SMS_SETTINGS), provider)
        self.assertEqual(service.send_test().status, "sent")
        episode = self.episodes.create(
            change_nature="Ouverture de la pêche",
            page_url="https://example.org/reglement",
        )

        result = service.send_episode_alert(episode)

        self.assertEqual(result.status, "sent")
        message = provider.calls[-1]
        self.assertIn("Ouverture de la peche", message)
        self.assertIn("https://example.org/reglement", message)
        self.assertLessEqual(len(message), 160)
        attempts = self.deliveries.list_for_episode(episode.id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].kind, "alert")
        self.assertEqual(attempts[0].status, "sent")

    def test_composition_failure_is_recorded_and_keeps_channel_failing(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(SMS_SETTINGS), provider)
        self.assertEqual(service.send_test().status, "sent")
        episode = self.episodes.create(
            change_nature="Nature " * 25,
            page_url="https://example.org/" + "a" * 200,
        )

        result = service.send_episode_alert(episode)

        self.assertEqual(result.status, "failed")
        self.assertIn("single SMS", result.error or "")
        self.assertEqual(len(provider.calls), 1)
        status = service.status()
        self.assertEqual(status.state, SMS_STATE_FAILING)
        self.assertTrue(status.active)
        attempts = self.deliveries.list_for_episode(episode.id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "failed")
        self.assertIsNone(attempts[0].segment_count)

    def test_fingerprint_change_invalidates_verification(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(SMS_SETTINGS), provider)
        self.assertEqual(service.send_test().status, "sent")
        self.assertEqual(service.status().state, SMS_STATE_VERIFIED)

        changed = self._config(dict(SMS_SETTINGS, credential="another-secret"))
        changed_service = self._service(changed, FakeSmsProvider())

        status = changed_service.status()
        self.assertEqual(status.state, SMS_STATE_UNVERIFIED)
        self.assertFalse(status.active)
        self.assertIsNone(status.verified_at)
        self.assertIsNone(status.last_test_status)
        self.assertIsNone(status.last_alert_status)

    def test_failed_alert_after_verification_is_visible_in_delivery_log(self) -> None:
        provider = FakeSmsProvider()
        service = self._service(self._config(SMS_SETTINGS), provider)
        service.send_test()
        episode = self.episodes.create(
            change_nature="Nouvelle règle",
            page_url="https://example.org/page",
        )
        provider.fail_next(SmsProviderError("sms_timeout", "timeout"))

        service.send_episode_alert(episode)

        attempts = self.deliveries.list_recent()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            [(attempt.kind, attempt.status) for attempt in attempts],
            [("alert", "failed"), ("test", "sent")],
        )
        self.assertIn("timeout", attempts[0].error or "")


class SmsChannelStateRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = SmsChannelStateRepository(
            self.database,
            now=lambda: datetime(2026, 3, 1, tzinfo=timezone.utc),
        )

    def test_state_is_singleton_and_fingerprint_change_resets_everything(
        self,
    ) -> None:
        first = self.repository.ensure("a" * 64)
        self.assertIsNone(first.verified_at)
        self.repository.record_test(fingerprint="a" * 64, status="sent")
        verified = self.repository.get()
        self.assertIsNotNone(verified.verified_at)
        self.repository.record_alert(
            fingerprint="a" * 64, status="failed", error="quota"
        )

        failing = self.repository.get()
        self.assertIsNotNone(failing.verified_at)
        self.assertIsNotNone(failing.failing_since)
        self.assertEqual(failing.last_alert_status, "failed")

        reset = self.repository.ensure("b" * 64)

        self.assertEqual(reset.fingerprint, "b" * 64)
        self.assertIsNone(reset.verified_at)
        self.assertIsNone(reset.failing_since)
        self.assertIsNone(reset.last_test_status)
        self.assertIsNone(reset.last_alert_status)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM sms_channel_state"
            ).fetchone()
        self.assertEqual(row[0], 1)

    def test_failed_test_clears_verification(self) -> None:
        self.repository.record_test(fingerprint="a" * 64, status="sent")
        self.assertIsNotNone(self.repository.get().verified_at)

        failed = self.repository.record_test(
            fingerprint="a" * 64, status="failed", error="boom"
        )

        self.assertIsNone(failed.verified_at)
        self.assertEqual(failed.last_test_status, "failed")
        self.assertEqual(failed.last_test_error, "boom")


class SmsFingerprintTests(unittest.TestCase):
    def _sms(self, **overrides) -> SmsConfig:
        values = {
            "provider": "free",
            "url": "https://sms.example.org/send",
            "method": "POST",
            "auth_mode": "bearer",
            "auth_header": "Authorization",
            "auth_query_param": "api_key",
            "sender": "Alertes",
            "recipient": "+33612345678",
            "credential": "secret-value",
            "body_template": "",
            "timeout_seconds": 5.0,
            "max_response_bytes": 4096,
            "success_json_field": "",
            "success_json_value": "",
        }
        values.update(overrides)
        return SmsConfig(**values)

    def test_fingerprint_is_stable_and_never_contains_the_credential(self) -> None:
        config = self._sms()

        first = sms_config_fingerprint(config)
        second = sms_config_fingerprint(self._sms())

        self.assertEqual(first, second)
        self.assertNotIn("secret-value", first)
        self.assertEqual(len(first), 64)

    def test_every_provider_relevant_setting_changes_the_fingerprint(self) -> None:
        base = self._sms()
        baseline = sms_config_fingerprint(base)
        changes = {
            "provider": "other",
            "url": "https://other.example.org/send",
            "method": "PUT",
            "auth_mode": "header",
            "auth_header": "X-Api-Key",
            "auth_query_param": "key",
            "sender": "Other",
            "recipient": "+33698765432",
            "credential": "other-secret",
            "body_template": '{"to":"{{to}}","message":"{{message}}"}',
            "timeout_seconds": 6.0,
            "max_response_bytes": 8192,
            "success_json_field": "ok",
            "success_json_value": "true",
        }

        for name, value in changes.items():
            with self.subTest(setting=name):
                changed = sms_config_fingerprint(
                    replace(base, **{name: value})
                )
                self.assertNotEqual(baseline, changed)


if __name__ == "__main__":
    unittest.main()
