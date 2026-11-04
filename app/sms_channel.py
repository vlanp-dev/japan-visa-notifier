"""Persisted verification and delivery for the optional SMS channel.

SMS is optional and must never degrade the mandatory email channel. This
module owns three things:

* a fingerprint of every provider-relevant setting (the credential is only
  represented by a hash), so a configuration change invalidates verification;
* the four explicit channel states shown to the operator;
* a service whose :meth:`status`, :meth:`send_test` and
  :meth:`send_episode_alert` methods share one provider, one composer and the
  common delivery-attempt repository.

No provider is selected by default, and no network call happens while the
channel is unconfigured or unverified.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from urllib.parse import quote

from .config import Config, ConfigError, SmsConfig
from .logging_config import get_logger
from .repositories import (
    DeliveryAttempt,
    DeliveryAttemptRepository,
    Episode,
    SmsChannelState,
    SmsChannelStateRepository,
)
from .sms_message import compose_sms
from .sms_provider import load_sms_provider
from .util import format_timestamp, mask_sms, sanitize_error, utcnow

UNSET = object()

SMS_STATE_UNCONFIGURED = "unconfigured"
SMS_STATE_UNVERIFIED = "unverified"
SMS_STATE_VERIFIED = "verified"
SMS_STATE_FAILING = "failing"

SMS_STATE_LABELS = {
    SMS_STATE_UNCONFIGURED: "Inactif — non configuré",
    SMS_STATE_UNVERIFIED: "Inactif — non vérifié",
    SMS_STATE_VERIFIED: "Actif et vérifié",
    SMS_STATE_FAILING: "Actif mais en échec",
}

UNCONFIGURED_ERROR = "Canal SMS non configuré : envoi impossible."
INVALID_CONFIGURATION_ERROR = "Configuration SMS invalide : envoi impossible."
TEST_NATURE = "Test du canal SMS"
SMS_MESSAGE_ERROR = "Échec de l'envoi du SMS."


def sms_config_fingerprint(config: SmsConfig) -> str:
    """Return a stable digest of every provider-relevant setting.

    The credential is hashed on its own before being included, so it is never
    part of a fingerprint an operator could read or compare.
    """

    credential = config.credential or ""
    material = {
        "provider": config.provider,
        "url": config.url,
        "method": config.method,
        "auth_mode": config.auth_mode,
        "auth_header": config.auth_header,
        "auth_query_param": config.auth_query_param,
        "sender": config.sender,
        "recipient": config.recipient,
        "body_template": config.body_template,
        "timeout_seconds": config.timeout_seconds,
        "max_response_bytes": config.max_response_bytes,
        "success_json_field": config.success_json_field,
        "success_json_value": config.success_json_value,
        "credential_sha256": (
            hashlib.sha256(credential.encode("utf-8")).hexdigest()
            if credential
            else ""
        ),
    }
    serialized = json.dumps(
        material, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SmsChannelStatus:
    """Read-only view of the current channel state for the operator."""

    state: str
    configured: bool
    provider: str
    recipient_masked: str
    fingerprint: str
    verified_at: str | None = None
    failing_since: str | None = None
    last_test_at: str | None = None
    last_test_status: str | None = None
    last_test_error: str | None = None
    last_alert_at: str | None = None
    last_alert_status: str | None = None
    last_alert_error: str | None = None
    configuration_error: str | None = None

    @property
    def label(self) -> str:
        return SMS_STATE_LABELS.get(
            self.state, SMS_STATE_LABELS[SMS_STATE_UNCONFIGURED]
        )

    @property
    def active(self) -> bool:
        """Whether the channel may send real alerts (verified or failing)."""

        return self.state in {SMS_STATE_VERIFIED, SMS_STATE_FAILING}

    @property
    def is_active(self) -> bool:
        return self.active

    @property
    def last_error(self) -> str | None:
        if self.state == SMS_STATE_FAILING and self.last_alert_error:
            return self.last_alert_error
        if self.last_test_status == "failed":
            return self.last_test_error
        if self.last_alert_status == "failed":
            return self.last_alert_error
        return None

    @property
    def last_error_at(self) -> str | None:
        if self.state == SMS_STATE_FAILING and self.last_alert_error:
            return self.last_alert_at
        if self.last_test_status == "failed":
            return self.last_test_at
        if self.last_alert_status == "failed":
            return self.last_alert_at
        return None


@dataclass(frozen=True)
class SmsChannelResult:
    """Outcome of one test or alert attempt."""

    status: str
    attempt: DeliveryAttempt | None
    error: str | None

    @property
    def sent(self) -> bool:
        return self.status == "sent"


class SmsChannelService:
    """Deliver SMS tests and episode alerts through the configured provider."""

    def __init__(
        self,
        config: Config,
        deliveries: DeliveryAttemptRepository,
        states: SmsChannelStateRepository,
        *,
        provider=UNSET,  # type: ignore[assignment]
        logger=None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.deliveries = deliveries
        self.states = states
        self._clock = clock
        self._logger = logger or get_logger("sms")
        self._provider_error: str | None = None
        if provider is UNSET:
            try:
                provider = load_sms_provider(config.sms)
            except ConfigError as exc:
                self._provider_error = (
                    sanitize_error(exc) or INVALID_CONFIGURATION_ERROR
                )
                provider = None
        self.provider = provider

    def status(self) -> SmsChannelStatus:
        """Derive the explicit state from configuration and persisted state.

        Calling this method also persists the fingerprint. A configuration
        change therefore invalidates the previous verification even before the
        next test or alert is attempted.
        """

        sms = self.config.sms
        fingerprint = sms_config_fingerprint(sms)
        configuration_error = self._configuration_error()
        row: SmsChannelState | None = None
        if sms.configured and self._provider_error is None:
            row = self.states.ensure(fingerprint)

        if configuration_error is not None:
            state = SMS_STATE_UNCONFIGURED
        elif row is None or row.fingerprint != fingerprint:
            state = SMS_STATE_UNVERIFIED
        elif row.verified_at is None:
            state = SMS_STATE_UNVERIFIED
        elif row.failing_since is not None:
            state = SMS_STATE_FAILING
        else:
            state = SMS_STATE_VERIFIED

        return SmsChannelStatus(
            state=state,
            configured=bool(sms.configured),
            provider=sms.provider,
            recipient_masked=mask_sms(sms.recipient) if sms.recipient else "",
            fingerprint=fingerprint,
            verified_at=row.verified_at if row else None,
            failing_since=row.failing_since if row else None,
            last_test_at=row.last_test_at if row else None,
            last_test_status=row.last_test_status if row else None,
            last_test_error=row.last_test_error if row else None,
            last_alert_at=row.last_alert_at if row else None,
            last_alert_status=row.last_alert_status if row else None,
            last_alert_error=row.last_alert_error if row else None,
            configuration_error=configuration_error,
        )

    def send_test(self) -> SmsChannelResult:
        """Send an explicit test message and make success or failure explicit.

        Incomplete or invalid configuration is refused without any provider
        call, but the refusal is still recorded as a failed ``test`` attempt.
        """

        started = time.perf_counter()
        fingerprint = sms_config_fingerprint(self.config.sms)
        refusal = self._configuration_error()
        if refusal is not None:
            return self._record_failure(
                kind="test",
                episode=None,
                fingerprint=fingerprint,
                error=refusal,
                started=started,
                segment_count=None,
            )

        try:
            composed = compose_sms(
                TEST_NATURE,
                f"{self.config.base_url}/",
            )
        except Exception as exc:  # boundary: composition errors are explicit
            return self._record_failure(
                kind="test",
                episode=None,
                fingerprint=fingerprint,
                error=sanitize_error(exc) or SMS_MESSAGE_ERROR,
                started=started,
                segment_count=None,
            )

        return self._send(
            kind="test",
            episode=None,
            fingerprint=fingerprint,
            composed=composed,
            started=started,
        )

    def send_episode_alert(self, episode: Episode) -> SmsChannelResult:
        """Attempt the SMS alert for an episode when the channel is active.

        Unconfigured and unverified channels are skipped without a provider
        call and without creating an attempt. A verified or already failing
        channel is attempted; every failure is contained and recorded.
        """

        status = self.status()
        if not status.active:
            self._logger.debug(
                "SMS alert skipped for episode %s: %s",
                episode.id,
                status.state,
            )
            return SmsChannelResult(status="skipped", attempt=None, error=None)

        started = time.perf_counter()
        try:
            composed = compose_sms(
                episode.change_nature,
                episode.page_url,
                acknowledgement_url=self._acknowledgement_url(episode),
            )
        except Exception as exc:  # boundary: composition errors are explicit
            return self._record_failure(
                kind="alert",
                episode=episode,
                fingerprint=status.fingerprint,
                error=sanitize_error(exc) or SMS_MESSAGE_ERROR,
                started=started,
                segment_count=None,
            )

        return self._send(
            kind="alert",
            episode=episode,
            fingerprint=status.fingerprint,
            composed=composed,
            started=started,
        )

    def _send(
        self,
        *,
        kind: str,
        episode: Episode | None,
        fingerprint: str,
        composed,
        started: float,
    ) -> SmsChannelResult:
        provider = self.provider
        recipient = self.config.sms.recipient
        try:
            provider.send(composed.text)
        except Exception as exc:  # boundary: never leak provider details
            return self._record_failure(
                kind=kind,
                episode=episode,
                fingerprint=fingerprint,
                error=sanitize_error(exc) or SMS_MESSAGE_ERROR,
                started=started,
                segment_count=composed.segment_count,
            )

        moment = format_timestamp(self._clock())
        attempt = self.deliveries.record(
            channel="sms",
            kind=kind,
            status="sent",
            episode_id=episode.id if episode else None,
            provider=self._provider_name(),
            recipient=recipient,
            segment_count=composed.segment_count,
            duration_ms=_elapsed_ms(started),
            created_at=moment,
        )
        self._record_state(
            kind=kind,
            fingerprint=fingerprint,
            status="sent",
            at=moment,
        )
        self._logger.info(
            "SMS %s sent for %s to %s via %s",
            kind,
            episode.id if episode else "test",
            mask_sms(recipient) if recipient else "***",
            self._provider_name() or "none",
        )
        return SmsChannelResult(status="sent", attempt=attempt, error=None)

    def _record_failure(
        self,
        *,
        kind: str,
        episode: Episode | None,
        fingerprint: str,
        error: str,
        started: float,
        segment_count: int | None,
    ) -> SmsChannelResult:
        recipient = self.config.sms.recipient
        message = sanitize_error(error) or SMS_MESSAGE_ERROR
        if recipient:
            message = message.replace(recipient, mask_sms(recipient))

        moment = format_timestamp(self._clock())
        attempt = self.deliveries.record(
            channel="sms",
            kind=kind,
            status="failed",
            episode_id=episode.id if episode else None,
            provider=self._provider_name(),
            recipient=recipient,
            segment_count=segment_count,
            duration_ms=_elapsed_ms(started),
            error=message,
            created_at=moment,
        )
        self._record_state(
            kind=kind,
            fingerprint=fingerprint,
            status="failed",
            error=message,
            at=moment,
        )
        self._logger.error(
            "SMS %s failed for %s to %s via %s: %s",
            kind,
            episode.id if episode else "test",
            mask_sms(recipient) if recipient else "***",
            self._provider_name() or "none",
            message,
        )
        return SmsChannelResult(status="failed", attempt=attempt, error=message)

    def _record_state(
        self,
        *,
        kind: str,
        fingerprint: str,
        status: str,
        error: str | None = None,
        at: str | None = None,
    ) -> None:
        moment = at or format_timestamp(self._clock())
        if kind == "test":
            self.states.record_test(
                fingerprint=fingerprint, status=status, error=error, at=moment
            )
        else:
            self.states.record_alert(
                fingerprint=fingerprint, status=status, error=error, at=moment
            )

    def _configuration_error(self) -> str | None:
        if self._provider_error is not None:
            return self._provider_error
        if not self.config.sms.configured:
            return UNCONFIGURED_ERROR
        if self.provider is None:
            return INVALID_CONFIGURATION_ERROR
        return None

    def _provider_name(self) -> str:
        return getattr(self.provider, "name", "") or self.config.sms.provider

    def _acknowledgement_url(self, episode: Episode) -> str:
        detail_url = (
            f"{self.config.base_url}/episodes/{quote(episode.id, safe='')}"
        )
        return f"{detail_url}#acquitter"


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000.0)))
