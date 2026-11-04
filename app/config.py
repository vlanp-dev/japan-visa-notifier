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
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .util import redact_mapping

DEFAULT_CONFIG_PATH = "config/app.json"
ENV_PREFIX = "ALERTS__"
CONFIG_FILE_ENV = "ALERTS_CONFIG_FILE"

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


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

    @property
    def email_configured(self) -> bool:
        return self.email.configured

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
