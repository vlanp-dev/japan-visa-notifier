"""Fixed-delay scheduler for check cycles.

The scheduler waits ``interval_seconds`` *after* each completed run, so a slow
retrieval never causes overlapping cycles or a burst of catch-up checks.  It
runs in a daemon thread and stops gracefully: a run already in progress is
allowed to finish before the thread exits.
"""

from __future__ import annotations

import threading

from ..logging_setup import StructuredLogger

DEFAULT_INTERVAL_SECONDS = 900


class Scheduler:
    """Periodically call ``service.run_once`` with a fixed delay."""

    def __init__(
        self,
        service,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        *,
        logger: StructuredLogger | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        if isinstance(interval_seconds, bool) or not isinstance(
            interval_seconds, (int, float)
        ):
            raise ValueError("interval_seconds must be a number")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        self._service = service
        self._interval_seconds = interval_seconds
        self._logger = logger
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> threading.Thread:
        """Start the background loop unless it is already running."""

        if self.running:
            assert self._thread is not None
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever,
            name="detection-scheduler",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def run_forever(self) -> None:
        """Run one cycle immediately, then one per fixed-delay interval."""

        self._log("scheduler_started", interval_seconds=self._interval_seconds)
        try:
            while not self._stop.is_set():
                try:
                    self._service.run_once()
                except Exception as exc:
                    self._log("scheduler_cycle_error", error=type(exc).__name__)
                # Fixed delay: the interval starts once the cycle is finished.
                if self._stop.wait(self._interval_seconds):
                    break
        finally:
            self._log("scheduler_stopped")

    def stop(self, timeout: float | None = 5.0) -> None:
        """Request a graceful stop and wait for the loop to finish.

        A cycle in progress is allowed to complete.  Calling stop from the
        scheduler thread itself only raises the flag.
        """

        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        if thread is threading.current_thread():
            return
        thread.join(timeout)
        if not thread.is_alive():
            self._thread = None

    # ``request_stop`` is the explicit name used by wiring and tests.
    request_stop = stop

    def _log(self, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        self._logger.log(event, **fields)


__all__ = ["DEFAULT_INTERVAL_SECONDS", "Scheduler"]
