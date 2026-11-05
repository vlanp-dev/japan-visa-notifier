from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.detection.canonical import CanonicalSnapshot, canonicalize_page
from app.detection.engine import (
    STATUS_DISABLED,
    STATUS_NOT_EVALUATED,
    STATUS_NOT_TRIGGERED,
    STATUS_TRIGGERED,
)
from app.detection.fetch import FailureCategory, FetchFailure, FetchResult
from app.detection.rules import (
    RULE_ID_CLOSURE_BLOCK,
    RULE_ID_NEW_ELEMENT,
    RULE_ID_UPDATE_TIMESTAMP,
)
from app.detection.service import (
    OUTCOME_CHANGED,
    OUTCOME_CONTROL_FAILURE,
    OUTCOME_UNCHANGED,
    REASON_REFERENCE_INITIALIZED,
    ChangeEvent,
    CheckService,
)
from app.detection.store import ReferenceSeed, Store, utc_now_iso
from app.logging_setup import StructuredLogger
from app.db import connect

FIXTURES = Path(__file__).with_name("fixtures")
BASE_URL = "https://exemple.fr/calendrier"


def fixture_html() -> str:
    return (FIXTURES / "closed_page.html").read_text(encoding="utf-8")


def variant_html() -> str:
    return (FIXTURES / "closed_page_variant.html").read_text(encoding="utf-8")


def snapshot_of(html: str) -> CanonicalSnapshot:
    return canonicalize_page(html, base_url=BASE_URL)


def success(snapshot: CanonicalSnapshot) -> FetchResult:
    return FetchResult(
        snapshot=snapshot,
        http_status=200,
        etag='"closed-2026"',
        last_modified="Wed, 04 Nov 2026 09:30:00 GMT",
        content_type="text/html",
        final_url=BASE_URL,
    )


def failure(category: FailureCategory, message: str = "échec") -> FetchResult:
    return FetchResult(failure=FetchFailure(category, message))


class SequenceFetcher:
    """Return queued results; repeat the last one for further cycles."""

    def __init__(self, *results: FetchResult):
        if not results:
            raise ValueError("at least one result is required")
        self._results = list(results)
        self.calls = 0

    def fetch(self) -> FetchResult:
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[index]


class BlockingFetcher:
    def __init__(self, result: FetchResult):
        self.result = result
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def fetch(self) -> FetchResult:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("test did not release the fetcher")
        return self.result


def make_config(directory: str, **overrides) -> Config:
    values = {
        "target_url": BASE_URL,
        "database_path": str(Path(directory) / "detection.sqlite3"),
        "operator_label": "opérateur",
        "interval_seconds": 900,
    }
    values.update(overrides)
    return Config.from_mapping(values)


class CheckServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name
        self.config = make_config(self.directory)
        self.events: list[ChangeEvent] = []

    def make_service(self, *results: FetchResult, logger=None) -> CheckService:
        self.store = Store(self.config.database_path)
        self.fetcher = SequenceFetcher(*results)
        return CheckService(
            self.config,
            self.store,
            fetcher=self.fetcher,
            change_sink=self.events.append,
            logger=logger,
        )

    def check_count(self) -> int:
        connection = connect(self.config.database_path)
        try:
            return connection.execute(
                "SELECT COUNT(*) FROM checks"
            ).fetchone()[0]
        finally:
            connection.close()


class InitialCaptureTests(CheckServiceTestCase):
    def test_first_check_captures_the_reference_as_unchanged(self) -> None:
        service = self.make_service(success(snapshot_of(fixture_html())))

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_UNCHANGED)
        self.assertEqual(run.reason, REASON_REFERENCE_INITIALIZED)
        self.assertFalse(run.changed)
        self.assertEqual(self.events, [])

        current = service.store.current_reference()
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.content_hash, snapshot_of(fixture_html()).digest())
        self.assertEqual(current.operator_label, "opérateur")
        self.assertEqual(current.content_type, "text/html")

        check = service.store.latest_check()
        assert check is not None
        self.assertEqual(check.outcome, "unchanged")
        self.assertEqual(check.reason, "reference_initialized")
        self.assertEqual(check.reference_id, current.id)

        results = service.store.results_for_check(check.id)
        self.assertEqual(len(results), 3)
        self.assertTrue(
            all(result.status == STATUS_NOT_TRIGGERED for result in results)
        )

    def test_identical_content_is_unchanged_without_alerting(self) -> None:
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(fixture_html())),
        )
        service.run_once()

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_UNCHANGED)
        self.assertIsNone(run.reason)
        self.assertEqual(self.events, [])
        self.assertEqual(
            {result.status for result in run.results}, {STATUS_NOT_TRIGGERED}
        )

    def test_equivalent_reformatting_is_unchanged(self) -> None:
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(variant_html())),
        )
        service.run_once()

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_UNCHANGED)
        self.assertEqual(self.events, [])


class ChangedContentTests(CheckServiceTestCase):
    def test_changed_announcement_triggers_the_closure_rule_and_sink(self) -> None:
        changed_html = fixture_html().replace(
            "<strong>Complet 2026</strong>",
            "<strong>Complet 2026 — liste d'attente</strong>",
        )
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(changed_html)),
        )
        service.run_once()

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_CHANGED)
        self.assertTrue(run.changed)
        self.assertEqual(run.triggered_rule_ids, (RULE_ID_CLOSURE_BLOCK,))
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.check_id, run.check_id)
        self.assertEqual(event.target_url, BASE_URL)
        self.assertEqual(event.triggered_rule_ids, (RULE_ID_CLOSURE_BLOCK,))

        check = service.store.latest_check()
        assert check is not None
        self.assertEqual(check.outcome, "changed")
        results = {
            result.rule_id: result
            for result in service.store.results_for_check(check.id)
        }
        self.assertEqual(results[RULE_ID_CLOSURE_BLOCK].status, STATUS_TRIGGERED)
        self.assertIn(
            "liste d'attente", results[RULE_ID_CLOSURE_BLOCK].current_excerpt
        )
        self.assertIn(
            "Complet 2026", results[RULE_ID_CLOSURE_BLOCK].reference_excerpt
        )
        self.assertEqual(
            results[RULE_ID_UPDATE_TIMESTAMP].status, STATUS_NOT_TRIGGERED
        )
        self.assertEqual(
            results[RULE_ID_NEW_ELEMENT].status, STATUS_NOT_TRIGGERED
        )

    def test_new_element_triggers_only_its_rule(self) -> None:
        changed_html = fixture_html().replace(
            "</ul>",
            (
                '<li><a href="https://exemple.fr/reservation?periode=2027">'
                "Réserver pour 2027</a></li>\n  </ul>"
            ),
        )
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(changed_html)),
        )
        service.run_once()

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_CHANGED)
        self.assertEqual(run.triggered_rule_ids, (RULE_ID_NEW_ELEMENT,))
        self.assertEqual(len(self.events), 1)

    def test_editing_timestamp_window_takes_effect_on_the_next_check(self) -> None:
        html = (
            "<html><body><p>Dernière mise à jour :"
            + "x" * 80
            + " 4 novembre 2026 à 09 h 30</p></body></html>"
        )
        changed = html.replace("09 h 30", "10 h 15")
        service = self.make_service(
            success(snapshot_of(html)),
            success(snapshot_of(changed)),
        )
        service.run_once()
        reference = service.store.current_reference()
        assert reference is not None
        self.assertTrue(reference.to_snapshot().timestamps)

        rule = service.store.get_rule(RULE_ID_UPDATE_TIMESTAMP)
        assert rule is not None
        service.store.update_rule(
            replace(
                rule,
                parameters={
                    "label": rule.parameters["label"],
                    "window": 10,
                },
            )
        )

        run = service.run_once()

        # The stored reference is re-extracted with the new window, so the
        # distant timestamp is no longer associated with the label.
        statuses = {result.rule_id: result.status for result in run.results}
        self.assertEqual(statuses[RULE_ID_UPDATE_TIMESTAMP], STATUS_NOT_EVALUATED)
        self.assertEqual(run.outcome, OUTCOME_UNCHANGED)
        self.assertEqual(self.events, [])

    def test_disabled_rule_does_not_produce_a_change(self) -> None:
        changed_html = fixture_html().replace(
            "<strong>Complet 2026</strong>",
            "<strong>Complet 2026 — liste d'attente</strong>",
        )
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(changed_html)),
        )
        service.store.set_rule_enabled(RULE_ID_CLOSURE_BLOCK, False)
        service.run_once()

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_UNCHANGED)
        statuses = {result.rule_id: result.status for result in run.results}
        self.assertEqual(statuses[RULE_ID_CLOSURE_BLOCK], STATUS_DISABLED)
        self.assertEqual(self.events, [])

    def test_change_sink_runs_only_after_the_check_is_committed(self) -> None:
        changed_html = fixture_html().replace("09 h 30", "10 h 15")
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(changed_html)),
        )
        service.run_once()

        observed: list[tuple[int, str]] = []

        def sink(event: ChangeEvent) -> None:
            fresh = Store(self.config.database_path)
            check = fresh.latest_check()
            assert check is not None
            observed.append((check.id, check.outcome))
            self.events.append(event)

        service._change_sink = sink  # noqa: SLF001 - deliberate test hook
        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_CHANGED)
        self.assertEqual(observed, [(run.check_id, "changed")])

    def test_failing_sink_does_not_lose_the_recorded_change(self) -> None:
        changed_html = fixture_html().replace("09 h 30", "10 h 15")
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(changed_html)),
        )
        service.run_once()

        def sink(event: ChangeEvent) -> None:
            raise RuntimeError("consumer unavailable")

        service._change_sink = sink  # noqa: SLF001 - deliberate test hook
        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_CHANGED)
        check = service.store.latest_check()
        assert check is not None
        self.assertEqual(check.outcome, "changed")

    def test_no_change_event_on_unchanged_cycles(self) -> None:
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(fixture_html())),
        )
        service.run_once()
        service.run_once()
        service.run_once()

        self.assertEqual(self.events, [])


class ControlFailureTests(CheckServiceTestCase):
    def test_fetch_failure_is_a_control_failure_not_a_change(self) -> None:
        service = self.make_service(
            failure(FailureCategory.TIMEOUT, "délai dépassé")
        )

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_CONTROL_FAILURE)
        self.assertTrue(run.failed)
        self.assertFalse(run.changed)
        self.assertEqual(run.failure_category, "timeout")
        self.assertEqual(self.events, [])
        self.assertIsNone(service.store.current_reference())

        check = service.store.latest_check()
        assert check is not None
        self.assertEqual(check.outcome, "control_failure")
        self.assertEqual(check.failure_category, "timeout")
        self.assertEqual(check.failure_message, "délai dépassé")
        results = service.store.results_for_check(check.id)
        self.assertEqual(len(results), 3)
        self.assertTrue(
            all(result.status == STATUS_NOT_EVALUATED for result in results)
        )

    def test_failure_keeps_the_existing_reference(self) -> None:
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            failure(FailureCategory.NETWORK_ERROR, "page inaccessible"),
        )
        service.run_once()
        reference = service.store.current_reference()
        assert reference is not None

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_CONTROL_FAILURE)
        current = service.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, reference.id)
        self.assertEqual(self.events, [])

    def test_malformed_and_unreachable_responses_are_failures(self) -> None:
        for category in (
            FailureCategory.UNPARSEABLE_CONTENT,
            FailureCategory.HTTP_STATUS,
            FailureCategory.NETWORK_ERROR,
        ):
            with self.subTest(category=category):
                service = self.make_service(failure(category))
                run = service.run_once()
                self.assertEqual(run.outcome, OUTCOME_CONTROL_FAILURE)
                self.assertEqual(run.failure_category, category.value)
                check = service.store.latest_check()
                assert check is not None
                self.assertEqual(check.outcome, "control_failure")
                self.assertNotEqual(check.outcome, "changed")
                self.assertEqual(self.events, [])

    def test_empty_snapshot_is_a_control_failure_not_a_change(self) -> None:
        service = self.make_service(
            FetchResult(
                snapshot=CanonicalSnapshot(text="", blocks=()),
                http_status=200,
                content_type="text/html",
            )
        )

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_CONTROL_FAILURE)
        self.assertEqual(run.failure_category, FailureCategory.UNPARSEABLE_CONTENT)
        self.assertFalse(run.changed)
        self.assertEqual(self.events, [])
        self.assertIsNone(service.store.current_reference())

    def test_a_fetcher_exception_is_treated_as_a_network_failure(self) -> None:
        class ExplodingFetcher:
            def fetch(self) -> FetchResult:
                raise OSError("boom")

        store = Store(self.config.database_path)
        service = CheckService(
            self.config,
            store,
            fetcher=ExplodingFetcher(),  # type: ignore[arg-type]
            change_sink=self.events.append,
        )

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_CONTROL_FAILURE)
        self.assertEqual(run.failure_category, "network_error")
        self.assertEqual(self.events, [])


class CycleDisciplineTests(CheckServiceTestCase):
    def test_exactly_one_fetch_per_cycle(self) -> None:
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(fixture_html())),
            success(snapshot_of(fixture_html())),
        )

        service.run_once()
        service.run_once()
        service.run_once()

        self.assertEqual(self.fetcher.calls, 3)
        self.assertEqual(self.check_count(), 3)

    def test_overlapping_cycle_is_skipped(self) -> None:
        store = Store(self.config.database_path)
        fetcher = BlockingFetcher(success(snapshot_of(fixture_html())))
        service = CheckService(
            self.config,
            store,
            fetcher=fetcher,  # type: ignore[arg-type]
            change_sink=self.events.append,
        )
        first_run: list = []

        def run_first() -> None:
            first_run.append(service.run_once())

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(fetcher.entered.wait(5))

        second = service.run_once()

        self.assertTrue(second.skipped)
        self.assertEqual(second.outcome, "skipped")

        fetcher.release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(fetcher.calls, 1)
        self.assertEqual(len(first_run), 1)
        self.assertEqual(first_run[0].outcome, OUTCOME_UNCHANGED)
        self.assertEqual(self.check_count(), 1)

    def test_simulated_24_hours_of_identical_checks_never_alerts(self) -> None:
        # 96 cycles at the default 900-second interval is exactly 24 hours.
        cycles = 24 * 60 * 60 // self.config.interval_seconds
        self.assertEqual(cycles, 96)
        service = self.make_service(success(snapshot_of(fixture_html())))

        runs = [service.run_once() for _ in range(cycles)]

        self.assertEqual(len(runs), 96)
        self.assertTrue(
            all(run.outcome == OUTCOME_UNCHANGED for run in runs)
        )
        self.assertTrue(all(not run.changed for run in runs))
        self.assertEqual(self.events, [])
        self.assertEqual(self.check_count(), 96)
        self.assertEqual(self.fetcher.calls, 96)

        changed_checks = [
            check
            for check in service.store.list_checks(limit=200)
            if check.outcome == "changed"
        ]
        self.assertEqual(changed_checks, [])
        self.assertEqual(
            service.store.list_checks(limit=200)[-1].reason,
            REASON_REFERENCE_INITIALIZED,
        )

    def test_superseded_reference_never_records_a_stale_change(self) -> None:
        service = self.make_service(success(snapshot_of(fixture_html())))
        service.run_once()
        checks_before = self.check_count()

        changed_html = fixture_html().replace("09 h 30", "10 h 15")
        service._fetcher = SequenceFetcher(success(snapshot_of(changed_html)))  # noqa: SLF001

        from app.detection import service as service_module

        original_evaluate = service_module.evaluate

        def evaluate_after_refreeze(*args, **kwargs):
            service.store.save_reference(
                ReferenceSeed(
                    target_url=BASE_URL,
                    snapshot=snapshot_of(variant_html()),
                    operator_label="opérateur",
                    captured_at=utc_now_iso(),
                )
            )
            return original_evaluate(*args, **kwargs)

        with patch(
            "app.detection.service.evaluate",
            side_effect=evaluate_after_refreeze,
        ):
            run = service.run_once()

        self.assertTrue(run.skipped)
        self.assertEqual(run.reason, "reference_changed")
        self.assertFalse(run.changed)
        self.assertEqual(self.check_count(), checks_before)
        self.assertEqual(self.events, [])

    def test_rules_and_reference_survive_a_restart(self) -> None:
        first = self.make_service(success(snapshot_of(fixture_html())))
        first.run_once()

        reopened_store = Store(self.config.database_path)
        second = CheckService(
            self.config,
            reopened_store,
            fetcher=SequenceFetcher(success(snapshot_of(fixture_html()))),
            change_sink=self.events.append,
        )

        run = second.run_once()

        self.assertEqual(run.outcome, OUTCOME_UNCHANGED)
        self.assertIsNone(run.reason)
        self.assertEqual(len(reopened_store.list_rules()), 3)
        self.assertEqual(len(reopened_store.list_references()), 1)
        self.assertEqual(self.events, [])
        self.assertEqual(len(reopened_store.list_checks()), 2)


class LoggingTests(CheckServiceTestCase):
    def test_cycle_logs_never_contain_page_content(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger(stream=stream)
        changed_html = fixture_html().replace(
            "<strong>Complet 2026</strong>",
            "<strong>Complet 2026 — liste d'attente</strong>",
        )
        service = self.make_service(
            success(snapshot_of(fixture_html())),
            success(snapshot_of(changed_html)),
            logger=logger,
        )
        service.run_once()
        service.run_once()

        logged = stream.getvalue()
        self.assertNotIn("Complet 2026", logged)
        self.assertNotIn("liste d'attente", logged)
        self.assertNotIn("SCRIPT_CONTENT", logged)
        self.assertNotIn("rendez-vous", logged)

        records = [json.loads(line) for line in logged.splitlines()]
        events = [record["event"] for record in records]
        self.assertIn("check_completed", events)
        for record in records:
            self.assertNotIn("snapshot", record)
            self.assertNotIn("excerpt", record)


if __name__ == "__main__":
    unittest.main()
