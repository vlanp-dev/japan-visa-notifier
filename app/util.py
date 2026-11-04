"""Small shared helpers: timestamps, masking and text redaction."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

MAX_ERROR_LENGTH = 1000

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|credential|api[_-]?key|authorization)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|credential|api[_-]?key|authorization)"
    r"\s*[:=]\s*\S+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""

    return datetime.now(timezone.utc)


def format_timestamp(value: datetime | None = None) -> str:
    """Format an aware datetime as a compact ISO-8601 UTC string."""

    moment = value or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(key or ""))


def redact_mapping(value: Any, key: str = "") -> Any:
    """Return a deep copy of ``value`` with secret-looking values replaced.

    Empty values are preserved so an operator can tell the difference between
    "not configured" and "configured but hidden".
    """

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for child_key, child_value in value.items():
            child_key = str(child_key)
            if is_sensitive_key(child_key) and child_value not in (None, ""):
                result[child_key] = "***"
            else:
                result[child_key] = redact_mapping(child_value, child_key)
        return result
    if isinstance(value, list):
        return [redact_mapping(item, key) for item in value]
    if isinstance(value, tuple):
        return [redact_mapping(item, key) for item in value]
    return value


def redact_text(value: Any) -> str:
    """Remove common secret patterns from free-form text before storing it."""

    text = str(value).replace("\x00", "").strip()
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=***", text)
    return text


def sanitize_error(value: Any) -> str:
    """Redact and bound an error message that will be stored or displayed."""

    if value is None:
        return ""
    return redact_text(value)[:MAX_ERROR_LENGTH]


def mask_email(address: str) -> str:
    local, separator, domain = (address or "").strip().partition("@")
    if not separator or not domain:
        return "***"
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def mask_sms(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return "***"
    return f"***{digits[-2:]}"


def mask_recipient(channel: str, recipient: str) -> str:
    if not recipient:
        return ""
    if channel == "email":
        return mask_email(recipient)
    if channel == "sms":
        return mask_sms(recipient)
    return "***"


def redact_url(value: str) -> str:
    """Return a URL safe to display: credentials, query and fragment hidden."""

    text = (value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "***"

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    if parts.scheme and netloc:
        result = f"{parts.scheme}://{netloc}{parts.path}"
    elif parts.scheme:
        result = f"{parts.scheme}:{parts.path}"
    else:
        result = parts.path
    if parts.query:
        result += "?***"
    if parts.fragment:
        result += "#***"
    return result or "***"
