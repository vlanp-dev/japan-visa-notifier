"""Minimal WSGI application and French HTML rendering helpers."""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs

from .config import Config
from .logging_setup import StructuredLogger

Handler = Callable[..., "Response"]

_SAFE_PATH_LENGTH = 200


def escape(value: Any) -> str:
    """Escape a value for HTML text and quoted attributes."""

    return html.escape("" if value is None else str(value), quote=True)


def parse_cookies(header: str | None) -> dict[str, str]:
    if not header:
        return {}
    cookie: SimpleCookie[str] = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:  # http.cookies raises a few unrelated parser errors
        return {}
    return {name: morsel.value for name, morsel in cookie.items()}


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: Mapping[str, list[str]]
    headers: Mapping[str, str]
    form: Mapping[str, list[str]]
    cookies: Mapping[str, str]
    csrf_token: str | None
    environ: Mapping[str, Any]

    @classmethod
    def from_environ(cls, environ: Mapping[str, Any]) -> "Request":
        return cls(
            method=str(environ.get("REQUEST_METHOD", "GET")).upper(),
            path=str(environ.get("PATH_INFO") or "/"),
            query=parse_qs(
                str(environ.get("QUERY_STRING") or ""),
                keep_blank_values=True,
            ),
            headers=_headers_from_environ(environ),
            form=environ.get("detection.form") or {},
            cookies=parse_cookies(environ.get("HTTP_COOKIE")),
            csrf_token=environ.get("detection.csrf_token"),
            environ=environ,
        )

    def query_one(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def form_one(self, name: str, default: str | None = None) -> str | None:
        values = self.form.get(name)
        return values[0] if values else default


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)


def html_response(
    status: int,
    document: str,
    headers: list[tuple[str, str]] | None = None,
) -> Response:
    merged = [("Content-Type", "text/html; charset=utf-8")]
    if headers:
        merged.extend(headers)
    return Response(
        status=status,
        body=document.encode("utf-8"),
        headers=merged,
    )


def json_response(
    status: int,
    payload: Mapping[str, Any],
    headers: list[tuple[str, str]] | None = None,
) -> Response:
    merged = [("Content-Type", "application/json; charset=utf-8")]
    if headers:
        merged.extend(headers)
    return Response(
        status=status,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=merged,
    )


def render_page(title: str, content: str) -> str:
    """Render a small French page; ``content`` must already be escaped."""

    return (
        "<!doctype html>\n"
        '<html lang="fr">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{content}\n"
        "</body>\n"
        "</html>\n"
    )


@dataclass(frozen=True)
class _Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler


class Application:
    """Small route-table based WSGI application."""

    def __init__(
        self,
        config: Config,
        logger: StructuredLogger | None = None,
    ):
        self.config = config
        self.logger = logger or StructuredLogger()
        self._routes: list[_Route] = []

    def add_route(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append(
            _Route(
                method=method.upper(),
                pattern=re.compile(f"^(?:{pattern})$"),
                handler=handler,
            )
        )

    def __call__(self, environ: Mapping[str, Any], start_response: Callable) -> list[bytes]:
        started = time.monotonic()
        request = Request.from_environ(environ)
        status = 500
        try:
            response, status = self._dispatch(request)
        except Exception as exc:  # never leak page content or tracebacks
            self.logger.log(
                "http_error",
                level="error",
                method=request.method,
                path=_safe_path(request.path),
                error_type=type(exc).__name__,
            )
            response = html_response(
                500,
                render_page(
                    "Erreur interne",
                    "<h1>Erreur interne</h1>"
                    "<p>Le service a rencontré une erreur inattendue.</p>",
                ),
            )
            status = 500

        headers = list(response.headers)
        headers.append(("Content-Length", str(len(response.body))))
        start_response(status_line(response.status), headers)

        self.logger.log(
            "http_request",
            method=request.method,
            path=_safe_path(request.path),
            status=response.status,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            remote=_remote_address(environ),
        )
        return [response.body]

    def _dispatch(self, request: Request) -> tuple[Response, int]:
        allowed: set[str] = set()
        path = request.path if request.path.startswith("/") else "/"
        for route in self._routes:
            match = route.pattern.fullmatch(path)
            if match is None:
                continue
            allowed.add(route.method)
            if route.method != request.method:
                continue
            response = route.handler(request, **match.groupdict())
            return response, response.status

        if allowed:
            response = html_response(
                405,
                render_page(
                    "Méthode non autorisée",
                    "<h1>Méthode non autorisée</h1>"
                    "<p>Cette action n'est pas disponible avec cette méthode.</p>",
                ),
                headers=[("Allow", ", ".join(sorted(allowed)))],
            )
        else:
            response = html_response(
                404,
                render_page(
                    "Page introuvable",
                    "<h1>Page introuvable</h1>"
                    "<p>La page demandée n'existe pas.</p>",
                ),
            )
        return response, response.status


def status_line(status: int) -> str:
    reasons = {
        200: "OK",
        303: "See Other",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Content Too Large",
        415: "Unsupported Media Type",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }
    return f"{status} {reasons.get(status, 'Unknown')}"


def _headers_from_environ(environ: Mapping[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            headers[key[5:].lower().replace("_", "-")] = str(value)
    if environ.get("CONTENT_TYPE"):
        headers["content-type"] = str(environ["CONTENT_TYPE"])
    if environ.get("CONTENT_LENGTH"):
        headers["content-length"] = str(environ["CONTENT_LENGTH"])
    return headers


def _safe_path(path: str) -> str:
    cleaned = "".join(
        ch if 32 <= ord(ch) < 127 else "_" for ch in str(path)[:_SAFE_PATH_LENGTH]
    )
    return cleaned or "/"


def _remote_address(environ: Mapping[str, Any]) -> str:
    return str(environ.get("REMOTE_ADDR") or "-")[:64]
