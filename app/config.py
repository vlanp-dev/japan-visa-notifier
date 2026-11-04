"""Configuration loading and validation for the detection service.

Precedence is deliberately simple and explicit:

    built-in defaults  <  JSON configuration file  <  environment variables

Only the Python standard library is used so the service can run on a bare
Python 3.12 installation.  Invalid configuration is rejected at startup with a
single :class:`ConfigError` listing every problem found.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

DEFAULT_CONFIG_PATH = "config/app.json"

#: A stable, explicit identification of the robot.  Operators are expected to
#: replace the contact address with a real one when they deploy the service.
DEFAULT_USER_AGENT = (
    "DetectionCalendrier/1.0 (+contact: veille-calendrier@example.org)"
)

ENV_VARS: dict[str, str] = {
    "target_url": "DETECTION_TARGET_URL",
    "interval_seconds": "DETECTION_INTERVAL_SECONDS",
    "request_timeout_seconds": "DETECTION_REQUEST_TIMEOUT_SECONDS",
    "max_response_bytes": "DETECTION_MAX_RESPONSE_BYTES",
    "user_agent": "DETECTION_USER_AGENT",
    "database_path": "DETECTION_DATABASE_PATH",
    "operator_label": "DETECTION_OPERATOR_LABEL",
    "max_request_bytes": "DETECTION_MAX_REQUEST_BYTES",
    "host": "DETECTION_HOST",
    "port": "DETECTION_PORT",
}

_FIELD_NAMES = frozenset(ENV_VARS)


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or is invalid."""

    def __init__(self, errors: list[str] | str):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration."""

    target_url: str = "https://example.org/"
    interval_seconds: int = 900
    request_timeout_seconds: float = 15.0
    max_response_bytes: int = 2 * 1024 * 1024
    user_agent: str = DEFAULT_USER_AGENT
    database_path: str = "var/detection.sqlite3"
    operator_label: str = "opérateur"
    max_request_bytes: int = 1024 * 1024
    host: str = "127.0.0.1"
    port: int = 8080

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "Config":
        """Build a configuration from overrides applied on top of defaults.

        Keys use the snake_case names of the dataclass fields.  Unknown keys,
        wrong types and out-of-range values are reported together.
        """

        errors: list[str] = []
        prepared: dict[str, Any] = {}

        for key in values:
            if key not in _FIELD_NAMES:
                errors.append(f"unknown configuration key: {key!r}")

        raw: dict[str, Any] = {name: getattr(cls, name) for name in _FIELD_NAMES}
        for name, value in values.items():
            if name in _FIELD_NAMES:
                raw[name] = value

        prepared["target_url"] = _as_text(
            raw["target_url"], "target_url", errors, strip=True
        )
        prepared["interval_seconds"] = _as_int(
            raw["interval_seconds"], "interval_seconds", errors, minimum=1
        )
        prepared["request_timeout_seconds"] = _as_float(
            raw["request_timeout_seconds"],
            "request_timeout_seconds",
            errors,
            minimum=0.1,
        )
        prepared["max_response_bytes"] = _as_int(
            raw["max_response_bytes"], "max_response_bytes", errors, minimum=1
        )
        prepared["user_agent"] = _as_text(
            raw["user_agent"], "user_agent", errors, strip=True
        )
        prepared["database_path"] = _as_text(
            raw["database_path"], "database_path", errors, strip=True
        )
        prepared["operator_label"] = _as_text(
            raw["operator_label"], "operator_label", errors, strip=True
        )
        prepared["max_request_bytes"] = _as_int(
            raw["max_request_bytes"], "max_request_bytes", errors, minimum=1
        )
        prepared["host"] = _as_text(raw["host"], "host", errors, strip=True)
        prepared["port"] = _as_int(raw["port"], "port", errors, minimum=1)

        target_url = prepared.get("target_url")
        if isinstance(target_url, str) and target_url:
            _validate_target_url(target_url, errors)

        user_agent = prepared.get("user_agent")
        if isinstance(user_agent, str) and user_agent:
            if len(user_agent) > 512:
                errors.append("user_agent must be at most 512 characters")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in user_agent):
                errors.append("user_agent must not contain control characters")

        operator_label = prepared.get("operator_label")
        if isinstance(operator_label, str) and len(operator_label) > 120:
            errors.append("operator_label must be at most 120 characters")

        port = prepared.get("port")
        if isinstance(port, int) and not 1 <= port <= 65535:
            errors.append("port must be between 1 and 65535")

        if errors:
            raise ConfigError(errors)

        return cls(**prepared)

    @classmethod
    def load(
        cls,
        environ: Mapping[str, str] | None = None,
        config_path: str | None = None,
    ) -> "Config":
        """Load configuration from the JSON file and the environment.

        ``environ`` defaults to :data:`os.environ`.  Passing a mapping is useful
        in tests and keeps the loader independent from the process environment.
        """

        env = os.environ if environ is None else environ
        explicit_path = config_path or env.get("DETECTION_CONFIG")
        path = explicit_path or DEFAULT_CONFIG_PATH

        file_values: dict[str, Any] = {}
        candidate = Path(path)
        if candidate.exists():
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except OSError as exc:
                raise ConfigError(f"cannot read configuration file {path!r}: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"invalid JSON in configuration file {path!r}: {exc.msg}"
                ) from exc
            if not isinstance(loaded, dict):
                raise ConfigError(
                    f"configuration file {path!r} must contain a JSON object"
                )
            file_values = loaded
        elif explicit_path:
            raise ConfigError(f"configuration file not found: {explicit_path!r}")

        merged: dict[str, Any] = dict(file_values)
        for name, variable in ENV_VARS.items():
            if variable in env and env[variable] != "":
                merged[name] = env[variable]

        return cls.from_mapping(merged)


def load_config(
    environ: Mapping[str, str] | None = None,
    config_path: str | None = None,
) -> Config:
    """Convenience wrapper around :meth:`Config.load`."""

    return Config.load(environ=environ, config_path=config_path)


def _as_text(value: Any, name: str, errors: list[str], *, strip: bool) -> str:
    if not isinstance(value, str):
        errors.append(f"{name} must be a string")
        return ""
    text = value.strip() if strip else value
    if not text:
        errors.append(f"{name} must not be empty")
    return text


def _as_int(
    value: Any, name: str, errors: list[str], *, minimum: int
) -> int:
    if isinstance(value, bool):
        errors.append(f"{name} must be an integer")
        return minimum
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        try:
            number = int(value.strip(), 10)
        except (TypeError, ValueError):
            errors.append(f"{name} must be an integer")
            return minimum
    else:
        errors.append(f"{name} must be an integer")
        return minimum
    if number < minimum:
        errors.append(f"{name} must be at least {minimum}")
    return number


def _as_float(
    value: Any, name: str, errors: list[str], *, minimum: float
) -> float:
    if isinstance(value, bool):
        errors.append(f"{name} must be a number")
        return minimum
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            errors.append(f"{name} must be a number")
            return minimum
    else:
        errors.append(f"{name} must be a number")
        return minimum
    if not math.isfinite(number):
        errors.append(f"{name} must be a finite number")
        return minimum
    if number < minimum:
        errors.append(f"{name} must be at least {minimum}")
    return number


def _validate_target_url(url: str, errors: list[str]) -> None:
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        errors.append("target_url must not contain whitespace or control characters")
        return
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        errors.append("target_url must use the http or https scheme")
        return
    if not parsed.netloc or not parsed.hostname:
        errors.append("target_url must contain a host")
        return
    if parsed.username is not None or parsed.password is not None:
        errors.append("target_url must not contain credentials")
        return
    try:
        parsed.port
    except ValueError:
        errors.append("target_url contains an invalid port")

