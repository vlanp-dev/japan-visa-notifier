"""Development server entry point: ``python -m app``."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from wsgiref.simple_server import WSGIRequestHandler, make_server

from .config import ConfigError, load_config
from .logging_config import configure_logging, get_logger, log_startup
from .web import create_app


class RequestHandler(WSGIRequestHandler):
    """Hide the interpreter version and route access logs through logging."""

    server_version = "episode-alerts"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        get_logger("server").info("%s - %s", self.address_string(), format % args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app",
        description="Serve the episode alert service.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="JSON configuration file (default: config/app.json if present)",
    )
    parser.add_argument("--host", help="Override the listening host")
    parser.add_argument("--port", type=int, help="Override the listening port")
    parser.add_argument("--database", help="Override the SQLite database path")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override the logging level",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config).with_overrides(
            host=args.host,
            port=args.port,
            database_path=args.database,
            log_level=args.log_level,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    logger = get_logger("server")
    try:
        application = create_app(config)
    except Exception as exc:  # startup errors should be actionable, not a traceback
        logger.error("Cannot start application: %s", exc)
        return 1

    log_startup(logger, config)
    try:
        with make_server(
            config.host,
            config.port,
            application,
            handler_class=RequestHandler,
        ) as server:
            logger.info(
                "Listening on http://%s:%s", config.host, config.port
            )
            server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopped by keyboard interrupt")
    except OSError as exc:
        logger.error("Cannot bind %s:%s: %s", config.host, config.port, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
