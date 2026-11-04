"""Provider-independent SMS adapter.

The only adapter shipped here is a generic HTTP-JSON one. It builds a request
from the operator-supplied URL, method, authentication mode and JSON body
template. Changing the free SMS provider therefore only requires configuration
changes; no code in episode or detection logic needs to change.

No network access happens at import or construction time. The transport is
injectable so the adapter can be tested completely offline.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .config import (
    DEFAULT_SMS_BODY_TEMPLATE,
    MAX_SMS_MAX_RESPONSE_BYTES,
    SMS_AUTH_MODES,
    SMS_METHODS,
    SMS_REQUIRED_PLACEHOLDERS,
    SMS_PLACEHOLDERS,
    ConfigError,
    SmsConfig,
)
from .util import mask_sms, redact_text

MAX_ERROR_DETAIL_LENGTH = 500
RESPONSE_EXCERPT_LENGTH = 300
USER_AGENT = "episode-alerts-sms/1.0"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"

_PLACEHOLDER_RE = re.compile(r"\{\{(to|sender|message)\}\}")


class SmsProviderError(Exception):
    """A provider call failed with a stable, non-secret error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SmsSendResult:
    """A successful provider call."""

    provider: str
    status_code: int
    duration_seconds: float
    response_excerpt: str


@dataclass(frozen=True)
class HttpRequest:
    """The request prepared by the adapter and handed to the transport."""

    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    timeout: float
    max_response_bytes: int


@dataclass(frozen=True)
class HttpResponse:
    """The minimal response shape the transport must return."""

    status_code: int
    body: bytes = b""


HttpTransport = Callable[[HttpRequest], HttpResponse]


@runtime_checkable
class SmsProvider(Protocol):
    """Minimal contract used by the alerting logic."""

    @property
    def name(self) -> str:
        ...

    @property
    def recipient(self) -> str:
        ...

    def send(self, message: str) -> SmsSendResult:
        ...


class _TransportError(Exception):
    """Transport failure converted into a provider error by the adapter."""


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """Default transport with a bounded response read.

    Connection errors are raised without the endpoint URL so the adapter can
    redact them before they are persisted or displayed.
    """

    http_request = Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    limit = request.max_response_bytes

    try:
        with urlopen(http_request, timeout=request.timeout) as response:
            body = response.read(limit + 1)
            return HttpResponse(status_code=int(response.status), body=body[:limit])
    except HTTPError as exc:
        body = exc.read(limit + 1)
        return HttpResponse(status_code=int(exc.code), body=body[:limit])
    except TimeoutError as exc:
        raise _TransportError("timeout") from exc
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise _TransportError("timeout") from exc
        raise _TransportError(str(exc.reason)) from exc
    except OSError as exc:
        raise _TransportError(str(exc)) from exc


def load_sms_provider(
    config: SmsConfig,
    *,
    transport: HttpTransport | None = None,
) -> HttpJsonSmsProvider | None:
    """Build the configured adapter, or return ``None`` when SMS is inactive.

    No provider is selected by default: an incomplete SMS block never creates
    an adapter and therefore never performs a network call.
    """

    if not config.configured:
        return None
    return HttpJsonSmsProvider(config, transport=transport)


class HttpJsonSmsProvider:
    """Generic HTTP-JSON SMS adapter driven entirely by configuration."""

    def __init__(
        self,
        config: SmsConfig,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        if not config.configured:
            raise ConfigError("SMS configuration is incomplete")

        self._config = config
        self._transport = transport or urllib_transport
        self._method = config.method.strip().upper()
        self._auth_mode = config.auth_mode.strip().lower()
        self._auth_header = config.auth_header.strip() or "Authorization"
        self._auth_query_param = config.auth_query_param.strip() or "api_key"

        if self._method not in SMS_METHODS:
            raise ConfigError(
                "sms.method must be one of " + ", ".join(sorted(SMS_METHODS))
            )
        if self._auth_mode not in SMS_AUTH_MODES:
            raise ConfigError(
                "sms.auth_mode must be one of "
                + ", ".join(sorted(SMS_AUTH_MODES))
            )
        if not _is_header_name(self._auth_header):
            raise ConfigError("sms.auth_header is not a valid HTTP header name")
        if self._auth_mode in {"bearer", "header"} and _has_control(
            config.credential
        ):
            raise ConfigError(
                "sms.credential must not contain control characters"
            )
        if config.max_response_bytes <= 0:
            raise ConfigError("sms.max_response_bytes must be positive")
        self._max_response_bytes = min(
            int(config.max_response_bytes), MAX_SMS_MAX_RESPONSE_BYTES
        )
        self._validate_url(config.url)
        self._body_template = _parse_body_template(config.body_template)
        if config.success_json_value and not config.success_json_field:
            raise ConfigError(
                "sms.success_json_value requires sms.success_json_field"
            )

    @property
    def name(self) -> str:
        return self._config.provider

    @property
    def recipient(self) -> str:
        return self._config.recipient

    @property
    def masked_recipient(self) -> str:
        return mask_sms(self._config.recipient)

    def send(self, message: str) -> SmsSendResult:
        """Send an already composed message and return the provider result.

        Every transport, HTTP-status and success-predicate failure becomes a
        :class:`SmsProviderError` whose detail never contains the credential,
        recipient or full endpoint.
        """

        text = "" if message is None else str(message)
        if not text.strip():
            raise SmsProviderError("sms_empty_message", "The SMS message is empty")

        url, headers = self._request_target()
        body = self._render_body(text)
        request = HttpRequest(
            method=self._method,
            url=url,
            headers=tuple(headers.items()),
            body=body,
            timeout=float(self._config.timeout_seconds),
            max_response_bytes=self._max_response_bytes,
        )

        started = time.monotonic()
        try:
            response = self._transport(request)
        except SmsProviderError:
            raise
        except TimeoutError as exc:
            raise SmsProviderError(
                "sms_timeout", "The SMS provider timed out"
            ) from exc
        except _TransportError as exc:
            if str(exc) == "timeout":
                raise SmsProviderError(
                    "sms_timeout", "The SMS provider timed out"
                ) from exc
            raise SmsProviderError(
                "sms_network_error",
                self._redact(f"Cannot reach the SMS provider: {exc}"),
            ) from exc
        except Exception as exc:  # boundary: never leak transport details
            raise SmsProviderError(
                "sms_network_error",
                self._redact(f"Cannot reach the SMS provider: {exc}"),
            ) from exc

        duration_seconds = max(0.0, time.monotonic() - started)
        raw_body = bytes(response.body or b"")[: self._max_response_bytes]
        excerpt = self._excerpt(raw_body)

        if not 200 <= int(response.status_code) <= 299:
            detail = f"HTTP {int(response.status_code)}"
            if excerpt:
                detail = f"{detail}: {excerpt}"
            raise SmsProviderError("sms_http_error", detail)

        if self._config.success_json_field:
            self._check_success_predicate(raw_body)

        return SmsSendResult(
            provider=self._config.provider,
            status_code=int(response.status_code),
            duration_seconds=duration_seconds,
            response_excerpt=excerpt,
        )

    def _request_target(self) -> tuple[str, dict[str, str]]:
        headers = {
            "Content-Type": JSON_CONTENT_TYPE,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        url = self._config.url
        credential = self._config.credential

        if self._auth_mode == "bearer":
            headers[self._auth_header] = f"Bearer {credential}"
        elif self._auth_mode == "header":
            headers[self._auth_header] = credential
        elif self._auth_mode == "query":
            url = _with_query_parameter(
                url, self._auth_query_param, credential
            )
        return url, headers

    def _render_body(self, message: str) -> bytes:
        replacements = {
            "{{to}}": self._config.recipient,
            "{{sender}}": self._config.sender,
            "{{message}}": message,
        }
        rendered = _substitute(self._body_template, replacements)
        return json.dumps(
            rendered, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def _check_success_predicate(self, raw_body: bytes) -> None:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmsProviderError(
                "sms_invalid_response",
                "The SMS provider response is not valid JSON",
            ) from exc

        field = self._config.success_json_field
        found, actual = _lookup_json_path(payload, field)
        if not found:
            raise SmsProviderError(
                "sms_rejected",
                f"The SMS provider response has no field {field!r}",
            )

        expected = self._config.success_json_value
        if expected:
            if not _predicate_matches(actual, expected):
                raise SmsProviderError(
                    "sms_rejected",
                    "The SMS provider did not confirm the send",
                )
        elif not actual:
            raise SmsProviderError(
                "sms_rejected",
                "The SMS provider did not confirm the send",
            )

    def _excerpt(self, raw_body: bytes) -> str:
        excerpt = self._redact(raw_body.decode("utf-8", errors="replace"))
        if len(excerpt) > RESPONSE_EXCERPT_LENGTH:
            excerpt = excerpt[: RESPONSE_EXCERPT_LENGTH - 3].rstrip() + "..."
        return excerpt

    def _redact(self, value: object) -> str:
        text = redact_text(value)
        for secret in (self._config.credential, self._config.recipient):
            if secret:
                text = text.replace(secret, "***")
        if len(text) > MAX_ERROR_DETAIL_LENGTH:
            text = text[: MAX_ERROR_DETAIL_LENGTH - 3].rstrip() + "..."
        return text

    def _validate_url(self, url: str) -> None:
        if _has_control(url):
            raise ConfigError("sms.url must not contain control characters")
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise ConfigError(
                "sms.url must be an absolute http or https URL"
            ) from exc
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            raise ConfigError("sms.url must be an absolute http or https URL")


def _parse_body_template(raw_template: str) -> Any:
    template = raw_template.strip() or DEFAULT_SMS_BODY_TEMPLATE
    try:
        parsed = json.loads(template)
    except json.JSONDecodeError as exc:
        raise ConfigError("sms.body_template must be valid JSON") from exc
    if not isinstance(parsed, (dict, list)):
        raise ConfigError("sms.body_template must be a JSON object or array")

    present: set[str] = set()
    _collect_placeholders(parsed, present)
    missing = [item for item in SMS_REQUIRED_PLACEHOLDERS if item not in present]
    if missing:
        raise ConfigError(
            "sms.body_template must contain " + " and ".join(missing)
        )
    return parsed


def _collect_placeholders(value: Any, present: set[str]) -> None:
    if isinstance(value, str):
        for placeholder in SMS_PLACEHOLDERS:
            if placeholder in value:
                present.add(placeholder)
    elif isinstance(value, list):
        for item in value:
            _collect_placeholders(item, present)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_placeholders(item, present)


def _substitute(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(
            lambda match: replacements["{{" + match.group(1) + "}}"],
            value,
        )
    if isinstance(value, list):
        return [_substitute(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _substitute(item, replacements)
            for key, item in value.items()
        }
    return value


def _with_query_parameter(url: str, name: str, value: str) -> str:
    parts = urlsplit(url)
    pairs = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key != name
    ]
    pairs.append((name, value))
    query = urlencode(pairs)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _lookup_json_path(payload: Any, path: str) -> tuple[bool, Any]:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
            else:
                return False, None
        else:
            return False, None
    return True, current


def _predicate_matches(actual: Any, expected: str) -> bool:
    try:
        parsed_expected = json.loads(expected)
    except json.JSONDecodeError:
        parsed_expected = expected
    if actual == parsed_expected:
        return True
    return str(actual) == expected


def _is_header_name(value: str) -> bool:
    if not value:
        return False
    allowed = set(
        "!#$%&'*+-.^_`|~"
        "0123456789"
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )
    return all(character in allowed for character in value)


def _has_control(value: str) -> bool:
    return any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    )
