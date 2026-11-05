from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from app.config import Config
from app.detection.canonical import canonicalize_page
from app.detection.fetch import FailureCategory, FetchFailure, FetchResult
from app.detection.locking import reference_lock
from app.detection.refreeze import (
    SUCCESS_MESSAGE,
    RefreezeService,
    RefreezeResult,
)
from app.detection.service import (
    OUTCOME_UNCHANGED,
    ChangeEvent,
    CheckService,
)
from app.detection.store import ReferenceSeed, Store
from app.main import create_app
from app.security import CSRF_COOKIE
from tests.support import request

BASE_URL = "https://exemple.fr/calendrier"
FIXTURES = Path(__file__).with_name("fixtures")
OLD_CAPTURED_AT = "2026-11-04T09:30:00+00:00"


def fixture_html() -> str:
    return (FIXTURES / "closed_page.html").read_text(encoding="utf-8")


def variant_html() -> str:
    return (FIXTURES / "closed_page_variant.html").read_text(encoding="utf-8")


def snapshot_of(html: str):
    return canonicalize_page(html, base_url=BASE_URL)


def make_config(directory: str, **overrides) -> Config:
    values = {
        "target_url": BASE_URL,
        "database_path": str(Path(directory) / "detection.sqlite3"),
        "operator_label": "opérateur test",
        "interval_seconds": 900,
    }
    values.update(overrides)
    return Config.from_mapping(values)


def seed_for(html: str, *, operator: str = "opérateur initial") -> ReferenceSeed:
    return ReferenceSeed(
        target_url=BASE_URL,
        snapshot=snapshot_of(html),
        operator_label=operator,
        captured_at=OLD_CAPTURED_AT,
        http_status=200,
        etag='"old"',
        last_modified="Wed, 04 Nov 2026 09:30:00 GMT",
        content_type="text/html",
    )


def success(html: str) -> FetchResult:
    return FetchResult(
        snapshot=snapshot_of(html),
        http_status=200,
        etag='"new"',
        last_modified="Thu, 05 Nov 2026 10:00:00 GMT",
        content_type="text/html",
        final_url=BASE_URL,
    )


def failure(category: FailureCategory, message: str = "échec de test") -> FetchResult:
    return FetchResult(failure=FetchFailure(category, message))


class StaticFetcher:
    def __init__(self, result: FetchResult):
        self.result = result
        self.calls = 0

    def fetch(self) -> FetchResult:
        self.calls += 1
        return self.result


class ExplodingFetcher:
    def fetch(self) -> FetchResult:
        raise OSError("boom")


class SequenceFetcher:
    def __init__(self, *results: FetchResult):
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


class RefreezeServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name
        self.config = make_config(self.directory)
        self.store = Store(self.config.database_path)

    def capture_old_reference(self):
        return self.store.save_reference(seed_for(fixture_html()))


class SuccessfulRefreezeTests(RefreezeServiceTestCase):
    def test_refreeze_replaces_the_reference_and_records_success_history(
        self,
    ) -> None:
        old = self.capture_old_reference()
        fetcher = StaticFetcher(success(variant_html()))
        service = RefreezeService(self.config, self.store, fetcher=fetcher)

        result = service.refreeze()

        self.assertIsInstance(result, RefreezeResult)
        self.assertTrue(result.success)
        self.assertEqual(fetcher.calls, 1)
        self.assertEqual(result.previous_reference, old)
        self.assertIsNotNone(result.reference)
        assert result.reference is not None
        new = result.reference
        self.assertNotEqual(new.id, old.id)
        self.assertTrue(new.is_current)
        self.assertEqual(new.previous_reference_id, old.id)
        self.assertEqual(new.content_hash, snapshot_of(variant_html()).digest())
        self.assertEqual(new.to_snapshot(), snapshot_of(variant_html()))
        self.assertEqual(new.operator_label, "opérateur test")
        self.assertEqual(new.http_status, 200)
        self.assertEqual(new.etag, '"new"')
        self.assertEqual(new.content_type, "text/html")
        self.assertEqual(
            new.last_modified, "Thu, 05 Nov 2026 10:00:00 GMT"
        )
        self.assertNotEqual(new.captured_at, OLD_CAPTURED_AT)

        superseded = self.store.get_reference(old.id)
        assert superseded is not None
        self.assertFalse(superseded.is_current)

        current = self.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, new.id)

        self.assertTrue(result.event.success)
        self.assertEqual(result.event.previous_reference_id, old.id)
        self.assertEqual(result.event.new_reference_id, new.id)
        self.assertEqual(result.event.operator_label, "opérateur test")
        self.assertEqual(result.event.message, SUCCESS_MESSAGE)
        self.assertEqual(self.store.list_refreeze_events(), [result.event])
        self.assertEqual(len(self.store.list_references()), 2)

    def test_refreeze_does_not_create_a_check_or_emit_a_change(self) -> None:
        old = self.capture_old_reference()
        events: list[ChangeEvent] = []
        check_service = CheckService(
            self.config,
            self.store,
            fetcher=StaticFetcher(success(variant_html())),
            change_sink=events.append,
        )

        result = RefreezeService(
            self.config,
            self.store,
            fetcher=StaticFetcher(success(variant_html())),
        ).refreeze()

        self.assertTrue(result.success)
        self.assertEqual(events, [])
        self.assertEqual(self.store.list_checks(), [])
        current = check_service.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, result.reference.id)
        self.assertNotEqual(
            self.store.current_reference().id, old.id
        )

    def test_next_identical_check_is_unchanged_after_refreeze(self) -> None:
        events: list[ChangeEvent] = []
        service = CheckService(
            self.config,
            self.store,
            fetcher=SequenceFetcher(
                success(fixture_html()), success(variant_html())
            ),
            change_sink=events.append,
        )
        first = service.run_once()
        self.assertEqual(first.outcome, OUTCOME_UNCHANGED)

        result = RefreezeService(
            self.config,
            self.store,
            fetcher=StaticFetcher(success(variant_html())),
        ).refreeze()
        assert result.reference is not None

        run = service.run_once()

        self.assertEqual(run.outcome, OUTCOME_UNCHANGED)
        self.assertIsNone(run.reason)
        self.assertEqual(events, [])
        self.assertTrue(
            all(item.status == "not_triggered" for item in run.results)
        )
        check = self.store.latest_check()
        assert check is not None
        self.assertEqual(check.reference_id, result.reference.id)

    def test_refreeze_state_survives_a_restart(self) -> None:
        old = self.capture_old_reference()
        RefreezeService(
            self.config,
            self.store,
            fetcher=StaticFetcher(success(variant_html())),
        ).refreeze()

        reopened = Store(self.config.database_path)
        current = reopened.current_reference()
        assert current is not None
        self.assertNotEqual(current.id, old.id)
        self.assertEqual(current.previous_reference_id, old.id)
        self.assertEqual(current.content_hash, snapshot_of(variant_html()).digest())
        events = reopened.list_refreeze_events()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].success)
        self.assertEqual(events[0].new_reference_id, current.id)

        second = RefreezeService(
            self.config,
            reopened,
            fetcher=StaticFetcher(success(fixture_html())),
        ).refreeze()
        self.assertTrue(second.success)
        assert second.reference is not None
        self.assertEqual(second.reference.previous_reference_id, current.id)
        self.assertEqual(len(reopened.list_references()), 3)


class FailedRefreezeTests(RefreezeServiceTestCase):
    def test_failed_retrieval_records_a_failed_event_and_keeps_reference(
        self,
    ) -> None:
        old = self.capture_old_reference()
        fetcher = StaticFetcher(
            failure(FailureCategory.TIMEOUT, "délai de récupération dépassé")
        )

        result = RefreezeService(
            self.config, self.store, fetcher=fetcher
        ).refreeze()

        self.assertFalse(result.success)
        self.assertIsNone(result.reference)
        self.assertEqual(result.failure_category, "timeout")
        self.assertEqual(
            result.failure_message, "délai de récupération dépassé"
        )

        current = self.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, old.id)
        self.assertEqual(len(self.store.list_references()), 1)

        events = self.store.list_refreeze_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertFalse(event.success)
        self.assertEqual(event.previous_reference_id, old.id)
        self.assertIsNone(event.new_reference_id)
        self.assertEqual(event.operator_label, "opérateur test")
        self.assertEqual(event.message, "délai de récupération dépassé")

        # The preserved reference stays usable for the next check.
        check = CheckService(
            self.config,
            self.store,
            fetcher=StaticFetcher(success(fixture_html())),
        ).run_once()
        self.assertEqual(check.outcome, OUTCOME_UNCHANGED)

    def test_fetcher_exception_is_a_recorded_network_failure(self) -> None:
        old = self.capture_old_reference()

        result = RefreezeService(
            self.config, self.store, fetcher=ExplodingFetcher()
        ).refreeze()

        self.assertFalse(result.success)
        self.assertEqual(result.failure_category, "network_error")
        current = self.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, old.id)
        events = self.store.list_refreeze_events()
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].success)

    def test_no_reference_is_created_when_the_very_first_refreeze_fails(
        self,
    ) -> None:
        result = RefreezeService(
            self.config,
            self.store,
            fetcher=StaticFetcher(failure(FailureCategory.NETWORK_ERROR)),
        ).refreeze()

        self.assertFalse(result.success)
        self.assertIsNone(self.store.current_reference())
        self.assertEqual(self.store.list_references(), [])
        self.assertEqual(len(self.store.list_refreeze_events()), 1)


class RefreezeAtomicityTests(RefreezeServiceTestCase):
    def test_event_write_failure_rolls_back_the_new_reference(self) -> None:
        old = self.capture_old_reference()
        new_seed = seed_for(variant_html())

        with patch(
            "app.detection.store._insert_refreeze_event",
            side_effect=RuntimeError("event write failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.refreeze_reference(
                    new_seed, operator_label="opérateur test"
                )

        current = self.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, old.id)
        self.assertEqual(len(self.store.list_references()), 1)
        self.assertEqual(self.store.list_refreeze_events(), [])

    def test_reference_write_failure_leaves_no_partial_event(self) -> None:
        self.capture_old_reference()

        with patch(
            "app.detection.store._insert_reference",
            side_effect=RuntimeError("reference write failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.refreeze_reference(
                    seed_for(variant_html()), operator_label="opérateur test"
                )

        self.assertEqual(len(self.store.list_references()), 1)
        self.assertEqual(self.store.list_refreeze_events(), [])


class RefreezeSerializationTests(RefreezeServiceTestCase):
    def test_refreeze_waits_for_a_running_check_before_replacing(self) -> None:
        self.capture_old_reference()
        blocking = BlockingFetcher(success(variant_html()))
        check_service = CheckService(
            self.config, self.store, fetcher=blocking
        )
        check_results = []

        def run_check() -> None:
            check_results.append(check_service.run_once())

        check_thread = threading.Thread(target=run_check)
        check_thread.start()
        self.assertTrue(blocking.entered.wait(5))

        refreeze_fetcher = StaticFetcher(success(fixture_html()))
        refreeze_service = RefreezeService(
            self.config, self.store, fetcher=refreeze_fetcher
        )
        refreeze_results = []

        def run_refreeze() -> None:
            refreeze_results.append(refreeze_service.refreeze())

        refreeze_thread = threading.Thread(target=run_refreeze)
        refreeze_thread.start()
        time.sleep(0.05)

        # The check owns the shared lock, so the refreeze cannot even retrieve.
        self.assertEqual(refreeze_fetcher.calls, 0)

        blocking.release.set()
        check_thread.join(5)
        refreeze_thread.join(5)
        self.assertFalse(check_thread.is_alive())
        self.assertFalse(refreeze_thread.is_alive())

        self.assertEqual(check_results[0].outcome, OUTCOME_UNCHANGED)
        self.assertTrue(refreeze_results[0].success)
        current = self.store.current_reference()
        assert current is not None
        self.assertEqual(
            current.content_hash, snapshot_of(fixture_html()).digest()
        )
        self.assertEqual(len(self.store.list_references()), 2)

    def test_check_is_skipped_while_a_refreeze_holds_the_lock(self) -> None:
        self.capture_old_reference()
        holder_ready = threading.Event()
        release_holder = threading.Event()

        def hold_lock() -> None:
            with reference_lock(self.store.database_path):
                holder_ready.set()
                release_holder.wait(5)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(holder_ready.wait(5))
        fetcher = StaticFetcher(success(fixture_html()))
        try:
            run = CheckService(
                self.config, self.store, fetcher=fetcher
            ).run_once()
        finally:
            release_holder.set()
            holder.join(5)

        self.assertTrue(run.skipped)
        self.assertEqual(fetcher.calls, 0)


class RefreezeWebTestCase(RefreezeServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.old = self.capture_old_reference()
        self.fetcher = StaticFetcher(success(variant_html()))
        self.open_app()

    def open_app(self) -> None:
        service = RefreezeService(
            self.config, self.store, fetcher=self.fetcher
        )
        self.app = create_app(
            self.config, store=self.store, refreeze_service=service
        )
        first = request(self.app, "/reference/refreeze")
        self.assertEqual(first.status, 200)
        self.token = first.cookies[CSRF_COOKIE]

    def get(self, path: str):
        return request(self.app, path, cookie=f"{CSRF_COOKIE}={self.token}")

    def post(self, fields: dict, *, with_token: bool = True):
        data = dict(fields)
        if with_token:
            data["csrf_token"] = self.token
        body = urlencode(data).encode("utf-8")
        return request(
            self.app,
            "/reference/refreeze",
            method="POST",
            body=body,
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={self.token}",
        )


class RefreezePageTests(RefreezeWebTestCase):
    def test_confirmation_page_shows_target_reference_and_warning(self) -> None:
        response = self.get("/reference/refreeze")

        self.assertEqual(response.status, 200)
        for expected in (
            "Refiger la référence",
            BASE_URL,
            self.old.content_hash,
            self.old.captured_at,
            self.old.operator_label,
            "Type de contenu",
            "text/html",
            "remplace la référence figée actuelle",
            'name="csrf_token"',
            f'value="{self.token}"',
            'name="confirm"',
            'value="yes"',
            "Historique des refigements",
            "Aucun refigement",
        ):
            self.assertIn(expected, response.text)
        # The page must show metadata only, never the frozen page content.
        self.assertNotIn("Complet 2026", response.text)
        self.assertNotIn("rendez-vous", response.text)

    def test_status_page_links_to_the_refreeze_confirmation(self) -> None:
        response = self.get("/")

        self.assertEqual(response.status, 200)
        self.assertIn('href="/reference/refreeze"', response.text)


class RefreezePostTests(RefreezeWebTestCase):
    def test_missing_confirmation_is_a_no_op(self) -> None:
        for fields in ({}, {"confirm": "no"}, {"cancel": "1"}):
            with self.subTest(fields=fields):
                response = self.post(fields)

                self.assertEqual(response.status, 303)
                self.assertEqual(
                    response.header("Location"),
                    "/reference/refreeze?ok=cancelled",
                )
                self.assertEqual(self.fetcher.calls, 0)
                current = self.store.current_reference()
                assert current is not None
                self.assertEqual(current.id, self.old.id)
                self.assertEqual(self.store.list_refreeze_events(), [])

    def test_missing_csrf_token_is_rejected_without_a_retrieval(self) -> None:
        response = self.post({"confirm": "yes"}, with_token=False)

        self.assertEqual(response.status, 403)
        self.assertEqual(self.fetcher.calls, 0)
        current = self.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, self.old.id)
        self.assertEqual(self.store.list_refreeze_events(), [])

    def test_confirmed_refreeze_redirects_and_shows_history(self) -> None:
        response = self.post({"confirm": "yes"})

        self.assertEqual(response.status, 303)
        self.assertEqual(
            response.header("Location"), "/reference/refreeze?ok=refrozen"
        )
        self.assertEqual(self.fetcher.calls, 1)

        current = self.store.current_reference()
        assert current is not None
        self.assertNotEqual(current.id, self.old.id)
        self.assertEqual(current.previous_reference_id, self.old.id)

        events = self.store.list_refreeze_events()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].success)

        page = self.get("/reference/refreeze?ok=refrozen")
        self.assertEqual(page.status, 200)
        self.assertIn("La référence a été refigée", page.text)
        self.assertIn("Réussite", page.text)
        self.assertIn(str(self.old.id), page.text)
        self.assertIn(str(current.id), page.text)
        self.assertIn(current.content_hash, page.text)

    def test_failed_refreeze_shows_failure_and_preserves_reference(self) -> None:
        self.fetcher.result = failure(
            FailureCategory.HTTP_STATUS, "statut HTTP 503 inattendu"
        )

        response = self.post({"confirm": "yes"})

        self.assertEqual(response.status, 200)
        self.assertIn("Refigement en échec", response.text)
        self.assertIn("statut HTTP inattendu", response.text)
        self.assertIn("http_status", response.text)
        self.assertIn("La référence actuelle a été conservée", response.text)

        current = self.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, self.old.id)

        events = self.store.list_refreeze_events()
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].success)
        self.assertEqual(events[0].previous_reference_id, self.old.id)

        page = self.get("/reference/refreeze")
        self.assertIn("Échec", page.text)

    def test_refreeze_history_survives_an_application_restart(self) -> None:
        self.post({"confirm": "yes"})

        self.open_app()
        page = self.get("/reference/refreeze")

        self.assertEqual(page.status, 200)
        self.assertIn("Réussite", page.text)
        self.assertIn(str(self.old.id), page.text)


if __name__ == "__main__":
    unittest.main()
