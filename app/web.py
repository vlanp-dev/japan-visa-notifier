"""WSGI application, routing and middleware.

``create_app`` is the composition root: it wires configuration, SQLite,
repositories and routes. Every dependency can be replaced, which keeps the
application testable without a running server.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from typing import Callable, Iterable
from urllib.parse import quote

from . import __version__
from .alerting import AlertService, UNSET
from .config import Config
from .csrf import CSRF_FORM_FIELD, CSRF_HEADER_NAME, SESSION_COOKIE_NAME, CsrfProtector
from .db import Database
from .http import HTTPError, Request, Response
from .logging_config import get_logger
from .mailer import EmailProvider
from .repositories import (
    DeliveryAttemptRepository,
    EpisodeRepository,
    SmsChannelStateRepository,
)
from .sms_channel import SmsChannelService, UNSET as SMS_UNSET
from .util import format_timestamp, utcnow
from .views import (
    deliveries_page,
    episode_page,
    error_page,
    health_page,
    new_episode_page,
)

Handler = Callable[[Request], Response]

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "geolocation=(), camera=(), microphone=()"),
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    ),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
)

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    regex: re.Pattern[str]
    handler: Handler
    specificity: int = 0


class Application:
    """A small WSGI callable with explicit routing and CSRF protection."""

    def __init__(
        self,
        config: Config,
        database: Database,
        episodes: EpisodeRepository,
        deliveries: DeliveryAttemptRepository,
        alerts: AlertService,
        *,
        logger=None,
    ) -> None:
        self.config = config
        self.database = database
        self.episodes = episodes
        self.deliveries = deliveries
        self.alerts = alerts
        self.csrf = CsrfProtector(config.secret)
        self._logger = logger or get_logger("web")
        self._routes: list[Route] = []
        self.add_route("GET", "/", self._health_page)
        self.add_route("GET", "/health", self._health_json)
        self.add_route("GET", "/healthz", self._health_json)
        self.add_route("GET", "/episodes/new", self._new_episode)
        self.add_route("POST", "/episodes", self._create_episode)
        self.add_route("GET", "/episodes/{episode_id}", self._episode_detail)
        self.add_route(
            "POST",
            "/episodes/{episode_id}/acknowledge",
            self._acknowledge_episode,
        )
        self.add_route(
            "POST", "/episodes/{episode_id}/resend", self._resend_episode
        )
        self.add_route("GET", "/deliveries", self._deliveries)

    def add_route(self, method: str, pattern: str, handler: Handler) -> None:
        method = method.upper()
        if not method.isalpha():
            raise ValueError("HTTP method must contain only letters")
        if not callable(handler):
            raise ValueError("route handler must be callable")
        self._routes.append(
            Route(
                method,
                pattern,
                _compile_pattern(pattern),
                handler,
                _specificity(pattern),
            )
        )

    def __call__(self, environ, start_response):
        started = time.perf_counter()
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO") or "/")
        session_id = ""
        new_session = False

        try:
            request = Request.from_environ(environ)
            session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
            if not self._is_valid_session_id(session_id):
                session_id = self.csrf.new_session_id()
                new_session = True
            request = request.with_session(session_id)

            if request.method in UNSAFE_METHODS:
                token = request.header(CSRF_HEADER_NAME) or request.form.get(
                    CSRF_FORM_FIELD, ""
                )
                if not self.csrf.is_valid(session_id, token):
                    raise HTTPError(
                        403, "Jeton de protection CSRF absent ou invalide."
                    )
            response = self._dispatch(request)
        except HTTPError as exc:
            response = Response.html(
                error_page(
                    status=exc.status,
                    message=exc.message,
                    service_name=self.config.service_name,
                ),
                status=exc.status,
                headers=exc.headers,
            )
        except Exception:
            self._logger.exception(
                "Unhandled error while serving %s %s", method, path
            )
            response = Response.html(
                error_page(
                    status=500,
                    message="Une erreur interne est survenue.",
                    service_name=self.config.service_name,
                ),
                status=500,
            )

        if not session_id:
            session_id = self.csrf.new_session_id()
            new_session = True

        response.headers = _merge_headers(
            response.headers, list(SECURITY_HEADERS)
        )
        if self.config.secure_cookies:
            response.headers.append(
                ("Strict-Transport-Security", "max-age=31536000")
            )
        body = response.body
        response.headers = _replace_header(
            response.headers, "Content-Length", str(len(body))
        )
        if new_session:
            response.headers.append(
                ("Set-Cookie", self._session_cookie(session_id))
            )

        status_text = _status_text(response.status)
        start_response(f"{response.status} {status_text}", response.headers)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._logger.info(
            "%s %s -> %s (%.1f ms)", method, path, response.status, elapsed_ms
        )
        if method == "HEAD":
            return [b""]
        return [body]

    def _dispatch(self, request: Request) -> Response:
        path_match = False
        allowed: set[str] = set()
        selected: tuple[Route, re.Match[str]] | None = None
        selected_key: tuple[int, int] = (-1, -1)
        for index, route in enumerate(self._routes):
            match = route.regex.match(request.path)
            if not match:
                continue
            path_match = True
            allowed.add(route.method)
            if route.method == "GET":
                allowed.add("HEAD")
            if route.method == request.method or (
                request.method == "HEAD" and route.method == "GET"
            ):
                # More specific routes win (``/episodes/new`` over
                # ``/episodes/{id}``); later registrations break ties so
                # callers can extend the application deterministically.
                key = (route.specificity, index)
                if key > selected_key:
                    selected = (route, match)
                    selected_key = key
        if selected is not None:
            route, match = selected
            return route.handler(request.with_path_params(match.groupdict()))
        if path_match:
            allow = ", ".join(sorted(allowed))
            raise HTTPError(
                405,
                "Méthode non autorisée pour cette ressource.",
                headers=[("Allow", allow)],
            )
        raise HTTPError(404, "Ressource introuvable.")

    def _health_page(self, request: Request) -> Response:
        database_ok = self._database_ok()
        if database_ok:
            try:
                episode_count = self.episodes.count()
                attempt_count = self.deliveries.count()
                episodes = self.episodes.list_recent(5)
                attempts = self.deliveries.list_recent(10)
            except Exception:
                self._logger.exception("Cannot read dashboard data")
                database_ok = False
        if not database_ok:
            episode_count = 0
            attempt_count = 0
            episodes = []
            attempts = []
        markup = health_page(
            config=self.config,
            database_ok=database_ok,
            episode_count=episode_count,
            attempt_count=attempt_count,
            episodes=episodes,
            attempts=attempts,
        )
        return Response.html(markup)

    def _health_json(self, request: Request) -> Response:
        database_ok = self._database_ok()
        payload = {
            "status": "ok" if database_ok else "error",
            "service": self.config.service_name,
            "environment": self.config.environment,
            "version": __version__,
            "database": {"status": "ok" if database_ok else "error"},
            "email": {
                "status": "configured"
                if self.config.email_configured
                else "unconfigured",
                "provider": (
                    self.config.email.provider
                    if self.config.email_configured
                    else ""
                ),
            },
            "time": format_timestamp(),
        }
        return Response.json(payload, status=200 if database_ok else 503)

    def _new_episode(self, request: Request) -> Response:
        markup = new_episode_page(
            config=self.config, csrf_token=self._token(request)
        )
        return Response.html(markup)

    def _create_episode(self, request: Request) -> Response:
        values = {
            "change_nature": request.form.get("change_nature", ""),
            "page_url": request.form.get("page_url", ""),
            "summary": request.form.get("summary", ""),
        }
        try:
            episode = self.alerts.create_episode(
                change_nature=values["change_nature"],
                page_url=values["page_url"],
                summary=values["summary"],
            )
        except ValueError as exc:
            markup = new_episode_page(
                config=self.config,
                csrf_token=self._token(request),
                error=str(exc),
                values=values,
            )
            return Response.html(markup, status=400)
        location = (
            f"/episodes/{quote(episode.id, safe='')}?notice=created"
        )
        return Response.redirect(location)

    def _episode_detail(self, request: Request) -> Response:
        episode = self.alerts.get_episode(request.path_params["episode_id"])
        if episode is None:
            raise HTTPError(404, "Épisode introuvable.")
        attempts = self.deliveries.list_for_episode(episode.id)
        markup = episode_page(
            config=self.config,
            episode=episode,
            attempts=attempts,
            csrf_token=self._token(request),
            notice=request.query_one("notice"),
        )
        return Response.html(markup)

    def _acknowledge_episode(self, request: Request) -> Response:
        episode = self.alerts.acknowledge(request.path_params["episode_id"])
        if episode is None:
            raise HTTPError(404, "Épisode introuvable.")
        location = (
            f"/episodes/{quote(episode.id, safe='')}?notice=acknowledged"
        )
        return Response.redirect(location)

    def _resend_episode(self, request: Request) -> Response:
        episode = self.alerts.get_episode(request.path_params["episode_id"])
        if episode is None:
            raise HTTPError(404, "Épisode introuvable.")
        result = self.alerts.resend(episode)
        notice = "resent_sent" if result.status == "sent" else "resent_failed"
        location = (
            f"/episodes/{quote(episode.id, safe='')}?notice={notice}"
        )
        return Response.redirect(location)

    def _deliveries(self, request: Request) -> Response:
        attempts = self.deliveries.list_recent(200)
        return Response.html(
            deliveries_page(config=self.config, attempts=attempts)
        )

    def _token(self, request: Request) -> str:
        return self.csrf.token_for(request.session_id)

    def _database_ok(self) -> bool:
        try:
            return bool(self.database.check())
        except Exception:
            self._logger.exception("Database health check failed")
            return False

    @staticmethod
    def _is_valid_session_id(value: str) -> bool:
        return bool(value and _SESSION_ID_RE.match(value))

    def _session_cookie(self, session_id: str) -> str:
        cookie = SimpleCookie()
        cookie[SESSION_COOKIE_NAME] = session_id
        morsel = cookie[SESSION_COOKIE_NAME]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Lax"
        if self.config.secure_cookies:
            morsel["secure"] = True
        return morsel.OutputString()


def create_app(
    config: Config,
    *,
    database: Database | None = None,
    episodes: EpisodeRepository | None = None,
    deliveries: DeliveryAttemptRepository | None = None,
    alerts: AlertService | None = None,
    email_provider: EmailProvider | None = UNSET,  # type: ignore[assignment]
    sms: SmsChannelService | None = None,
    sms_provider=SMS_UNSET,
    logger=None,
    clock: Callable[[], datetime] = utcnow,
    extra_routes: Iterable[tuple[str, str, Handler]] = (),
) -> Application:
    """Build the WSGI application with optional injected dependencies."""

    database = database if database is not None else Database(config.database_path)
    database.initialize()
    if episodes is None:
        episodes = EpisodeRepository(database, now=clock)
    if deliveries is None:
        deliveries = DeliveryAttemptRepository(database, now=clock)
    if alerts is None:
        if sms is None:
            sms = SmsChannelService(
                config,
                deliveries,
                SmsChannelStateRepository(database, now=clock),
                provider=sms_provider,
                logger=logger,
                clock=clock,
            )
        alerts = AlertService(
            config,
            episodes,
            deliveries,
            provider=email_provider,
            sms=sms,
            logger=logger,
            clock=clock,
        )
    app = Application(
        config,
        database,
        episodes,
        deliveries,
        alerts,
        logger=logger,
    )
    for method, pattern, handler in extra_routes:
        app.add_route(method, pattern, handler)
    return app


def _specificity(pattern: str) -> int:
    """Count literal path segments so specific routes win over parameters."""

    return sum(
        1
        for segment in pattern.strip("/").split("/")
        if segment and not (segment.startswith("{") and segment.endswith("}"))
    )


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern.startswith("/"):
        raise ValueError("route pattern must start with '/'")
    if pattern == "/":
        return re.compile(r"^/$")
    if "?" in pattern or "#" in pattern:
        raise ValueError("route pattern must not contain a query or fragment")
    pieces: list[str] = []
    seen: set[str] = set()
    for segment in pattern.strip("/").split("/"):
        if not segment:
            raise ValueError("route pattern contains an empty segment")
        if segment.startswith("{") and segment.endswith("}"):
            name = segment[1:-1]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("route parameter has an invalid name")
            if name in seen:
                raise ValueError("route parameter names must be unique")
            seen.add(name)
            pieces.append(f"(?P<{name}>[^/]+)")
        else:
            pieces.append(re.escape(segment))
    return re.compile("^/" + "/".join(pieces) + "/?$")


def _status_text(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Unknown"


def _merge_headers(
    base: list[tuple[str, str]], extra: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Merge headers, letting ``extra`` replace a base header with the same name."""

    replaced = {name.lower() for name, _ in extra}
    result = [(name, value) for name, value in base if name.lower() not in replaced]
    result.extend(extra)
    return result


def _replace_header(
    headers: list[tuple[str, str]], name: str, value: str
) -> list[tuple[str, str]]:
    result = [(key, val) for key, val in headers if key.lower() != name.lower()]
    result.append((name, value))
    return result
