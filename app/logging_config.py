"""Logging setup with a defensive redaction filter."""

from __future__ import annotations

import logging
import sys
from typing import IO, Any

from .util import redact_text

LOGGER_NAME = "app"


class RedactionFilter(logging.Filter):
    """Masks common secret patterns in log messages before they are emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact_text(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact_text(value) if isinstance(value, str) else value
                    for value in record.args
                )
        return True


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(RedactionFilter())

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False


def get_logger(name: str | None = None) -> logging.Logger:
    suffix = f".{name}" if name else ""
    return logging.getLogger(f"{LOGGER_NAME}{suffix}")


def log_startup(logger: logging.Logger, config: Any) -> None:
    logger.info(
        "Starting %s in %s on %s:%s (database=%s, email_configured=%s)",
        config.service_name,
        config.environment,
        config.host,
        config.port,
        config.database_path,
        config.email_configured,
    )
    if config.secret_generated:
        logger.warning(
            "server.secret is not configured; using an ephemeral signing secret. "
            "Sessions will be invalidated on restart."
        )
