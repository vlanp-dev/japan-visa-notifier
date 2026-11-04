"""Minimal HTTP request/response primitives for the WSGI application."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from typing import Any, Mapping
from urllib.parse import parse_qs

MAX_BODY_BYTES = 64 * 1024
MAX_FORM_FIELDS = 100


class HTTPError(Exception):
    def __init__(self, status: int, message: str, *, headers: list[tuple[str, str]] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or []


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: Mapping[str, list[str]]
    headers: Mapping[str, str]
    cookies: Mapping[str, str]
    body: bytes
    form: Mapping[str, str]
    remote_addr: str
    session_id: str = ""
    path_params: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def query_one(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    @classmethod
    def from_environ(cls, environ: Mapping[str, Any]) -> "Request":
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        if not method or not method.isalpha():
            raise HTTPError(400, "Méthode HTTP invalide")

        raw_path = environ.get("PATH_INFO") or "/"
        path = str(raw_path)
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 2048 or "\x00" in path:
            raise HTTPError(414, "Chemin de requête trop long")

        try:
            query = parse_qs(
                str(environ.get("QUERY_STRING") or ""),
                keep_blank_values=True,
                max_num_fields=MAX_FORM_FIELDS,
            )
        except ValueError as exc:
            raise HTTPError(400, "Chaîne de requête invalide") from exc

        headers: dict[str, str] = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                headers[key[5:].lower().replace("_", "-")] = str(value)
            elif key in {"CONTENT_TYPE", "CONTENT_LENGTH"}:
                headers[key.lower().replace("_", "-")] = str(value or "")

        cookies: dict[str, str] = {}
        raw_cookie = headers.get("cookie", "")
        if raw_cookie:
            parsed = SimpleCookie()
            try:
                parsed.load(raw_cookie)
            except CookieError as exc:
                raise HTTPError(400, "En-tête Cookie invalide") from exc
            cookies = {name: morsel.value for name, morsel in parsed.items()}

        body = _read_body(environ, headers)

        form: dict[str, str] = {}
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if body and content_type == "application/x-www-form-urlencoded":
            try:
                decoded = body.decode("utf-8", errors="strict")
                parsed_form = parse_qs(
                    decoded, keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise HTTPError(400, "Formulaire invalide") from exc
            form = {key: values[0] for key, values in parsed_form.items() if values}
        elif body and content_type not in {"", "application/x-www-form-urlencoded"}:
            raise HTTPError(415, "Type de contenu non pris en charge")

        return cls(
            method=method,
            path=path,
            query=query,
            headers=headers,
            cookies=cookies,
            body=body,
            form=form,
            remote_addr=str(environ.get("REMOTE_ADDR") or ""),
        )

    def with_session(self, session_id: str) -> "Request":
        return Request(
            method=self.method,
            path=self.path,
            query=self.query,
            headers=self.headers,
            cookies=self.cookies,
            body=self.body,
            form=self.form,
            remote_addr=self.remote_addr,
            session_id=session_id,
            path_params=self.path_params,
        )

    def with_path_params(self, params: Mapping[str, str]) -> "Request":
        return Request(
            method=self.method,
            path=self.path,
            query=self.query,
            headers=self.headers,
            cookies=self.cookies,
            body=self.body,
            form=self.form,
            remote_addr=self.remote_addr,
            session_id=self.session_id,
            path_params=dict(params),
        )


def _read_body(
    environ: Mapping[str, Any], headers: Mapping[str, str]
) -> bytes:
    raw_length = headers.get("content-length", "").strip()
    length = 0
    if raw_length:
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HTTPError(400, "Content-Length invalide") from exc
        if length < 0:
            raise HTTPError(400, "Content-Length invalide")
        if length > MAX_BODY_BYTES:
            raise HTTPError(413, "Corps de requête trop volumineux")
    elif environ.get("REQUEST_METHOD", "GET").upper() in {"POST", "PUT", "PATCH"}:
        # Without a declared length we cannot safely parse a form; treat the
        # body as empty rather than reading until EOF.
        length = 0

    stream = environ.get("wsgi.input")
    if length == 0 or stream is None:
        return b""
    try:
        body = stream.read(length)
    except OSError as exc:
        raise HTTPError(400, "Lecture du corps impossible") from exc
    if len(body) > MAX_BODY_BYTES:
        raise HTTPError(413, "Corps de requête trop volumineux")
    return body


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def html(
        cls, markup: str, *, status: int = 200, headers: list[tuple[str, str]] | None = None
    ) -> "Response":
        return cls(
            status=status,
            body=markup.encode("utf-8"),
            headers=[
                ("Content-Type", "text/html; charset=utf-8"),
                *(headers or []),
            ],
        )

    @classmethod
    def json(
        cls, payload: Mapping[str, Any], *, status: int = 200
    ) -> "Response":
        body = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        return cls(
            status=status,
            body=body,
            headers=[("Content-Type", "application/json; charset=utf-8")],
        )

    @classmethod
    def text(
        cls, message: str, *, status: int = 200
    ) -> "Response":
        return cls(
            status=status,
            body=message.encode("utf-8"),
            headers=[("Content-Type", "text/plain; charset=utf-8")],
        )

    @classmethod
    def redirect(cls, location: str, *, status: int = 303) -> "Response":
        return cls(status=status, body=b"", headers=[("Location", location)])
