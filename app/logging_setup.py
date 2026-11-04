"""Structured JSON logging to stdout.

Log records are one JSON object per line.  Page content is untrusted data and
must never be written to logs: known content-bearing field names are dropped
defensively, and values are bounded so a large body cannot flood the log.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

#: Field names (case-insensitive) that must never be emitted.
SENSITIVE_FIELDS = frozenset(
    {
        "authorization",
        "body",
        "content",
        "cookie",
        "csrf_token",
        "current_excerpt",
        "excerpt",
        "html",
        "page",
        "page_content",
        "password",
        "raw",
        "raw_html",
        "reference_excerpt",
        "secret",
        "snapshot",
        "token",
    }
)

_MAX_VALUE_LENGTH = 200


class StructuredLogger:
    """Write bounded JSON log lines to a stream (stdout by default)."""

    def __init__(self, stream: TextIO | None = None):
        self._stream = stream

    @property
    def stream(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stdout

    def log(self, event: str, *, level: str = "info", **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": level,
            "event": event,
        }
        for key, value in fields.items():
            if key.lower() in SENSITIVE_FIELDS:
                continue
            record[key] = _bounded(value)
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            line = json.dumps(
                {
                    "ts": record["ts"],
                    "level": level,
                    "event": event,
                    "log_error": "unserializable fields",
                },
                ensure_ascii=False,
            )
        self.stream.write(line + "\n")
        self.stream.flush()


def _bounded(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    if len(text) > _MAX_VALUE_LENGTH:
        return text[:_MAX_VALUE_LENGTH] + "…"
    return text
