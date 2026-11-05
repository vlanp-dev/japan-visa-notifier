"""Runtime wiring for the detection service.

Builds the store, check service and scheduler from validated configuration and
starts/stops them with the WSGI server.  Keeping this in one place means the
command-line entry point and tests use exactly the same composition.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .detection.scheduler import Scheduler
from .detection.service import ChangeSink, CheckService
from .detection.store import Store
from .logging_setup import StructuredLogger


@dataclass
class Runtime:
    """Composed detection dependencies for one process."""

    config: Config
    logger: StructuredLogger
    store: Store
    service: CheckService
    scheduler: Scheduler

    @classmethod
    def create(
        cls,
        config: Config,
        logger: StructuredLogger | None = None,
        *,
        fetcher=None,
        change_sink: ChangeSink | None = None,
    ) -> "Runtime":
        logger = logger if logger is not None else StructuredLogger()
        store = Store(config.database_path)
        service = CheckService(
            config,
            store,
            fetcher=fetcher,
            change_sink=change_sink,
            logger=logger,
        )
        scheduler = Scheduler(
            service, config.interval_seconds, logger=logger
        )
        return cls(
            config=config,
            logger=logger,
            store=store,
            service=service,
            scheduler=scheduler,
        )

    def start(self) -> None:
        self.logger.log(
            "runtime_started",
            interval_seconds=self.config.interval_seconds,
            target_url=self.config.target_url,
        )
        self.scheduler.start()

    def stop(self) -> None:
        # Allow an in-flight retrieval to finish within its own deadline.
        self.scheduler.stop(timeout=self.config.request_timeout_seconds + 5)
        self.logger.log("runtime_stopped")


__all__ = ["Runtime"]
