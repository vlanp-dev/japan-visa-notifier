"""Email composition and delivery.

The mandatory email channel is split in two independent pieces:

* :func:`compose_episode_email` builds the French message, including the page
  and acknowledgement links;
* an :class:`EmailProvider` delivers an :class:`OutgoingEmail`.

Two providers are available and selected by configuration: ``smtp`` (real
delivery through :mod:`smtplib`) and ``outbox`` (a directory of ``.eml`` files,
useful for local operation and tests). There is no implicit provider: an
unconfigured email section produces no provider at all.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import tempfile
import uuid
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote

from .config import Config, EmailConfig
from .repositories import Episode
from .util import mask_recipient, sanitize_error


class EmailDeliveryError(RuntimeError):
    """Raised when an email cannot be composed into a provider call."""


@dataclass(frozen=True)
class OutgoingEmail:
    sender: str
    recipient: str
    subject: str
    body: str


@dataclass(frozen=True)
class EmailContent:
    subject: str
    body: str


class EmailProvider(Protocol):
    """Minimal provider boundary used by the alert service."""

    name: str

    def send(self, message: OutgoingEmail) -> None:
        ...


def compose_episode_email(
    *,
    episode: Episode,
    base_url: str,
    service_name: str,
) -> EmailContent:
    """Build the French episode alert.

    The message always contains the nature of the change, the page URL and the
    acknowledgement link. Header injection is prevented by collapsing any
    whitespace (including line breaks) in the subject.
    """

    if not isinstance(base_url, str) or not base_url:
        raise EmailDeliveryError("base_url is required to compose an email")
    detail_url = f"{base_url}/episodes/{quote(episode.id, safe='')}"
    acknowledgement_url = f"{detail_url}#acquitter"
    subject = " ".join(f"[{service_name}] {episode.change_nature}".split())
    if not subject:
        raise EmailDeliveryError("email subject must not be empty")
    subject = subject[:200]

    lines = [
        "Bonjour,",
        "",
        "Un changement a été détecté sur une page suivie.",
        "",
        f"Nature du changement : {episode.change_nature}",
        f"Page concernée : {episode.page_url}",
        f"Détecté le : {episode.detected_at}",
    ]
    if episode.summary:
        lines.extend(["", f"Résumé : {episode.summary}"])
    lines.extend(
        [
            "",
            f"Consulter l'épisode : {detail_url}",
            f"Acquitter l'épisode : {acknowledgement_url}",
            "",
            "L'acquittement se fait depuis la page de l'épisode.",
        ]
    )
    return EmailContent(subject=subject, body="\n".join(lines) + "\n")


class SmtpEmailProvider:
    """Deliver through SMTP, optionally with STARTTLS and authentication."""

    name = "smtp"

    def __init__(
        self,
        config: EmailConfig,
        *,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
    ) -> None:
        self.config = config
        self._smtp_factory = smtp_factory

    def send(self, message: OutgoingEmail) -> None:
        client = None
        try:
            client = self._smtp_factory(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=self.config.timeout_seconds,
            )
            client.ehlo()
            if self.config.use_starttls:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if self.config.username:
                client.login(self.config.username, self.config.password)
            client.sendmail(
                message.sender,
                [message.recipient],
                _as_mime_bytes(message),
            )
        except Exception as exc:  # the boundary must never leak provider errors
            raise EmailDeliveryError(_safe_error(exc, message.recipient)) from exc
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    pass


class OutboxEmailProvider:
    """Write messages as ``.eml`` files to a directory operator controls."""

    name = "outbox"

    def __init__(self, directory: str) -> None:
        if not directory:
            raise EmailDeliveryError("outbox_path must not be empty")
        self.directory = Path(directory)

    def send(self, message: OutgoingEmail) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            payload = _as_mime_bytes(message)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".outbox-", suffix=".tmp", dir=str(self.directory)
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                final_path = self.directory / (
                    f"{uuid.uuid4().hex}.eml"
                )
                os.replace(temporary_path, final_path)
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise
        except EmailDeliveryError:
            raise
        except Exception as exc:
            raise EmailDeliveryError(_safe_error(exc, message.recipient)) from exc


def build_email_provider(config: Config) -> EmailProvider | None:
    """Return the configured provider, or ``None`` when email is incomplete.

    Nothing is contacted at construction time, and no default provider is
    picked when the configuration is absent.
    """

    email = config.email
    if not email.configured:
        return None
    if email.provider == "outbox":
        return OutboxEmailProvider(email.outbox_path)
    return SmtpEmailProvider(email)


def _as_mime_bytes(message: OutgoingEmail) -> bytes:
    mime = MimeMessage()
    mime["From"] = message.sender
    mime["To"] = message.recipient
    mime["Subject"] = message.subject
    mime["Date"] = formatdate(localtime=True)
    mime["Message-ID"] = make_msgid()
    mime.set_content(message.body, subtype="plain", charset="utf-8")
    return mime.as_bytes()


def _safe_error(error: Exception, recipient: str) -> str:
    text = sanitize_error(error) or error.__class__.__name__
    if recipient:
        text = text.replace(recipient, mask_recipient("email", recipient))
    return text[:1000]
