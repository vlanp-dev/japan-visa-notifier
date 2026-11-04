"""Application factory and placeholder routes."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .config import Config, load_config
from .db import connect, initialize_database
from .logging_setup import StructuredLogger
from .security import CsrfMiddleware, SecurityHeadersMiddleware
from .web import (
    Application,
    Request,
    escape,
    html_response,
    json_response,
    render_page,
)


def create_app(
    config: Config | None = None,
    logger: StructuredLogger | None = None,
) -> Any:
    """Build the WSGI callable, initializing the database idempotently."""

    config = config or load_config()
    logger = logger or StructuredLogger()
    initialize_database(config.database_path)

    application = Application(config, logger)
    application.add_route("GET", "/health", _health_handler(config))
    application.add_route("GET", "/", _status_handler(config))
    return SecurityHeadersMiddleware(
        CsrfMiddleware(application, config.max_request_bytes)
    )


def _health_handler(config: Config):
    def handle(request: Request):
        try:
            connection = connect(config.database_path)
            try:
                connection.execute("SELECT 1 FROM rules LIMIT 1").fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return json_response(
                503,
                {"status": "degraded", "service": "detection-calendrier"},
            )
        return json_response(
            200,
            {"status": "ok", "service": "detection-calendrier"},
        )

    return handle


def _status_handler(config: Config):
    def handle(request: Request):
        content = (
            "<h1>Veille du calendrier</h1>\n"
            "<p>Service de détection — fondation active.</p>\n"
            "<dl>\n"
            f"<dt>Page surveillée</dt><dd>{escape(config.target_url)}</dd>\n"
            f"<dt>Intervalle de contrôle</dt>"
            f"<dd>{escape(config.interval_seconds)} secondes</dd>\n"
            f"<dt>Opérateur</dt><dd>{escape(config.operator_label)}</dd>\n"
            "</dl>\n"
            "<p>Cette page de statut est provisoire. Les résultats de contrôle "
            "et l'historique seront affichés ici.</p>"
        )
        return html_response(200, render_page("Veille du calendrier", content))

    return handle


def main() -> int:
    """Run the development WSGI server until interrupted."""

    from wsgiref.simple_server import make_server

    config = load_config()
    logger = StructuredLogger()
    application = create_app(config, logger)

    logger.log(
        "service_start",
        host=config.host,
        port=config.port,
        interval_seconds=config.interval_seconds,
    )
    try:
        with make_server(config.host, config.port, application) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        logger.log("service_stop")
    return 0
