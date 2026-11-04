from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.sms_provider import SmsProviderError, SmsSendResult
from app.web import create_app
from tests.support import WSGIClient, null_logger, temporary_config

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


class SmsUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.outbox = Path(self._temp.name) / "outbox"
        self.clock = lambda: datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.config = None
        self.app = None
        self.client = None
        self.provider = None

    def _build(self, *, sms: dict | None = None, provider=None):
        overrides = {
            "service": {
                "name": "Alertes de test",
                "environment": "test",
                "base_url": "https://alerts.example.org",
            },
            "email": {
                "provider": "outbox",
                "outbox_path": str(self.outbox),
                "sender": "alerts@example.org",
                "recipient": "operator@example.org",
            },
        }
        if sms is not None:
            overrides["sms"] = sms
        holder, self.config = temporary_config(**overrides)
        self.addCleanup(holder.cleanup)
        self.provider = provider if provider is not None else FakeSmsProvider()
        kwargs = {"logger": null_logger(), "clock": self.clock}
        if sms is not None:
            kwargs["sms_provider"] = self.provider
        self.app = create_app(self.config, **kwargs)
        self.client = WSGIClient(self.app)
        return self.app, self.client

    def _token(self) -> str:
        self.client.get("/")
        return self.app.csrf.token_for(self.client.cookies["alerts_session"])

    def _create_episode(self) -> str:
        response = self.client.post_form(
            "/episodes",
            {
                "csrf_token": self._token(),
                "change_nature": "Ouverture de la pêche",
                "page_url": PAGE_URL,
            },
        )
        self.assertEqual(response.status, 303)
        return response.header("Location").split("?", 1)[0]

    def test_unconfigured_channel_is_explicitly_inactive_on_dashboard(self) -> None:
        self._build()

        text = self.client.get("/").text

        self.assertIn("Canal SMS", text)
        self.assertIn("Inactif — non configuré", text)
        self.assertIn("Aucun test effectué", text)
        self.assertIn('action="/sms/test"', text)
        self.assertIn('name="csrf_token"', text)
        self.assertNotIn("credential-secret", text)

    def test_configured_but_unverified_channel_is_explicit_and_silent(self) -> None:
        self._build(sms=SMS_SETTINGS, provider=FakeSmsProvider())

        text = self.client.get("/").text

        self.assertIn("Inactif — non vérifié", text)
        self.assertIn("free-test", text)
        self.assertIn("***78", text)
        self.assertNotIn("+33612345678", text)
        self.assertNotIn("credential-secret", text)
        self.assertEqual(self.provider.calls, [])

    def test_test_route_refuses_unconfigured_channel_without_episode(self) -> None:
        self._build()

        response = self.client.post_form(
            "/sms/test", {"csrf_token": self._token()}
        )

        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("Location"), "/?notice=sms_test_refused")
        self.assertEqual(self.app.episodes.count(), 0)
        attempts = self.app.deliveries.list_recent()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].channel, "sms")
        self.assertEqual(attempts[0].kind, "test")
        self.assertEqual(attempts[0].status, "failed")
        self.assertIsNone(attempts[0].episode_id)

        dashboard = self.client.get("/", query="notice=sms_test_refused").text
        self.assertIn("Test SMS refusé", dashboard)
        self.assertIn("Inactif — non configuré", dashboard)

    def test_test_route_success_verifies_channel_and_shows_notice(self) -> None:
        provider = FakeSmsProvider()
        self._build(sms=SMS_SETTINGS, provider=provider)

        response = self.client.post_form(
            "/sms/test", {"csrf_token": self._token()}
        )

        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("Location"), "/?notice=sms_test_sent")
        self.assertEqual(self.app.episodes.count(), 0)
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("Test du canal SMS", provider.calls[0])

        attempts = self.app.deliveries.list_recent()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].channel, "sms")
        self.assertEqual(attempts[0].kind, "test")
        self.assertEqual(attempts[0].status, "sent")
        self.assertEqual(attempts[0].recipient_masked, "***78")

        dashboard = self.client.get("/", query="notice=sms_test_sent").text
        self.assertIn("Test SMS réussi", dashboard)
        self.assertIn("Actif et vérifié", dashboard)
        self.assertIn("Réussi le 2026-02-01T00:00:00Z", dashboard)

    def test_test_route_failure_is_explicit_and_keeps_channel_unverified(
        self,
    ) -> None:
        provider = FakeSmsProvider()
        provider.fail_next()
        self._build(sms=SMS_SETTINGS, provider=provider)

        response = self.client.post_form(
            "/sms/test", {"csrf_token": self._token()}
        )

        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("Location"), "/?notice=sms_test_failed")
        attempts = self.app.deliveries.list_recent()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].kind, "test")
        self.assertEqual(attempts[0].status, "failed")

        dashboard = self.client.get("/", query="notice=sms_test_failed").text
        self.assertIn("Test SMS échoué", dashboard)
        self.assertIn("Inactif — non vérifié", dashboard)
        self.assertIn("HTTP 429 quota exceeded", dashboard)
        self.assertIn("2026-02-01T00:00:00Z", dashboard)

    def test_test_route_requires_csrf_token(self) -> None:
        provider = FakeSmsProvider()
        self._build(sms=SMS_SETTINGS, provider=provider)

        response = self.client.post_form("/sms/test", {})

        self.assertEqual(response.status, 403)
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.app.deliveries.count(), 0)

    def test_sms_alert_failure_is_visible_and_email_still_works(self) -> None:
        provider = FakeSmsProvider()
        self._build(sms=SMS_SETTINGS, provider=provider)
        self.client.post_form("/sms/test", {"csrf_token": self._token()})
        provider.fail_next()

        location = self._create_episode()
        episode_id = location.rsplit("/", 1)[-1]

        attempts = self.app.deliveries.list_for_episode(episode_id)
        self.assertEqual(
            [(attempt.channel, attempt.kind, attempt.status) for attempt in attempts],
            [("email", "alert", "sent"), ("sms", "alert", "failed")],
        )
        self.assertEqual(len(list(self.outbox.glob("*.eml"))), 1)

        dashboard = self.client.get("/").text
        self.assertIn("Actif mais en échec", dashboard)
        self.assertIn("HTTP 429 quota exceeded", dashboard)
        self.assertIn("2026-02-01T00:00:00Z", dashboard)

        resent = self.client.post_form(
            f"/episodes/{episode_id}/resend",
            {"csrf_token": self._token()},
        )
        self.assertEqual(resent.status, 303)
        self.assertIn("notice=resent_sent", resent.header("Location"))
        self.assertEqual(len(list(self.outbox.glob("*.eml"))), 2)
        self.assertEqual(
            [attempt.kind for attempt in self.app.deliveries.list_for_episode(episode_id)],
            ["alert", "alert", "resend"],
        )

    def test_delivery_log_and_episode_detail_show_sms_attempts_masked(self) -> None:
        provider = FakeSmsProvider()
        self._build(sms=SMS_SETTINGS, provider=provider)
        self.client.post_form("/sms/test", {"csrf_token": self._token()})
        location = self._create_episode()

        log = self.client.get("/deliveries").text
        self.assertIn("sms", log)
        self.assertIn("***78", log)
        self.assertNotIn("+33612345678", log)
        self.assertNotIn("credential-secret", log)

        detail = self.client.get(location).text
        self.assertIn("sms", detail)
        self.assertIn("***78", detail)
        self.assertNotIn("+33612345678", detail)
        self.assertNotIn("credential-secret", detail)


if __name__ == "__main__":
    unittest.main()
