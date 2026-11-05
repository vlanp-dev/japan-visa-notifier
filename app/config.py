"""Configuration loading from a JSON document plus environment overrides.

The JSON document is deliberately small and explicit. Every value can be
overridden with an environment variable using ``ALERTS__`` and double
underscores for nesting, for example::

    ALERTS__SERVER__PORT=9000
    ALERTS__EMAIL__PASSWORD=...

Unknown keys are ignored so a newer configuration file can still be read by an
older deployment. Secrets are never exposed by :meth:`Config.redacted`.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .util import mask_sms, redact_mapping, redact_url, sanitize_error

DEFAULT_CONFIG_PATH = "config/app.json"
ENV_PREFIX = "ALERTS__"
CONFIG_FILE_ENV = "ALERTS_CONFIG_FILE"

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

DEFAULT_SMS_BODY_TEMPLATE = (
    '{"to": "{{to}}", "sender": "{{sender}}", "message": "{{message}}"}'
)
SMS_REQUIRED_PLACEHOLDERS = ("{{to}}", "{{message}}")
SMS_PLACEHOLDERS = SMS_REQUIRED_PLACEHOLDERS + ("{{sender}}",)
SMS_METHODS = frozenset({"POST", "PUT", "PATCH"})
SMS_AUTH_MODES = frozenset({"bearer", "header", "query"})
DEFAULT_SMS_AUTH_HEADER = "Authorization"
DEFAULT_SMS_AUTH_QUERY_PARAM = "api_key"
DEFAULT_SMS_MAX_RESPONSE_BYTES = 64 * 1024
MIN_SMS_MAX_RESPONSE_BYTES = 256
MAX_SMS_MAX_RESPONSE_BYTES = 1024 * 1024

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_PHONE_ALLOWED_RE = re.compile(r"^\+?[0-9 ().-]+$")


class ConfigError(ValueError):
    """Raised when configuration is structurally invalid or unsafe."""


@dataclass(frozen=True)
class EmailConfig:
    provider: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 25
    username: str = ""
    password: str = ""
    sender: str = ""
    recipient: str = ""
    use_starttls: bool = True
    timeout_seconds: float = 10.0
    outbox_path: str = ""

    @property
    def configured(self) -> bool:
        if not (self.sender and self.recipient):
            return False
        if self.provider == "outbox":
            return bool(self.outbox_path)
        return bool(self.smtp_host)

    def redacted(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "username": self.username,
            "password": "***" if self.password else "",
            "sender": self.sender,
            "recipient": self.recipient,
            "use_starttls": self.use_starttls,
            "timeout_seconds": self.timeout_seconds,
            "outbox_path": self.outbox_path,
        }


@dataclass(frozen=True, repr=False)
class SmsConfig:
    """Optional SMS provider settings.

    No provider is selected by default. The channel is considered configured
    only when every required value is present; an operator who wants SMS must
    choose and verify a free provider themselves.
    """

    provider: str = ""
    url: str = ""
    method: str = "POST"
    auth_mode: str = "bearer"
    auth_header: str = DEFAULT_SMS_AUTH_HEADER
    auth_query_param: str = DEFAULT_SMS_AUTH_QUERY_PARAM
    sender: str = ""
    recipient: str = ""
    credential: str = ""
    body_template: str = ""
    timeout_seconds: float = 10.0
    max_response_bytes: int = DEFAULT_SMS_MAX_RESPONSE_BYTES
    success_json_field: str = ""
    success_json_value: str = ""

    @property
    def configured(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.provider,
                self.url,
                self.sender,
                self.recipient,
                self.credential,
            )
        )

    @property
    def is_configured(self) -> bool:
        return self.configured

    def redacted(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "url": redact_url(self.url),
            "method": self.method,
            "auth_mode": self.auth_mode,
            "auth_header": self.auth_header,
            "auth_query_param": self.auth_query_param,
            "sender": self.sender,
            "recipient": mask_sms(self.recipient) if self.recipient else "",
            "credential": "***" if self.credential else "",
            "body_template_configured": bool(self.body_template),
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "success_predicate_configured": bool(self.success_json_field),
            "configured": self.configured,
        }

    def __repr__(self) -> str:
        recipient = mask_sms(self.recipient) if self.recipient else ""
        credential = "***" if self.credential else ""
        return (
            "SmsConfig("
            f"provider={self.provider!r}, url={redact_url(self.url)!r}, "
            f"recipient={recipient!r}, credential={credential!r}, "
            f"configured={self.configured!r})"
        )


@dataclass(frozen=True)
class Config:
    service_name: str = "Alertes épisodes"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    database_path: str = "var/alerts.sqlite3"
    log_level: str = "INFO"
    secret: str = ""
    secret_generated: bool = False
    base_url: str = ""
    base_url_configured: bool = False
    email: EmailConfig = field(default_factory=EmailConfig)
    source_path: str | None = None
    sms: SmsConfig = field(default_factory=SmsConfig)
    sms_configuration_error: str | None = None

    @property
    def email_configured(self) -> bool:
        return self.email.configured

    @property
    def sms_configured(self) -> bool:
        return self.sms.configured and self.sms_configuration_error is None

    @property
    def secure_cookies(self) -> bool:
        return self.environment == "production"

    def redacted(self) -> dict[str, Any]:
        document = {
            "service": {
                "name": self.service_name,
                "environment": self.environment,
                "base_url": self.base_url,
            },
            "server": {
                "host": self.host,
                "port": self.port,
                "secret": "***" if self.secret else "",
            },
            "database": {"path": self.database_path},
            "logging": {"level": self.log_level},
            "email": self.email.redacted(),
            "sms": self.sms.redacted(),
        }
        return redact_mapping(document)

    def with_overrides(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        database_path: str | None = None,
        log_level: str | None = None,
    ) -> "Config":
        changes: dict[str, Any] = {}
        if host is not None:
            changes["host"] = _as_text(host, "server.host")
        if port is not None:
            changes["port"] = _as_int(port, "server.port", minimum=1, maximum=65535)
        if database_path is not None:
            changes["database_path"] = _as_text(database_path, "database.path")
        if log_level is not None:
            changes["log_level"] = _as_log_level(log_level)
        if not self.base_url_configured and (
            "host" in changes or "port" in changes
        ):
            changes["base_url"] = (
                f"http://{changes.get('host', self.host)}:"
                f"{changes.get('port', self.port)}"
            )
        return replace(self, **changes)


def load_config(
    path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Load configuration.

    ``path`` wins over ``ALERTS_CONFIG_FILE``. When neither is provided,
    ``config/app.json`` is used only if it exists; otherwise built-in defaults
    are returned so the service can start without any file.
    """

    env = dict(os.environ if environ is None else environ)
    explicit_path = path if path is not None else env.get(CONFIG_FILE_ENV)
    source_path: Path | None = None

    if explicit_path:
        source_path = Path(explicit_path)
        if not source_path.is_file():
            raise ConfigError(f"Configuration file not found: {source_path}")
    else:
        candidate = Path(DEFAULT_CONFIG_PATH)
        if candidate.is_file():
            source_path = candidate

    document: dict[str, Any] = {}
    if source_path is not None:
        try:
            loaded = json.loads(source_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"Cannot read configuration file: {source_path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Configuration file is not valid JSON: {source_path}"
            ) from exc
        if not isinstance(loaded, dict):
            raise ConfigError("Configuration document must be a JSON object")
        document = loaded

    _apply_environment(document, env)
    return _build_config(document, str(source_path) if source_path else None)


def _apply_environment(document: dict[str, Any], env: Mapping[str, str]) -> None:
    for name, raw_value in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        parts = [part.lower() for part in name[len(ENV_PREFIX) :].split("__")]
        if not parts or any(not part for part in parts):
            raise ConfigError(f"Invalid environment override name: {name}")
        node: dict[str, Any] = document
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                child = {}
                node[part] = child
            if not isinstance(child, dict):
                raise ConfigError(
                    f"Environment override conflicts with configuration: {name}"
                )
            node = child
        node[parts[-1]] = raw_value


def _build_config(document: dict[str, Any], source_path: str | None) -> Config:
    service = _section(document, "service")
    server = _section(document, "server")
    database = _section(document, "database")
    logging_section = _section(document, "logging")
    email_section = _section(document, "email")

    service_name = _as_text(
        service.get("name", "Alertes épisodes"), "service.name", max_length=100
    )
    environment = _as_text(
        service.get("environment", "development"),
        "service.environment",
        max_length=50,
    )
    host = _as_text(server.get("host", "127.0.0.1"), "server.host", max_length=255)
    port = _as_int(server.get("port", 8000), "server.port", minimum=1, maximum=65535)
    database_path = _as_text(
        database.get("path", "var/alerts.sqlite3"),
        "database.path",
        max_length=1000,
    )
    log_level = _as_log_level(logging_section.get("level", "INFO"))
    base_url = _as_base_url(
        service.get("base_url", "") or f"http://{host}:{port}", "service.base_url"
    )

    raw_secret = server.get("secret", "")
    if raw_secret is None:
        raw_secret = ""
    if not isinstance(raw_secret, str):
        raise ConfigError("server.secret must be a string")
    secret = raw_secret
    secret_generated = False
    if not secret:
        secret = secrets.token_hex(32)
        secret_generated = True
    elif len(secret.strip()) < 16:
        raise ConfigError("server.secret must contain at least 16 characters")

    email = _build_email(email_section)
    sms, sms_configuration_error = _build_optional_sms(document)

    return Config(
        service_name=service_name,
        environment=environment,
        host=host,
        port=port,
        database_path=database_path,
        log_level=log_level,
        secret=secret,
        secret_generated=secret_generated,
        base_url=base_url,
        base_url_configured=bool(service.get("base_url")),
        email=email,
        sms=sms,
        sms_configuration_error=sms_configuration_error,
        source_path=source_path,
    )


def _build_email(section: Mapping[str, Any]) -> EmailConfig:
    outbox_path = _as_text(
        section.get("outbox_path", ""), "email.outbox_path", max_length=1000
    )
    smtp_host = _as_text(
        section.get("smtp_host", ""), "email.smtp_host", max_length=255
    )
    raw_provider = section.get("provider", "")
    if raw_provider is None:
        raw_provider = ""
    if not isinstance(raw_provider, str):
        raise ConfigError("email.provider must be a string")
    provider = raw_provider.strip().lower()
    if not provider:
        if smtp_host:
            provider = "smtp"
        elif outbox_path:
            provider = "outbox"
        else:
            provider = "smtp"
    if provider not in {"smtp", "outbox"}:
        raise ConfigError("email.provider must be one of: smtp, outbox")

    smtp_port = _as_int(
        section.get("smtp_port", 25), "email.smtp_port", minimum=1, maximum=65535
    )
    username = _as_text(
        section.get("username", ""), "email.username", max_length=255
    )
    password = section.get("password", "")
    if password is None:
        password = ""
    if not isinstance(password, str):
        raise ConfigError("email.password must be a string")
    sender = _as_text(section.get("sender", ""), "email.sender", max_length=320)
    recipient = _as_text(
        section.get("recipient", ""), "email.recipient", max_length=320
    )
    use_starttls = _as_bool(
        section.get("use_starttls", True), "email.use_starttls"
    )
    timeout_seconds = _as_float(
        section.get("timeout_seconds", 10.0),
        "email.timeout_seconds",
        minimum=0.1,
        maximum=120.0,
    )

    for field_name, address in (("sender", sender), ("recipient", recipient)):
        if address and not _looks_like_email(address):
            raise ConfigError(f"email.{field_name} is not a valid email address")

    any_value = bool(
        smtp_host or username or sender or recipient or outbox_path or raw_provider
    )
    if provider == "outbox":
        if any_value and not (outbox_path and sender and recipient):
            raise ConfigError(
                "email provider 'outbox' requires outbox_path, sender and "
                "recipient to be configured together"
            )
    elif any_value and not (smtp_host and sender and recipient):
        raise ConfigError(
            "email requires smtp_host, sender and recipient to be configured together"
        )

    return EmailConfig(
        provider=provider,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        username=username,
        password=password,
        sender=sender,
        recipient=recipient,
        use_starttls=use_starttls,
        timeout_seconds=timeout_seconds,
        outbox_path=outbox_path,
    )


def _build_optional_sms(
    document: Mapping[str, Any],
) -> tuple[SmsConfig, str | None]:
    """Build the optional SMS configuration without making it fatal.

    The SMS channel is optional: a malformed, partial or invalid ``sms``
    section must never prevent the mandatory email channel from starting.
    The reason is retained on the configuration so the channel can be shown as
    explicitly inactive instead of failing silently.
    """

    try:
        return _build_sms(_section(document, "sms")), None
    except ConfigError as exc:
        return SmsConfig(), sanitize_error(exc) or "Configuration SMS invalide."


def _build_sms(section: Mapping[str, Any]) -> SmsConfig:
    provider = _as_text(
        section.get("provider", ""), "sms.provider", max_length=100
    )
    url = _as_text(section.get("url", ""), "sms.url", max_length=2048)
    method = _as_text(
        section.get("method", "POST"), "sms.method", max_length=10
    ).upper()
    auth_mode = _as_text(
        section.get("auth_mode", "bearer"), "sms.auth_mode", max_length=20
    ).lower()
    auth_header = (
        _as_text(
            section.get("auth_header", DEFAULT_SMS_AUTH_HEADER),
            "sms.auth_header",
            max_length=100,
        )
        or DEFAULT_SMS_AUTH_HEADER
    )
    auth_query_param = (
        _as_text(
            section.get("auth_query_param", DEFAULT_SMS_AUTH_QUERY_PARAM),
            "sms.auth_query_param",
            max_length=100,
        )
        or DEFAULT_SMS_AUTH_QUERY_PARAM
    )
    sender = _as_text(section.get("sender", ""), "sms.sender", max_length=64)
    recipient = _as_text(
        section.get("recipient", ""), "sms.recipient", max_length=40
    )
    credential_value = section.get("credential", "")
    if credential_value is None:
        credential_value = ""
    if not isinstance(credential_value, str):
        raise ConfigError("sms.credential must be a string")
    credential = credential_value.strip()
    if len(credential) > 4096:
        raise ConfigError("sms.credential is too long (maximum 4096)")
    body_template = _as_text(
        section.get("body_template", ""), "sms.body_template", max_length=8192
    )
    timeout_seconds = _as_float(
        section.get("timeout_seconds", 10.0),
        "sms.timeout_seconds",
        minimum=0.1,
        maximum=60.0,
    )
    max_response_bytes = _as_int(
        section.get("max_response_bytes", DEFAULT_SMS_MAX_RESPONSE_BYTES),
        "sms.max_response_bytes",
        minimum=MIN_SMS_MAX_RESPONSE_BYTES,
        maximum=MAX_SMS_MAX_RESPONSE_BYTES,
    )
    success_json_field = _as_text(
        section.get("success_json_field", ""),
        "sms.success_json_field",
        max_length=200,
    )
    success_json_value = _as_text(
        section.get("success_json_value", ""),
        "sms.success_json_value",
        max_length=500,
    )

    if section and not (provider and url and sender and recipient and credential):
        raise ConfigError(
            "sms requires provider, url, sender, recipient and credential "
            "to be configured together"
        )
    if method not in SMS_METHODS:
        raise ConfigError(
            "sms.method must be one of " + ", ".join(sorted(SMS_METHODS))
        )
    if auth_mode not in SMS_AUTH_MODES:
        raise ConfigError(
            "sms.auth_mode must be one of " + ", ".join(sorted(SMS_AUTH_MODES))
        )
    if not _HEADER_NAME_RE.match(auth_header):
        raise ConfigError("sms.auth_header is not a valid HTTP header name")
    if any(
        character.isspace() or ord(character) < 0x20
        for character in auth_query_param
    ):
        raise ConfigError(
            "sms.auth_query_param must not contain spaces or control characters"
        )
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in credential
    ):
        raise ConfigError("sms.credential must not contain control characters")
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in sender
    ):
        raise ConfigError("sms.sender must not contain control characters")
    if recipient and not _looks_like_phone(recipient):
        raise ConfigError("sms.recipient must be a single phone number")
    if url:
        _validate_http_url(url, "sms.url")
    if body_template:
        _validate_sms_body_template(body_template)
    if success_json_value and not success_json_field:
        raise ConfigError(
            "sms.success_json_value requires sms.success_json_field"
        )

    return SmsConfig(
        provider=provider,
        url=url,
        method=method,
        auth_mode=auth_mode,
        auth_header=auth_header,
        auth_query_param=auth_query_param,
        sender=sender,
        recipient=recipient,
        credential=credential,
        body_template=body_template,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        success_json_field=success_json_field,
        success_json_value=success_json_value,
    )


def _validate_http_url(value: str, name: str) -> None:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ConfigError(f"{name} must not contain control characters")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an absolute http or https URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{name} must be an absolute http or https URL")


def _validate_sms_body_template(value: str) -> None:
    try:
        parsed = json.loads(value)
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


def _collect_placeholders(value: Any, present: set[str]) -> None:
    if isinstance(value, str):
        for placeholder in SMS_PLACEHOLDERS:
            if placeholder in value:
                present.add(placeholder)
    elif isinstance(value, list):
        for item in value:
            _collect_placeholders(item, present)
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect_placeholders(item, present)


def _looks_like_phone(value: str) -> bool:
    if not _PHONE_ALLOWED_RE.match(value):
        return False
    digits = re.sub(r"\D", "", value)
    return 5 <= len(digits) <= 15


def _section(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"Configuration section '{name}' must be an object")
    return value


def _as_text(
    value: Any, name: str, *, max_length: int = 2048, allow_empty: bool = True
) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise ConfigError(f"{name} must not be empty")
    if len(result) > max_length:
        raise ConfigError(f"{name} is too long (maximum {max_length})")
    return result


def _as_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ConfigError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return result


def _as_float(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return result


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{name} must be a boolean")


def _as_log_level(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("logging.level must be a string")
    level = value.strip().upper()
    if level not in VALID_LOG_LEVELS:
        raise ConfigError(
            "logging.level must be one of " + ", ".join(sorted(VALID_LOG_LEVELS))
        )
    return level


def _looks_like_email(address: str) -> bool:
    if any(char.isspace() for char in address):
        return False
    local, separator, domain = address.partition("@")
    return bool(separator and local and domain and "@" not in domain)


def _as_base_url(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    candidate = value.strip()
    if not candidate:
        raise ConfigError(f"{name} must not be empty")
    if len(candidate) > 500:
        raise ConfigError(f"{name} is too long (maximum 500)")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{name} must be an absolute http or https URL")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{name} must not contain a query or fragment")
    return candidate.rstrip("/")
