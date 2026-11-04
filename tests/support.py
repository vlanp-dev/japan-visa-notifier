"""Small WSGI test client shared by the foundation tests."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Mapping
from urllib.parse import urlsplit


@dataclass
class ClientResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None


def request(
    app: Any,
    path: str = "/",
    *,
    method: str = "GET",
    body: bytes = b"",
    content_type: str | None = None,
    cookie: str | None = None,
    extra_environ: Mapping[str, Any] | None = None,
) -> ClientResponse:
    split = urlsplit(path)
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method.upper(),
        "PATH_INFO": split.path or "/",
        "QUERY_STRING": split.query,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "127.0.0.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if content_type is not None:
        environ["CONTENT_TYPE"] = content_type
    if body:
        environ["CONTENT_LENGTH"] = str(len(body))
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    if extra_environ:
        environ.update(extra_environ)

    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]], exc_info=None):
        captured["status"] = status
        captured["headers"] = list(headers)
        return lambda data: None

    chunks = list(app(environ, start_response))
    response_body = b"".join(chunks)
    headers = captured.get("headers", [])
    cookies: dict[str, str] = {}
    for name, value in headers:
        if name.lower() == "set-cookie":
            parsed: SimpleCookie[str] = SimpleCookie()
            parsed.load(value)
            for key, morsel in parsed.items():
                cookies[key] = morsel.value
    status = int(str(captured.get("status", "500")).split(" ", 1)[0])
    return ClientResponse(
        status=status,
        headers=headers,
        body=response_body,
        cookies=cookies,
    )
