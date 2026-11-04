"""The mandatory email channel applied to episode alerts.

The service composes and sends the episode email, then records the outcome as a
delivery attempt. Email is the mandatory channel, but a failure is never
allowed to make the episode unusable: the episode exists before the send, and
acknowledgement and resend keep working afterwards.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import Config
from .logging_config import get_logger
from .mailer import (
    EmailProvider,
    OutgoingEmail,
    build_email_provider,
    compose_episode_email,
)
from .repositories import (
    DeliveryAttempt,
    DeliveryAttemptRepository,
    Episode,
    EpisodeRepository,
)
from .sms_channel import SmsChannelService
from .util import format_timestamp, mask_recipient, sanitize_error, utcnow

UNSET = object()

UNCONFIGURED_ERROR = "Canal email non configuré : envoi impossible."


@dataclass(frozen=True)
class SendResult:
    status: str
    attempt: DeliveryAttempt
    error: str | None


class AlertService:
    """Create episodes and deliver their email alerts and resends."""

    def __init__(
        self,
        config: Config,
        episodes: EpisodeRepository,
        deliveries: DeliveryAttemptRepository,
        *,
        provider: EmailProvider | None = UNSET,  # type: ignore[assignment]
        sms: SmsChannelService | None = None,
        logger=None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.episodes = episodes
        self.deliveries = deliveries
        if provider is UNSET:
            provider = build_email_provider(config)
        self.provider = provider
        self.sms = sms
        self._logger = logger or get_logger("email")
        self._clock = clock

    def create_episode(
        self,
        *,
        change_nature: str,
        page_url: str,
        summary: str = "",
        detected_at: datetime | str | None = None,
    ) -> Episode:
        """Persist the episode first, then attempt the mandatory email."""

        episode = self.episodes.create(
            change_nature=change_nature,
            page_url=page_url,
            summary=summary,
            detected_at=detected_at,
        )
        self.send_episode_alert(episode)
        self._send_sms_alert(episode)
        return episode

    def get_episode(self, episode_id: str) -> Episode | None:
        return self.episodes.get(episode_id)

    def acknowledge(
        self, episode_id: str, acknowledged_at: datetime | str | None = None
    ) -> Episode | None:
        return self.episodes.acknowledge(episode_id, acknowledged_at)

    def send_episode_alert(self, episode: Episode) -> SendResult:
        return self._deliver(episode, kind="alert")

    def resend(self, episode: Episode) -> SendResult:
        """Resend the email only; this never touches another channel."""

        return self._deliver(episode, kind="resend")

    def _send_sms_alert(self, episode: Episode) -> None:
        """Attempt the optional SMS alert after the email attempt is stored.

        The SMS channel is fully self-contained: it skips itself when it is
        unconfigured or unverified, and every failure is caught here so no SMS
        outcome can change the email attempt, block resend or fail the request.
        """

        if self.sms is None:
            return
        try:
            self.sms.send_episode_alert(episode)
        except Exception:  # boundary: the optional channel must never leak
            self._logger.exception(
                "SMS alert raised at the boundary for episode %s", episode.id
            )

    def _deliver(self, episode: Episode, *, kind: str) -> SendResult:
        started = time.perf_counter()
        recipient = self.config.email.recipient
        provider = self.provider

        if provider is None:
            return self._record_failure(
                episode=episode,
                kind=kind,
                provider_name="",
                recipient="",
                error=UNCONFIGURED_ERROR,
                started=started,
            )

        segment_count = None
        try:
            content = compose_episode_email(
                episode=episode,
                base_url=self.config.base_url,
                service_name=self.config.service_name,
            )
            segment_count = 1
            message = OutgoingEmail(
                sender=self.config.email.sender,
                recipient=recipient,
                subject=content.subject,
                body=content.body,
            )
            provider.send(message)
        except Exception as exc:
            return self._record_failure(
                episode=episode,
                kind=kind,
                provider_name=getattr(provider, "name", ""),
                recipient=recipient,
                error=sanitize_error(exc) or "Échec de l'envoi de l'email.",
                started=started,
                segment_count=segment_count,
            )

        attempt = self.deliveries.record(
            channel="email",
            kind=kind,
            status="sent",
            episode_id=episode.id,
            provider=getattr(provider, "name", ""),
            recipient=recipient,
            segment_count=segment_count,
            duration_ms=_elapsed_ms(started),
            created_at=format_timestamp(self._clock()),
        )
        self._logger.info(
            "Email %s sent for episode %s to %s via %s",
            kind,
            episode.id,
            mask_recipient("email", recipient),
            getattr(provider, "name", ""),
        )
        return SendResult(status="sent", attempt=attempt, error=None)

    def _record_failure(
        self,
        *,
        episode: Episode,
        kind: str,
        provider_name: str,
        recipient: str,
        error: str,
        started: float,
        segment_count: int | None = None,
    ) -> SendResult:
        message = sanitize_error(error) or "Échec de l'envoi de l'email."
        if recipient:
            message = message.replace(
                recipient, mask_recipient("email", recipient)
            )
        attempt = self.deliveries.record(
            channel="email",
            kind=kind,
            status="failed",
            episode_id=episode.id,
            provider=provider_name,
            recipient=recipient,
            segment_count=segment_count,
            duration_ms=_elapsed_ms(started),
            error=message,
            created_at=format_timestamp(self._clock()),
        )
        self._logger.error(
            "Email %s failed for episode %s to %s via %s: %s",
            kind,
            episode.id,
            mask_recipient("email", recipient) or "***",
            provider_name or "none",
            message,
        )
        return SendResult(status="failed", attempt=attempt, error=message)


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000.0)))
