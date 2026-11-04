"""Test support: temporary configuration and a tiny WSGI client."""

from __future__ import annotations

import io
import json
import logging
import tempfile
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from app.config import Config, load_config

TEST_SECRET = "test-secret-with-more-than-16-characters"


def config_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "service": {"name": "Alertes de test", "environment": "test"},
        "server": {"host": "127.0.0.1", "port": 8000, "secret": TEST_SECRET},
        "database": {"path": "var/test.sqlite3"},
        "logging": {"level": "INFO"},
    }
    document.update(overrides)
    return document


def write_config(directory: Path, document: Mapping[str, Any]) -> Config:
    path = directory / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_config(path=path, environ={})


def temporary_config(**overrides: Any) -> tuple[tempfile.TemporaryDirectory, Config]:
    holder = tempfile.TemporaryDirectory()
    directory = Path(holder.name)
    document = config_document(**overrides)
    document.setdefault("database", {})["path"] = str(directory / "alerts.sqlite3")
    return holder, write_config(directory, document)


def null_logger(name: str = "tests.null") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


@dataclass
class ClientResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str, default: str = "") -> str:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return default

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.text)


class WSGIClient:
    """Drive a WSGI app directly, preserving session cookies."""

    def __init__(self, app) -> None:
        self.app = app
        self.cookies: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        query: str = "",
        content_type: str | None = None,
    ) -> ClientResponse:
        environ: dict[str, Any] = {}
        setup_testing_defaults(environ)
        environ["REQUEST_METHOD"] = method.upper()
        environ["PATH_INFO"] = path
        environ["QUERY_STRING"] = query
        environ["wsgi.input"] = io.BytesIO(body)
        environ["CONTENT_LENGTH"] = str(len(body))
        if content_type:
            environ["CONTENT_TYPE"] = content_type
        for key, value in (headers or {}).items():
            header_key = key.upper().replace("-", "_")
            if header_key not in {"CONTENT_TYPE", "CONTENT_LENGTH"}:
                header_key = "HTTP_" + header_key
            environ[header_key] = value

        chosen_cookies = self.cookies if cookies is None else cookies
        if chosen_cookies:
            environ["HTTP_COOKIE"] = "; ".join(
                f"{name}={value}" for name, value in chosen_cookies.items()
            )

        captured: dict[str, Any] = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = list(response_headers)
            return None

        chunks = self.app(environ, start_response)
        response_body = b"".join(chunks) if chunks else b""
        raw_status = str(captured.get("status", "500 Internal Server Error"))
        status = int(raw_status.split(" ", 1)[0])
        response_headers = captured.get("headers", [])
        for key, value in response_headers:
            if key.lower() == "set-cookie":
                parsed = SimpleCookie()
                parsed.load(value)
                for name, morsel in parsed.items():
                    self.cookies[name] = morsel.value
        return ClientResponse(status, response_headers, response_body)

    def get(self, path: str, **kwargs: Any) -> ClientResponse:
        return self.request("GET", path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> ClientResponse:
        return self.request("HEAD", path, **kwargs)

    def post_form(
        self, path: str, form: Mapping[str, str] | None = None, **kwargs: Any
    ) -> ClientResponse:
        body = urlencode(list((form or {}).items())).encode("utf-8")
        return self.request(
            "POST",
            path,
            body=body,
            content_type="application/x-www-form-urlencoded",
            **kwargs,
        )
