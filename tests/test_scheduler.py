from __future__ import annotations

import threading
import time
import unittest

from app.detection.scheduler import DEFAULT_INTERVAL_SECONDS, Scheduler


class CountingService:
    def __init__(self) -> None:
        self.runs = 0
        self.lock = threading.Lock()

    def run_once(self) -> None:
        with self.lock:
            self.runs += 1


class SchedulerTests(unittest.TestCase):
    def test_default_interval_is_fifteen_minutes(self) -> None:
        self.assertEqual(DEFAULT_INTERVAL_SECONDS, 900)
        scheduler = Scheduler(CountingService())
        self.assertEqual(scheduler.interval_seconds, 900)

    def test_runs_immediately_and_stops_gracefully(self) -> None:
        service = CountingService()
        scheduler = Scheduler(service, 3600)
        scheduler.start()

        deadline = time.monotonic() + 5
        while service.runs < 1 and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(service.runs, 1)
        self.assertTrue(scheduler.running)

        scheduler.stop(timeout=2)

        self.assertFalse(scheduler.running)
        self.assertEqual(service.runs, 1)

    def test_fixed_delay_runs_more_cycles_while_started(self) -> None:
        service = CountingService()
        scheduler = Scheduler(service, 0.02)
        scheduler.start()

        deadline = time.monotonic() + 5
        while service.runs < 3 and time.monotonic() < deadline:
            time.sleep(0.01)

        scheduler.stop(timeout=2)

        self.assertGreaterEqual(service.runs, 3)
        self.assertFalse(scheduler.running)

    def test_stop_when_never_started_is_a_noop(self) -> None:
        service = CountingService()
        scheduler = Scheduler(service, 0.01)

        scheduler.stop(timeout=0)

        self.assertFalse(scheduler.running)
        self.assertEqual(service.runs, 0)

    def test_interval_must_be_positive(self) -> None:
        service = CountingService()
        for value in (0, -1, True, "900"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Scheduler(service, value)  # type: ignore[arg-type]

    def test_cycle_error_does_not_kill_the_loop(self) -> None:
        class FlakyService:
            def __init__(self) -> None:
                self.runs = 0

            def run_once(self) -> None:
                self.runs += 1
                if self.runs == 1:
                    raise RuntimeError("transient")

        service = FlakyService()
        scheduler = Scheduler(service, 0.02)
        scheduler.start()

        deadline = time.monotonic() + 5
        while service.runs < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

        scheduler.stop(timeout=2)

        self.assertGreaterEqual(service.runs, 2)


if __name__ == "__main__":
    unittest.main()
