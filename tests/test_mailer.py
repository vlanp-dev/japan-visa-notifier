from __future__ import annotations

import smtplib
import tempfile
import unittest
from email.parser import BytesParser
from pathlib import Path

from app.config import EmailConfig
from app.mailer import (
    EmailDeliveryError,
    OutgoingEmail,
    OutboxEmailProvider,
    SmtpEmailProvider,
    build_email_provider,
    compose_episode_email,
)
from app.repositories import Episode
from tests.support import temporary_config


def sample_episode() -> Episode:
    return Episode(
        id="abc123",
        change_nature="Ouverture de la pêche",
        page_url="https://example.org/reglement",
        summary="Le règlement a changé.",
        detected_at="2026-01-02T03:04:05Z",
        acknowledged_at=None,
        created_at="2026-01-02T03:04:05Z",
    )


class FakeSmtp:
    instances: list["FakeSmtp"] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ehlo_calls = 0
        self.starttls_calls = 0
        self.logins: list[tuple[str, str]] = []
        self.sent: list[tuple[str, list[str], bytes]] = []
        self.quit_calls = 0
        self.fail_with: Exception | None = None
        FakeSmtp.instances.append(self)

    def ehlo(self) -> None:
        self.ehlo_calls += 1

    def starttls(self, context=None) -> None:
        self.starttls_calls += 1

    def login(self, username: str, password: str) -> None:
        self.logins.append((username, password))

    def sendmail(self, sender: str, recipients: list[str], payload: bytes) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((sender, recipients, payload))

    def quit(self) -> None:
        self.quit_calls += 1


class CompositionTests(unittest.TestCase):
    def test_message_contains_nature_page_and_acknowledgement_links(self) -> None:
        content = compose_episode_email(
            episode=sample_episode(),
            base_url="https://alerts.example.org",
            service_name="Alertes",
        )

        self.assertIn("Ouverture de la pêche", content.subject)
        self.assertIn("Nature du changement : Ouverture de la pêche", content.body)
        self.assertIn("https://example.org/reglement", content.body)
        self.assertIn(
            "https://alerts.example.org/episodes/abc123", content.body
        )
        self.assertIn(
            "https://alerts.example.org/episodes/abc123#acquitter", content.body
        )
        self.assertIn("Bonjour,", content.body)

    def test_subject_collapses_header_injection(self) -> None:
        episode = sample_episode()
        episode = Episode(
            id=episode.id,
            change_nature="Nature\r\nBcc: attacker@example.org",
            page_url=episode.page_url,
            summary="",
            detected_at=episode.detected_at,
            acknowledged_at=None,
            created_at=episode.created_at,
        )

        content = compose_episode_email(
            episode=episode, base_url="https://alerts.example.org", service_name="A"
        )

        self.assertNotIn("\r", content.subject)
        self.assertNotIn("\n", content.subject)
        self.assertIn("Bcc: attacker@example.org", content.subject)


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSmtp.instances.clear()

    def _config(self, **overrides) -> EmailConfig:
        values = {
            "provider": "smtp",
            "smtp_host": "smtp.example.org",
            "smtp_port": 587,
            "username": "bot",
            "password": "smtp-password",
            "sender": "alerts@example.org",
            "recipient": "operator@example.org",
            "use_starttls": True,
        }
        values.update(overrides)
        return EmailConfig(**values)

    def _message(self) -> OutgoingEmail:
        return OutgoingEmail(
            sender="alerts@example.org",
            recipient="operator@example.org",
            subject="Sujet",
            body="Corps du message.",
        )

    def test_smtp_provider_uses_starttls_login_and_sends(self) -> None:
        provider = SmtpEmailProvider(self._config(), smtp_factory=FakeSmtp)
        provider.send(self._message())

        self.assertEqual(len(FakeSmtp.instances), 1)
        client = FakeSmtp.instances[0]
        self.assertEqual(client.host, "smtp.example.org")
        self.assertEqual(client.port, 587)
        self.assertEqual(client.starttls_calls, 1)
        self.assertEqual(client.logins, [("bot", "smtp-password")])
        self.assertEqual(len(client.sent), 1)
        sender, recipients, payload = client.sent[0]
        self.assertEqual(sender, "alerts@example.org")
        self.assertEqual(recipients, ["operator@example.org"])
        parsed = BytesParser().parsebytes(payload)
        self.assertEqual(parsed["Subject"], "Sujet")
        self.assertIn("Corps du message.", parsed.get_payload())
        self.assertEqual(client.quit_calls, 1)

    def test_smtp_provider_wraps_and_redacts_errors(self) -> None:
        provider = SmtpEmailProvider(self._config(), smtp_factory=FakeSmtp)
        original_init = FakeSmtp.__init__

        def failing_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.fail_with = smtplib.SMTPException(
                "refused password=smtp-password for operator@example.org"
            )

        FakeSmtp.__init__ = failing_init
        try:
            with self.assertRaises(EmailDeliveryError) as raised:
                provider.send(self._message())
        finally:
            FakeSmtp.__init__ = original_init

        text = str(raised.exception)
        self.assertNotIn("smtp-password", text)
        self.assertNotIn("operator@example.org", text)
        self.assertIn("***", text)

    def test_outbox_provider_writes_an_eml_file(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        provider = OutboxEmailProvider(holder.name)

        provider.send(self._message())

        files = list(Path(holder.name).glob("*.eml"))
        self.assertEqual(len(files), 1)
        parsed = BytesParser().parsebytes(files[0].read_bytes())
        self.assertEqual(parsed["To"], "operator@example.org")
        self.assertIn("Corps du message.", parsed.get_payload())

    def test_outbox_provider_reports_write_failures(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        blocker = Path(holder.name) / "file"
        blocker.write_text("x", encoding="utf-8")
        provider = OutboxEmailProvider(str(blocker))

        with self.assertRaises(EmailDeliveryError):
            provider.send(self._message())

    def test_build_provider_requires_configuration(self) -> None:
        holder, config = temporary_config()
        self.addCleanup(holder.cleanup)
        self.assertIsNone(build_email_provider(config))

    def test_build_provider_selects_outbox_by_configuration(self) -> None:
        holder, config = temporary_config(
            email={
                "provider": "outbox",
                "outbox_path": "var/outbox",
                "sender": "alerts@example.org",
                "recipient": "operator@example.org",
            }
        )
        self.addCleanup(holder.cleanup)

        provider = build_email_provider(config)

        self.assertIsInstance(provider, OutboxEmailProvider)


if __name__ == "__main__":
    unittest.main()
