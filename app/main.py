"""Application factory and HTTP routes."""

from __future__ import annotations

import sqlite3
from typing import Any

from .config import Config, load_config
from .db import connect
from .detection.admin import register_rule_routes
from .detection.dashboard import register_dashboard_routes
from .detection.refreeze import RefreezeService
from .detection.refreeze_admin import register_refreeze_routes
from .detection.store import Store
from .logging_setup import StructuredLogger
from .runtime import Runtime
from .security import CsrfMiddleware, SecurityHeadersMiddleware
from .web import Application, Request, json_response


def create_app(
    config: Config | None = None,
    logger: StructuredLogger | None = None,
    store: Store | None = None,
    refreeze_service: RefreezeService | None = None,
) -> Any:
    """Build the WSGI callable, initializing the database idempotently."""

    config = config or load_config()
    logger = logger or StructuredLogger()
    store = store if store is not None else Store(config.database_path)
    refreeze_service = (
        refreeze_service
        if refreeze_service is not None
        else RefreezeService(config, store, logger=logger)
    )

    application = Application(config, logger)
    register_rule_routes(application, store, logger)
    register_refreeze_routes(
        application, store, refreeze_service, config, logger
    )
    register_dashboard_routes(application, store, config, logger)
    application.add_route("GET", "/health", _health_handler(config))
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


def main() -> int:
    """Run the development WSGI server until interrupted."""

    from wsgiref.simple_server import make_server

    config = load_config()
    logger = StructuredLogger()
    application = create_app(config, logger)
    runtime = Runtime.create(config, logger)

    logger.log(
        "service_start",
        host=config.host,
        port=config.port,
        interval_seconds=config.interval_seconds,
    )
    runtime.start()
    try:
        with make_server(config.host, config.port, application) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
        logger.log("service_stop")
    return 0
