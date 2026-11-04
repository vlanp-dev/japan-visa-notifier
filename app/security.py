"""Security helpers: security headers and double-submit CSRF protection."""

from __future__ import annotations

import hmac
import re
import secrets
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs

from .web import (
    Request,
    Response,
    escape,
    html_response,
    parse_cookies,
    render_page,
    status_line,
)

CSRF_COOKIE = "detection_csrf"
CSRF_FIELD = "csrf_token"
CSRF_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
MAX_FORM_FIELDS = 100

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    ),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
    ("X-Permitted-Cross-Domain-Policies", "none"),
)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def valid_token_shape(token: str | None) -> bool:
    return bool(token) and CSRF_TOKEN_PATTERN.fullmatch(token) is not None


def csrf_cookie_header(token: str, secure: bool) -> str:
    parts = [
        f"{CSRF_COOKIE}={token}",
        "Path=/",
        "SameSite=Lax",
        "HttpOnly",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def tokens_match(cookie_token: str | None, form_token: str | None) -> bool:
    if not valid_token_shape(cookie_token) or not valid_token_shape(form_token):
        return False
    return hmac.compare_digest(cookie_token, form_token)


def current_csrf_token(environ: Mapping[str, Any]) -> str:
    """Return the CSRF token established for this request."""

    token = environ.get("detection.csrf_token")
    if isinstance(token, str) and valid_token_shape(token):
        return token
    return new_csrf_token()


class SecurityHeadersMiddleware:
    """Add conservative security headers to every response."""

    def __init__(self, application: Callable):
        self.application = application

    def __call__(self, environ: Mapping[str, Any], start_response: Callable) -> list[bytes]:
        def secured_start_response(
            status: str,
            headers: list[tuple[str, str]],
            exc_info: Any = None,
        ) -> Callable:
            headers = list(headers)
            present = {name.lower() for name, _ in headers}
            for name, value in SECURITY_HEADERS:
                if name.lower() not in present:
                    headers.append((name, value))
            if "cache-control" not in present:
                headers.append(("Cache-Control", "no-store"))
            return start_response(status, headers, exc_info)

        return self.application(environ, secured_start_response)


class CsrfMiddleware:
    """Double-submit cookie CSRF protection.

    A token is generated when the cookie is missing, stored in an
    ``HttpOnly``/``SameSite=Lax`` cookie, and must be echoed in the
    ``csrf_token`` form field of every unsafe request.  Parsed form values are
    placed in ``environ['detection.form']`` for the wrapped application.
    """

    def __init__(self, application: Callable, max_request_bytes: int):
        self.application = application
        self.max_request_bytes = max_request_bytes

    def __call__(self, environ: Mapping[str, Any], start_response: Callable) -> list[bytes]:
        cookies = parse_cookies(environ.get("HTTP_COOKIE"))
        existing = cookies.get(CSRF_COOKIE)
        token = existing if valid_token_shape(existing) else new_csrf_token()
        environ["detection.csrf_token"] = token
        cookie_to_send = None if existing == token else token

        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        if method in UNSAFE_METHODS:
            failure = self._prepare_form(environ, token)
            if failure is not None:
                return self._send(
                    failure,
                    environ,
                    start_response,
                    cookie_to_send,
                )

        def csrf_start_response(
            status: str,
            headers: list[tuple[str, str]],
            exc_info: Any = None,
        ) -> Callable:
            headers = list(headers)
            if cookie_to_send is not None:
                secure = str(environ.get("wsgi.url_scheme", "http")) == "https"
                headers.append(
                    ("Set-Cookie", csrf_cookie_header(cookie_to_send, secure))
                )
            return start_response(status, headers, exc_info)

        return self.application(environ, csrf_start_response)

    def _prepare_form(
        self, environ: Mapping[str, Any], token: str
    ) -> Response | None:
        content_type = str(environ.get("CONTENT_TYPE") or "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != FORM_CONTENT_TYPE:
            return _error_response(
                415,
                "Type de contenu non pris en charge",
                "Ce formulaire doit être envoyé en "
                "application/x-www-form-urlencoded.",
            )

        try:
            body = self._read_body(environ)
        except _BodyTooLarge:
            return _error_response(
                413,
                "Requête trop volumineuse",
                "Le corps de la requête dépasse la taille autorisée.",
            )
        except _BodyError:
            return _error_response(
                400,
                "Requête invalide",
                "Le corps de la requête est incomplet ou invalide.",
            )

        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return _error_response(
                400,
                "Requête invalide",
                "Le corps de la requête n'est pas un formulaire UTF-8 valide.",
            )

        try:
            form = parse_qs(
                text,
                keep_blank_values=True,
                max_num_fields=MAX_FORM_FIELDS,
            )
        except ValueError:
            return _error_response(
                400,
                "Requête invalide",
                "Le formulaire contient trop de champs.",
            )

        environ["detection.form"] = form
        submitted = form.get(CSRF_FIELD, [None])[0]
        if not tokens_match(token, submitted):
            return _error_response(
                403,
                "Jeton de sécurité invalide",
                "Le formulaire a expiré ou provient d'une origine non autorisée. "
                "Rechargez la page et réessayez.",
            )
        return None

    def _read_body(self, environ: Mapping[str, Any]) -> bytes:
        raw_length = environ.get("CONTENT_LENGTH")
        if raw_length in (None, ""):
            length = 0
        else:
            try:
                length = int(str(raw_length))
            except (TypeError, ValueError) as exc:
                raise _BodyError from exc
            if length < 0:
                raise _BodyError
        if length > self.max_request_bytes:
            raise _BodyTooLarge

        stream = environ.get("wsgi.input")
        if stream is None:
            raise _BodyError
        try:
            body = stream.read(length) if length else b""
        except (OSError, ValueError) as exc:
            raise _BodyError from exc
        if not isinstance(body, bytes) or len(body) != length:
            raise _BodyError
        return body

    def _send(
        self,
        response: Response,
        environ: Mapping[str, Any],
        start_response: Callable,
        token: str | None,
    ) -> list[bytes]:
        headers = list(response.headers)
        headers.append(("Content-Length", str(len(response.body))))
        if token is not None:
            secure = str(environ.get("wsgi.url_scheme", "http")) == "https"
            headers.append(("Set-Cookie", csrf_cookie_header(token, secure)))
        start_response(status_line(response.status), headers)
        return [response.body]


class _BodyError(Exception):
    pass


class _BodyTooLarge(_BodyError):
    pass


def _error_response(status: int, title: str, message: str) -> Response:
    return html_response(
        status,
        render_page(
            title,
            f"<h1>{escape(title)}</h1><p>{escape(message)}</p>",
        ),
    )


def form_value(request: Request, name: str, default: str | None = None) -> str | None:
    return request.form_one(name, default)
