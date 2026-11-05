"""End-to-end acceptance coverage for the optional SMS channel.

These tests drive the real WSGI application through its HTTP routes, from
startup without any SMS settings to a verified provider, episode alerting,
failure isolation and provider replacement. They intentionally overlap the
focused unit and UI suites: their purpose is to prove the operator-visible
acceptance criteria end to end.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.sms_message import gsm7_septets
from app.sms_provider import SmsProviderError, SmsSendResult
from app.web import create_app
from tests.support import (
    WSGIClient,
    config_document,
    null_logger,
    write_config,
)

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

SMS_SETTINGS_CHANGED_URL = dict(
    SMS_SETTINGS, url="https://sms.example.net/send"
)

SMS_SETTINGS_SECOND_PROVIDER = dict(
    SMS_SETTINGS,
    provider="free-test-alt",
    url="https://sms-alt.example.org/send",
    credential="other-credential-secret",
)

PAGE_URL = "https://example.org/reglement"
NATURE = "Ouverture de la pêche"


class FakeSmsAdapter:
    """A provider adapter that records messages and can fail on demand."""

    def __init__(self, name: str) -> None:
        self.name = name
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


class SmsAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.outbox = self.root / "outbox"
        self.database_path = self.root / "alerts.sqlite3"
        self.clock = lambda: datetime(2026, 2, 1, tzinfo=timezone.utc)

    def _config(self, *, sms: object | None = None, name: str = "config"):
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        document = config_document(
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
            database={"path": str(self.database_path)},
        )
        if sms is not None:
            document["sms"] = sms
        return write_config(directory, document)

    def _build(self, config, provider: FakeSmsAdapter):
        app = create_app(
            config,
            sms_provider=provider,
            logger=null_logger(),
            clock=self.clock,
        )
        return app, WSGIClient(app)

    def _token(self, app, client: WSGIClient) -> str:
        client.get("/")
        return app.csrf.token_for(client.cookies["alerts_session"])

    def _run_test(self, app, client: WSGIClient):
        return client.post_form(
            "/sms/test", {"csrf_token": self._token(app, client)}
        )

    def _create_episode(
        self,
        app,
        client: WSGIClient,
        *,
        nature: str = NATURE,
        page_url: str = PAGE_URL,
    ) -> str:
        response = client.post_form(
            "/episodes",
            {
                "csrf_token": self._token(app, client),
                "change_nature": nature,
                "page_url": page_url,
            },
        )
        self.assertEqual(response.status, 303)
        return response.header("Location").split("?", 1)[0].rsplit("/", 1)[-1]

    def _outbox_count(self) -> int:
        return len(list(self.outbox.glob("*.eml")))

    def test_startup_without_sms_is_explicitly_inactive_and_email_only(
        self,
    ) -> None:
        provider = FakeSmsAdapter("unused")
        app, client = self._build(self._config(), provider)

        dashboard = client.get("/").text
        self.assertIn("Canal SMS", dashboard)
        self.assertIn("Inactif — non configuré", dashboard)

        episode_id = self._create_episode(app, client)

        attempts = app.deliveries.list_for_episode(episode_id)
        self.assertEqual(
            [(attempt.channel, attempt.kind, attempt.status) for attempt in attempts],
            [("email", "alert", "sent")],
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(self._outbox_count(), 1)
        self.assertIn("Inactif — non configuré", client.get("/").text)

    def test_partial_or_malformed_sms_configuration_keeps_service_running(
        self,
    ) -> None:
        cases = {
            "partial": {"provider": "free-test"},
            "malformed": "not-an-object",
        }
        for name, sms in cases.items():
            with self.subTest(name=name):
                provider = FakeSmsAdapter("unused")
                config = self._config(
                    sms=sms, name=f"invalid-sms-{name}"
                )

                self.assertIsNotNone(config.sms_configuration_error)
                self.assertFalse(config.sms_configured)

                app, client = self._build(config, provider)
                response = client.get("/")
                self.assertEqual(response.status, 200)
                self.assertIn("Inactif — non configuré", response.text)

                episode_id = self._create_episode(app, client)
                attempts = app.deliveries.list_for_episode(episode_id)
                self.assertEqual(
                    [
                        (attempt.channel, attempt.kind, attempt.status)
                        for attempt in attempts
                    ],
                    [("email", "alert", "sent")],
                )
                self.assertEqual(provider.calls, [])

    def test_configured_but_unverified_channel_stays_silent(self) -> None:
        provider = FakeSmsAdapter("free-test")
        app, client = self._build(
            self._config(sms=SMS_SETTINGS, name="unverified"), provider
        )

        dashboard = client.get("/").text
        self.assertIn("Inactif — non vérifié", dashboard)
        self.assertIn("free-test", dashboard)

        episode_id = self._create_episode(app, client)

        attempts = app.deliveries.list_for_episode(episode_id)
        self.assertEqual(
            [(attempt.channel, attempt.status) for attempt in attempts],
            [("email", "sent")],
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(self._outbox_count(), 1)

    def test_http_test_route_reports_success_then_failure(self) -> None:
        provider = FakeSmsAdapter("free-test")
        app, client = self._build(
            self._config(sms=SMS_SETTINGS, name="http-test"), provider
        )

        sent = self._run_test(app, client)
        self.assertEqual(sent.status, 303)
        self.assertEqual(sent.header("Location"), "/?notice=sms_test_sent")
        self.assertEqual(app.episodes.count(), 0)
        dashboard = client.get("/", query="notice=sms_test_sent").text
        self.assertIn("Test SMS réussi", dashboard)
        self.assertIn("Actif et vérifié", dashboard)
        self.assertIn("Test du canal SMS", provider.calls[0])

        provider.fail_next()
        failed = self._run_test(app, client)
        self.assertEqual(failed.status, 303)
        self.assertEqual(failed.header("Location"), "/?notice=sms_test_failed")
        dashboard = client.get("/", query="notice=sms_test_failed").text
        self.assertIn("Test SMS échoué", dashboard)
        self.assertIn("Inactif — non vérifié", dashboard)
        self.assertIn("HTTP 429 quota exceeded", dashboard)

        attempts = app.deliveries.list_recent()
        self.assertEqual(
            [(attempt.kind, attempt.status) for attempt in attempts],
            [("test", "failed"), ("test", "sent")],
        )

    def test_episode_opening_sends_email_and_one_short_sms(self) -> None:
        provider = FakeSmsAdapter("free-test")
        app, client = self._build(
            self._config(sms=SMS_SETTINGS, name="both-channels"), provider
        )
        self.assertEqual(self._run_test(app, client).status, 303)

        episode_id = self._create_episode(app, client)

        attempts = app.deliveries.list_for_episode(episode_id)
        self.assertEqual(
            [(attempt.channel, attempt.kind, attempt.status) for attempt in attempts],
            [("email", "alert", "sent"), ("sms", "alert", "sent")],
        )
        self.assertEqual(self._outbox_count(), 1)
        self.assertEqual(len(provider.calls), 2)

        message = provider.calls[-1]
        self.assertIn("Ouverture de la peche", message)
        self.assertIn(PAGE_URL, message)
        self.assertLessEqual(gsm7_septets(message), 160)
        self.assertEqual(attempts[1].segment_count, 1)
        self.assertEqual(attempts[1].recipient_masked, "***78")

        detail = client.get(f"/episodes/{episode_id}").text
        self.assertIn("sms", detail)
        self.assertNotIn("+33612345678", detail)
        self.assertNotIn("credential-secret", detail)

    def test_acknowledgement_link_is_omitted_when_it_would_exceed_one_segment(
        self,
    ) -> None:
        provider = FakeSmsAdapter("free-test")
        app, client = self._build(
            self._config(sms=SMS_SETTINGS, name="long-page"), provider
        )
        self.assertEqual(self._run_test(app, client).status, 303)
        long_page = "https://example.org/" + "a" * 80

        episode_id = self._create_episode(
            app, client, page_url=long_page
        )

        attempts = app.deliveries.list_for_episode(episode_id)
        self.assertEqual(
            [(attempt.channel, attempt.status) for attempt in attempts],
            [("email", "sent"), ("sms", "sent")],
        )
        message = provider.calls[-1]
        self.assertIn("Ouverture de la peche", message)
        self.assertIn(long_page, message)
        self.assertNotIn("#acquitter", message)
        self.assertLessEqual(gsm7_septets(message), 160)
        self.assertEqual(attempts[1].segment_count, 1)

    def test_sms_failure_leaves_email_and_email_only_resend_working(
        self,
    ) -> None:
        provider = FakeSmsAdapter("free-test")
        app, client = self._build(
            self._config(sms=SMS_SETTINGS, name="sms-failure"), provider
        )
        self.assertEqual(self._run_test(app, client).status, 303)
        provider.fail_next()

        episode_id = self._create_episode(app, client)

        attempts = app.deliveries.list_for_episode(episode_id)
        self.assertEqual(
            [(attempt.channel, attempt.kind, attempt.status) for attempt in attempts],
            [("email", "alert", "sent"), ("sms", "alert", "failed")],
        )
        self.assertEqual(self._outbox_count(), 1)
        dashboard = client.get("/").text
        self.assertIn("Actif mais en échec", dashboard)
        self.assertIn("HTTP 429 quota exceeded", dashboard)
        self.assertIn("2026-02-01T00:00:00Z", dashboard)

        sms_calls_before_resend = len(provider.calls)
        resent = client.post_form(
            f"/episodes/{episode_id}/resend",
            {"csrf_token": self._token(app, client)},
        )
        self.assertEqual(resent.status, 303)
        self.assertIn("notice=resent_sent", resent.header("Location"))
        self.assertEqual(len(provider.calls), sms_calls_before_resend)
        self.assertEqual(self._outbox_count(), 2)
        self.assertEqual(
            [
                (attempt.channel, attempt.kind, attempt.status)
                for attempt in app.deliveries.list_for_episode(episode_id)
            ],
            [
                ("email", "alert", "sent"),
                ("sms", "alert", "failed"),
                ("email", "resend", "sent"),
            ],
        )

    def test_configuration_change_invalidates_verification(self) -> None:
        provider = FakeSmsAdapter("free-test")
        app_a, client_a = self._build(
            self._config(sms=SMS_SETTINGS, name="config-a"), provider
        )
        self.assertEqual(self._run_test(app_a, client_a).status, 303)
        verified = app_a.sms.status()
        self.assertTrue(verified.active)

        changed_provider = FakeSmsAdapter("free-test")
        app_b, client_b = self._build(
            self._config(
                sms=SMS_SETTINGS_CHANGED_URL, name="config-b"
            ),
            changed_provider,
        )

        dashboard = client_b.get("/").text
        self.assertIn("Inactif — non vérifié", dashboard)
        status = app_b.sms.status()
        self.assertFalse(status.active)
        self.assertNotEqual(status.fingerprint, verified.fingerprint)
        self.assertIsNone(status.verified_at)

        episode_id = self._create_episode(app_b, client_b)
        self.assertEqual(
            [
                attempt.channel
                for attempt in app_b.deliveries.list_for_episode(episode_id)
            ],
            ["email"],
        )
        self.assertEqual(changed_provider.calls, [])

        self.assertEqual(self._run_test(app_b, client_b).status, 303)
        self.assertIn("Actif et vérifié", client_b.get("/").text)
        new_episode_id = self._create_episode(app_b, client_b)
        self.assertEqual(
            [
                attempt.channel
                for attempt in app_b.deliveries.list_for_episode(new_episode_id)
            ],
            ["email", "sms"],
        )

    def test_provider_can_be_swapped_through_configuration_only(self) -> None:
        first = FakeSmsAdapter("free-test")
        app_a, client_a = self._build(
            self._config(sms=SMS_SETTINGS, name="provider-a"), first
        )
        self.assertEqual(self._run_test(app_a, client_a).status, 303)
        first_episode_id = self._create_episode(app_a, client_a)
        self.assertEqual(
            app_a.deliveries.list_for_episode(first_episode_id)[1].provider,
            "free-test",
        )

        second = FakeSmsAdapter("free-test-alt")
        app_b, client_b = self._build(
            self._config(
                sms=SMS_SETTINGS_SECOND_PROVIDER, name="provider-b"
            ),
            second,
        )
        self.assertIn("Inactif — non vérifié", client_b.get("/").text)
        self.assertEqual(self._run_test(app_b, client_b).status, 303)
        second_episode_id = self._create_episode(app_b, client_b)

        attempts = app_b.deliveries.list_for_episode(second_episode_id)
        self.assertEqual(
            [(attempt.channel, attempt.status) for attempt in attempts],
            [("email", "sent"), ("sms", "sent")],
        )
        self.assertEqual(attempts[1].provider, "free-test-alt")
        self.assertIn("Ouverture de la peche", second.calls[-1])
        self.assertIn(PAGE_URL, second.calls[-1])
        self.assertEqual(len(first.calls), 2)


if __name__ == "__main__":
    unittest.main()
