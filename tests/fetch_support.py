"""Local loopback HTTP fixture for fetch tests.

Everything is bound to ``127.0.0.1`` on an ephemeral port.  Responses are
produced by small callables so tests can exercise redirects, malformed status
lines, wrong content types, oversized bodies and deliberately slow ("drip")
servers without touching the network.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RecordedRequest:
    """One request as seen by the fixture."""

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)


Responder = Callable[[BaseHTTPRequestHandler], None]


class LoopbackServer:
    """Tiny HTTP server that records requests and dispatches to responders."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.routes: dict[str, Responder] = {}
        self.default: Responder = respond_html(
            b"<html><body><p>page par defaut</p></body></html>"
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fixture = self  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05),
            name="fetch-fixture",
            daemon=True,
        )

    # -- lifecycle ------------------------------------------------------

    def start(self) -> "LoopbackServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "LoopbackServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- addressing -----------------------------------------------------

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def url(self, path: str = "/") -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    # -- routing --------------------------------------------------------

    def route(self, path: str, responder: Responder) -> "LoopbackServer":
        self.routes[path] = responder
        return self

    def record(self, handler: BaseHTTPRequestHandler) -> None:
        self.requests.append(
            RecordedRequest(
                method=handler.command,
                path=handler.path,
                headers={
                    name.lower(): value
                    for name, value in handler.headers.items()
                },
            )
        )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_GET(self) -> None:
        fixture: LoopbackServer = self.server.fixture  # type: ignore[attr-defined]
        fixture.record(self)
        path = urlsplit(self.path).path
        responder = fixture.routes.get(path, fixture.default)
        try:
            responder(self)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The client may have aborted at its deadline.
            pass
        finally:
            self.close_connection = True


# -- responders ---------------------------------------------------------


def respond_html(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    headers: dict[str, str] | None = None,
) -> Responder:
    def responder(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        if body:
            handler.wfile.write(body)
            handler.wfile.flush()

    return responder


def respond_redirect(
    location: str,
    *,
    status: int = 302,
) -> Responder:
    def responder(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return responder


def respond_raw(raw: bytes) -> Responder:
    """Write an arbitrary byte stream, bypassing HTTP response helpers."""

    def responder(handler: BaseHTTPRequestHandler) -> None:
        handler.wfile.write(raw)
        handler.wfile.flush()

    return responder


def respond_after(delay: float, body: bytes) -> Responder:
    """Send a valid response only after ``delay`` seconds."""

    def responder(handler: BaseHTTPRequestHandler) -> None:
        time.sleep(delay)
        respond_html(body)(handler)

    return responder


def respond_drip_body(
    body: bytes,
    interval: float,
    *,
    content_type: str = "text/html; charset=utf-8",
) -> Responder:
    """Send headers at once, then one body byte every ``interval`` seconds."""

    def responder(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        for index in range(len(body)):
            handler.wfile.write(body[index : index + 1])
            handler.wfile.flush()
            time.sleep(interval)

    return responder


def respond_drip_headers(raw: bytes, interval: float) -> Responder:
    """Drip a complete raw response, one byte at a time."""

    def responder(handler: BaseHTTPRequestHandler) -> None:
        for index in range(len(raw)):
            handler.wfile.write(raw[index : index + 1])
            handler.wfile.flush()
            time.sleep(interval)

    return responder


def closed_port_url() -> str:
    """Return an ``http://`` URL on a port that was just released."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    finally:
        probe.close()
    return f"http://127.0.0.1:{port}/"
