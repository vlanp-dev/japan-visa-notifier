"""Disciplined retrieval of the target page.

The page and the transport are untrusted.  This module performs exactly one
logical ``GET`` per hop with a stable identifying ``User-Agent``, no cookies,
no authentication and no retries.  Only ``text/html`` and
``application/xhtml+xml`` responses are accepted; empty, oversized, undecodable
or unparseable bodies become typed control failures.

The configured timeout is a *total* budget for the whole retrieval, including
redirects, DNS resolution, connection setup, response headers and the body.
A small daemon worker performs the blocking I/O while the caller waits for the
deadline.  At the deadline the caller shuts the socket down, which unblocks a
slow header or body reader, and returns a :class:`FailureCategory.TIMEOUT`
failure.  A per-operation socket timeout alone is not sufficient: it bounds a
single idle ``recv``, not a server that drips one byte at a time.

Page content is never logged, executed or interpreted as an instruction, and
URLs found in the page are never followed.  Only ``Location`` headers of
HTTP(S) redirects are followed, up to the configured cap.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit

from ..config import Config
from ..logging_setup import StructuredLogger
from .canonical import (
    DEFAULT_CLOSURE_ANCHOR,
    DEFAULT_TIMESTAMP_LABEL,
    CanonicalSnapshot,
    CanonicalizationError,
    ContentDecodingError,
    canonicalize_page,
)

#: The requirement allows at most five HTTP(S) redirects per retrieval.
MAX_REDIRECTS = 5

#: Read at most this many bytes per socket operation so the deadline can be
#: re-checked regularly.  This is not the response-size cap.
_READ_CHUNK_BYTES = 64 * 1024

#: Content types the control accepts.  Parameters such as ``charset`` are
#: stripped before comparison.
_ACCEPTED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})

#: Bounded French messages.  They never contain page content or header values.
_MAX_MESSAGE_LENGTH = 200

_MESSAGES = {
    "timeout": "délai de récupération dépassé",
    "network_error": "la page n'a pas pu être récupérée",
    "http_status": "statut HTTP inattendu",
    "empty_body": "la page récupérée est vide",
    "unsupported_content_type": "type de contenu non pris en charge",
    "oversized_response": "réponse trop volumineuse",
    "unparseable_content": "le contenu de la page n'a pas pu être analysé",
}

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FailureCategory(StrEnum):
    """Typed control-failure categories.

    A control failure is explicitly *not* a content change and must never be
    turned into an alert by callers.
    """

    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    HTTP_STATUS = "http_status"
    EMPTY_BODY = "empty_body"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    OVERSIZED_RESPONSE = "oversized_response"
    UNPARSEABLE_CONTENT = "unparseable_content"


@dataclass(frozen=True)
class FetchFailure:
    """A typed, bounded, human-readable control failure."""

    category: FailureCategory
    message: str


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one logical retrieval.

    Exactly one of ``snapshot`` and ``failure`` is set.  ``http_status``,
    ``etag`` and ``last_modified`` describe the final response when one was
    received, even for an HTTP-status failure.
    """

    snapshot: CanonicalSnapshot | None = None
    failure: FetchFailure | None = None
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    final_url: str = ""
    redirects: int = 0
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.failure is None and self.snapshot is not None


class _DeadlineExceeded(Exception):
    """Internal signal: the total retrieval budget is exhausted."""


class _UnsupportedTarget(Exception):
    """Internal signal: a redirect pointed at a non-HTTP(S) URL."""


@dataclass
class _Hop:
    """One HTTP response, before redirect/body policy is applied."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    charset: str | None = None
    content_type: str | None = None
    failure: FetchFailure | None = None


class _Abort:
    """Coordination between the waiting caller and the I/O worker.

    The caller sets the abort flag at the deadline and shuts down whichever
    socket the worker currently owns.  ``shutdown`` is important: closing a
    socket that still has a ``makefile`` reader attached only defers the close,
    so it would not unblock a pending ``recv``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._aborted = False

    def attach(self, sock: socket.socket | None) -> None:
        if sock is None:
            return
        with self._lock:
            self._socket = sock
            aborted = self._aborted
        if aborted:
            _shutdown_socket(sock)

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            sock = self._socket
        if sock is not None:
            _shutdown_socket(sock)


class Fetcher:
    """Fetch the configured target exactly once per call.

    ``fetch`` is safe to call repeatedly; every call gets a fresh worker,
    deadline and socket.  There is deliberately no retry logic.
    """

    def __init__(
        self,
        config: Config,
        *,
        logger: StructuredLogger | None = None,
        max_redirects: int = MAX_REDIRECTS,
        closure_anchor: str = DEFAULT_CLOSURE_ANCHOR,
        timestamp_label: str = DEFAULT_TIMESTAMP_LABEL,
    ) -> None:
        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int):
            raise ValueError("max_redirects must be an integer")
        if not 0 <= max_redirects <= MAX_REDIRECTS:
            raise ValueError(
                f"max_redirects must be between 0 and {MAX_REDIRECTS}"
            )
        self._target_url = config.target_url
        self._user_agent = config.user_agent
        self._timeout_seconds = config.request_timeout_seconds
        self._max_response_bytes = config.max_response_bytes
        self._max_redirects = max_redirects
        self._closure_anchor = closure_anchor
        self._timestamp_label = timestamp_label
        self._logger = logger

    # -- public API -----------------------------------------------------

    def fetch(self) -> FetchResult:
        """Perform one logical retrieval and return a typed result.

        The call returns within roughly ``request_timeout_seconds`` even when
        a server drips headers or body bytes indefinitely.
        """

        started = time.monotonic()
        deadline = started + self._timeout_seconds
        abort = _Abort()
        finished = threading.Event()
        guard = threading.Lock()
        state = {"timed_out": False}
        outcomes: list[FetchResult] = []

        def worker() -> None:
            try:
                outcome = self._retrieve(started, deadline, abort)
            except BaseException:  # defensive: a worker must never hang silently
                outcome = self._failure_result(
                    FailureCategory.NETWORK_ERROR,
                    _MESSAGES["network_error"],
                    started,
                    final_url=self._target_url,
                )
            with guard:
                if not state["timed_out"]:
                    outcomes.append(outcome)
            finished.set()

        thread = threading.Thread(
            target=worker, name="detection-fetch", daemon=True
        )
        thread.start()

        remaining = deadline - time.monotonic()
        completed = remaining > 0 and finished.wait(remaining)
        if not completed:
            with guard:
                state["timed_out"] = True
            abort.abort()
            # Give the worker a brief moment to unwind and release its socket.
            # The result is already decided; this is cleanup only.
            finished.wait(0.1)
            result = self._failure_result(
                FailureCategory.TIMEOUT,
                _MESSAGES["timeout"],
                started,
                final_url=self._target_url,
            )
        else:
            with guard:
                result = outcomes[0]

        self._log(result)
        return result

    # -- retrieval ------------------------------------------------------

    def _retrieve(
        self, started: float, deadline: float, abort: _Abort
    ) -> FetchResult:
        current_url = self._target_url
        redirects = 0

        while True:
            if time.monotonic() >= deadline:
                return self._failure_result(
                    FailureCategory.TIMEOUT,
                    _MESSAGES["timeout"],
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )
            remaining = deadline - time.monotonic()

            try:
                hop = self._request_once(current_url, remaining, deadline, abort)
            except _DeadlineExceeded:
                return self._failure_result(
                    FailureCategory.TIMEOUT,
                    _MESSAGES["timeout"],
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )
            except (socket.timeout, TimeoutError):
                return self._failure_result(
                    FailureCategory.TIMEOUT,
                    _MESSAGES["timeout"],
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )
            except _UnsupportedTarget:
                return self._failure_result(
                    FailureCategory.NETWORK_ERROR,
                    "redirection vers une adresse non HTTP(S) refusée",
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )
            except ssl.SSLError:
                return self._failure_result(
                    FailureCategory.NETWORK_ERROR,
                    _MESSAGES["network_error"],
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )
            except http.client.HTTPException:
                return self._failure_result(
                    FailureCategory.NETWORK_ERROR,
                    "réponse HTTP invalide",
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )
            except (OSError, ValueError):
                return self._failure_result(
                    FailureCategory.NETWORK_ERROR,
                    _MESSAGES["network_error"],
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )
            except Exception:
                return self._failure_result(
                    FailureCategory.NETWORK_ERROR,
                    _MESSAGES["network_error"],
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )

            if hop.failure is not None:
                return self._hop_failure_result(
                    hop,
                    hop.failure,
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )

            if hop.status in _REDIRECT_STATUSES:
                location = hop.headers.get("location")
                if not location:
                    return self._hop_failure_result(
                        hop,
                        FetchFailure(
                            FailureCategory.HTTP_STATUS,
                            f"statut HTTP {hop.status} sans destination",
                        ),
                        started,
                        final_url=current_url,
                        redirects=redirects,
                    )
                if redirects >= self._max_redirects:
                    return self._hop_failure_result(
                        hop,
                        FetchFailure(
                            FailureCategory.HTTP_STATUS,
                            "trop de redirections "
                            f"(maximum {self._max_redirects})",
                        ),
                        started,
                        final_url=current_url,
                        redirects=redirects,
                    )
                next_url = urljoin(current_url, location.strip())
                if not _is_http_url(next_url):
                    raise _UnsupportedTarget
                redirects += 1
                current_url = next_url
                continue

            if not 200 <= hop.status < 300:
                return self._hop_failure_result(
                    hop,
                    FetchFailure(
                        FailureCategory.HTTP_STATUS,
                        f"statut HTTP {hop.status} inattendu",
                    ),
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )

            if not hop.body:
                return self._hop_failure_result(
                    hop,
                    FetchFailure(
                        FailureCategory.EMPTY_BODY,
                        _MESSAGES["empty_body"],
                    ),
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )

            try:
                snapshot = canonicalize_page(
                    hop.body,
                    charset=hop.charset,
                    base_url=current_url,
                    closure_anchor=self._closure_anchor,
                    timestamp_label=self._timestamp_label,
                )
            except (CanonicalizationError, ContentDecodingError):
                return self._hop_failure_result(
                    hop,
                    FetchFailure(
                        FailureCategory.UNPARSEABLE_CONTENT,
                        _MESSAGES["unparseable_content"],
                    ),
                    started,
                    final_url=current_url,
                    redirects=redirects,
                )

            return FetchResult(
                snapshot=snapshot,
                failure=None,
                http_status=hop.status,
                etag=hop.headers.get("etag"),
                last_modified=hop.headers.get("last-modified"),
                content_type=hop.content_type,
                final_url=current_url,
                redirects=redirects,
                elapsed_seconds=time.monotonic() - started,
            )

    def _request_once(
        self,
        url: str,
        timeout: float,
        deadline: float,
        abort: _Abort,
    ) -> _Hop:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise _UnsupportedTarget

        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"

        connection = self._new_connection(parts.scheme, parts, timeout)
        response: http.client.HTTPResponse | None = None
        try:
            connection.connect()
            abort.attach(connection.sock)

            connection.request(
                "GET",
                target,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "text/html, application/xhtml+xml",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            status = response.status
            headers = {
                name.lower(): value for name, value in response.getheaders()
            }

            # Redirects and error statuses need no body.  Closing the response
            # without draining it keeps a hostile payload from being read.
            if status in _REDIRECT_STATUSES or not 200 <= status < 300:
                return _Hop(status=status, headers=headers)

            media_type, charset = _parse_content_type(
                headers.get("content-type")
            )
            if media_type not in _ACCEPTED_CONTENT_TYPES:
                return _Hop(
                    status=status,
                    headers=headers,
                    failure=FetchFailure(
                        FailureCategory.UNSUPPORTED_CONTENT_TYPE,
                        _MESSAGES["unsupported_content_type"],
                    ),
                )

            body, oversized = self._read_body(response, connection, deadline)
            if oversized:
                return _Hop(
                    status=status,
                    headers=headers,
                    failure=FetchFailure(
                        FailureCategory.OVERSIZED_RESPONSE,
                        _MESSAGES["oversized_response"],
                    ),
                )
            return _Hop(
                status=status,
                headers=headers,
                body=body,
                charset=charset,
                content_type=media_type,
            )
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            try:
                connection.close()
            except Exception:
                pass

    def _read_body(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        deadline: float,
    ) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _DeadlineExceeded

            sock = connection.sock
            if sock is not None:
                try:
                    # Bound each individual recv by the time left as well.
                    sock.settimeout(max(0.001, remaining))
                except OSError:
                    pass

            allowance = self._max_response_bytes - total
            chunk = response.read1(min(_READ_CHUNK_BYTES, allowance + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > self._max_response_bytes:
                return b"".join(chunks), True
            chunks.append(chunk)
        return b"".join(chunks), False

    def _new_connection(
        self,
        scheme: str,
        parts: SplitResult,
        timeout: float,
    ) -> http.client.HTTPConnection:
        host = parts.hostname
        port = parts.port
        if scheme == "https":
            return http.client.HTTPSConnection(
                host,
                port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(host, port, timeout=timeout)

    # -- results and logging -------------------------------------------

    def _failure_result(
        self,
        category: FailureCategory,
        message: str,
        started: float,
        *,
        final_url: str = "",
        redirects: int = 0,
    ) -> FetchResult:
        return FetchResult(
            snapshot=None,
            failure=FetchFailure(category, _bounded_message(message)),
            http_status=None,
            etag=None,
            last_modified=None,
            final_url=final_url,
            redirects=redirects,
            elapsed_seconds=time.monotonic() - started,
        )

    def _hop_failure_result(
        self,
        hop: _Hop,
        failure: FetchFailure,
        started: float,
        *,
        final_url: str,
        redirects: int,
    ) -> FetchResult:
        return FetchResult(
            snapshot=None,
            failure=FetchFailure(
                failure.category, _bounded_message(failure.message)
            ),
            http_status=hop.status,
            etag=hop.headers.get("etag"),
            last_modified=hop.headers.get("last-modified"),
            content_type=hop.content_type,
            final_url=final_url,
            redirects=redirects,
            elapsed_seconds=time.monotonic() - started,
        )

    def _log(self, result: FetchResult) -> None:
        if self._logger is None:
            return
        fields: dict[str, Any] = {
            "ok": result.ok,
            "http_status": result.http_status,
            "redirects": result.redirects,
            "elapsed_ms": round(result.elapsed_seconds * 1000),
        }
        if result.failure is not None:
            fields["category"] = result.failure.category.value
        # Only bounded metadata is emitted; page content and header values are
        # deliberately absent.
        self._logger.log("fetch_completed", **fields)


def fetch_page(
    config: Config,
    *,
    logger: StructuredLogger | None = None,
    max_redirects: int = MAX_REDIRECTS,
    closure_anchor: str = DEFAULT_CLOSURE_ANCHOR,
    timestamp_label: str = DEFAULT_TIMESTAMP_LABEL,
) -> FetchResult:
    """Convenience wrapper around :class:`Fetcher`."""

    return Fetcher(
        config,
        logger=logger,
        max_redirects=max_redirects,
        closure_anchor=closure_anchor,
        timestamp_label=timestamp_label,
    ).fetch()


def _parse_content_type(raw: str | None) -> tuple[str | None, str | None]:
    """Return ``(media_type, charset)`` from a Content-Type header."""

    if not raw:
        return None, None
    pieces = raw.split(";")
    media_type = pieces[0].strip().lower()
    charset: str | None = None
    for piece in pieces[1:]:
        name, separator, value = piece.partition("=")
        if separator and name.strip().lower() == "charset":
            charset = value.strip().strip('"').strip("'")
    return media_type or None, charset


def _is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - validate the port eagerly
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def _bounded_message(message: str) -> str:
    if len(message) <= _MAX_MESSAGE_LENGTH:
        return message
    return message[: _MAX_MESSAGE_LENGTH - 1].rstrip() + "…"


def _shutdown_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
