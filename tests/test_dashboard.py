from __future__ import annotations

import tempfile
import unittest
from html import unescape as html_unescape
from pathlib import Path
from typing import Any

from app.config import Config
from app.detection.canonical import canonicalize_page
from app.detection.engine import STATUS_TRIGGERED, RuleResult
from app.detection.fetch import FailureCategory, FetchFailure, FetchResult
from app.detection.refreeze import RefreezeService
from app.detection.rules import (
    RULE_ID_CLOSURE_BLOCK,
    RULE_ID_UPDATE_TIMESTAMP,
    Rule,
)
from app.detection.service import (
    OUTCOME_CHANGED,
    OUTCOME_CONTROL_FAILURE,
    OUTCOME_UNCHANGED,
    REASON_REFERENCE_INITIALIZED,
    ChangeEvent,
    CheckService,
)
from app.detection.store import ReferenceSeed, Store
from app.main import create_app
from tests.fetch_support import LoopbackServer, respond_html
from tests.support import request

BASE_URL = "https://exemple.fr/calendrier"
FIXTURES = Path(__file__).with_name("fixtures")
CLOSED_PAGE = (FIXTURES / "closed_page.html").read_bytes()


def visible(text: str) -> str:
    """Undo HTML escaping for content assertions; escaping is tested directly."""

    return html_unescape(text)


def fixture_html() -> str:
    return CLOSED_PAGE.decode("utf-8")


def altered_html() -> str:
    return fixture_html().replace("Complet 2026", "Ouvert 2027")


def snapshot_of(html: str):
    return canonicalize_page(html, base_url=BASE_URL)


def make_config(directory: str, **overrides: Any) -> Config:
    values = {
        "target_url": BASE_URL,
        "database_path": str(Path(directory) / "detection.sqlite3"),
        "operator_label": "opérateur test",
        "interval_seconds": 900,
        "request_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return Config.from_mapping(values)


def success(html: str) -> FetchResult:
    return FetchResult(
        snapshot=snapshot_of(html),
        http_status=200,
        etag='"closed-2026"',
        last_modified="Wed, 04 Nov 2026 09:30:00 GMT",
        final_url=BASE_URL,
    )


def failure(
    category: FailureCategory = FailureCategory.NETWORK_ERROR,
    message: str = "la page n'a pas pu être récupérée",
    *,
    http_status: int | None = None,
) -> FetchResult:
    return FetchResult(
        failure=FetchFailure(category, message),
        http_status=http_status,
    )


class StaticFetcher:
    def __init__(self, result: FetchResult):
        self.result = result
        self.calls = 0

    def fetch(self) -> FetchResult:
        self.calls += 1
        return self.result


class SequenceFetcher:
    def __init__(self, *results: FetchResult):
        if not results:
            raise ValueError("at least one result is required")
        self._results = list(results)
        self.calls = 0

    def fetch(self) -> FetchResult:
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[index]


class DashboardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name
        self.config = make_config(self.directory)
        self.store = Store(self.config.database_path)
        self.store.seed_default_rules()
        self.app = create_app(self.config, store=self.store)

    def get(self, path: str = "/"):
        return request(self.app, path)

    def run_checks(self, *results: FetchResult) -> list[Any]:
        service = CheckService(
            self.config,
            self.store,
            fetcher=SequenceFetcher(*results),
        )
        return [service.run_once() for _ in results]

    def reference_seed(self, html: str) -> ReferenceSeed:
        return ReferenceSeed(
            target_url=BASE_URL,
            snapshot=snapshot_of(html),
            operator_label="opérateur test",
            captured_at="2026-11-04T09:30:00+00:00",
            http_status=200,
        )


class EmptyDashboardTests(DashboardTestCase):
    def test_empty_dashboard_is_french_and_links_to_administration(self) -> None:
        response = self.get("/")

        self.assertEqual(response.status, 200)
        for expected in (
            "Veille du calendrier",
            "opérationnel",
            BASE_URL,
            "900 secondes",
            "opérateur test",
            "Aucune référence",
            "Aucun contrôle",
            "Historique récent des contrôles",
            "Refigements",
            'href="/rules"',
            'href="/reference/refreeze"',
        ):
            self.assertIn(expected, response.text)
        self.assertIn("text/html", response.header("Content-Type") or "")

    def test_degraded_database_is_reported_without_leaking_details(self) -> None:
        from app.db import connect

        connection = connect(self.config.database_path)
        try:
            connection.execute("DROP TABLE checks")
            connection.commit()
        finally:
            connection.close()

        response = self.get("/")

        self.assertEqual(response.status, 200)
        self.assertIn("dégradé", response.text)
        self.assertIn("base de données", response.text)
        self.assertNotIn("Traceback", response.text)


class DashboardOutcomeTests(DashboardTestCase):
    def test_initial_capture_is_displayed_as_unchanged(self) -> None:
        runs = self.run_checks(success(fixture_html()))
        self.assertEqual(runs[0].outcome, OUTCOME_UNCHANGED)
        self.assertEqual(runs[0].reason, REASON_REFERENCE_INITIALIZED)

        response = self.get("/")
        text = visible(response.text)

        self.assertIn("Aucun changement", text)
        self.assertIn("Référence initiale capturée", text)
        self.assertIn("reference_initialized", text)
        self.assertIn("Bloc d'annonce de fermeture", text)
        self.assertIn("Horodatage de mise à jour", text)
        self.assertIn("Nouvel élément absent de la référence", text)
        self.assertIn("Non déclenchée", text)
        self.assertIn("Extrait actuel", text)
        self.assertIn("Extrait de référence", text)
        self.assertIn('href="/checks/1"', text)

        reference = self.store.current_reference()
        assert reference is not None
        self.assertIn(reference.content_hash, text)
        # The page's script body is never part of the rendered dashboard.
        self.assertNotIn("SCRIPT_CONTENT", text)

    def test_changed_check_shows_per_rule_status_and_excerpts(self) -> None:
        runs = self.run_checks(success(fixture_html()), success(altered_html()))
        self.assertEqual(runs[1].outcome, OUTCOME_CHANGED)

        response = self.get("/")
        text = visible(response.text)

        self.assertIn("Changement détecté", text)
        self.assertIn("Déclenchée", text)
        self.assertEqual(text.count("Déclenchée"), 1)
        self.assertIn("Le bloc d'annonce de fermeture a disparu", text)
        self.assertIn("Complet 2026", text)
        self.assertIn("r1-closure-block", text)
        self.assertNotIn("<script>", response.text)

    def test_control_failure_is_not_presented_as_a_change(self) -> None:
        runs = self.run_checks(
            success(fixture_html()),
            failure(
                FailureCategory.HTTP_STATUS,
                'statut HTTP inattendu — <script>alert("panne")</script>',
                http_status=503,
            ),
        )
        self.assertEqual(runs[1].outcome, OUTCOME_CONTROL_FAILURE)

        response = self.get("/")
        text = visible(response.text)

        self.assertIn("Échec du contrôle", text)
        self.assertIn("statut HTTP inattendu", text)
        self.assertIn("http_status", text)
        self.assertIn("503", text)
        self.assertIn("Non évaluée", text)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertNotIn("<script>", response.text)
        self.assertNotIn("Changement détecté", text)

    def test_disabled_rule_is_reported_as_disabled(self) -> None:
        self.store.set_rule_enabled(RULE_ID_UPDATE_TIMESTAMP, False)
        self.run_checks(success(fixture_html()))

        response = self.get("/")
        text = visible(response.text)

        self.assertIn("Désactivée", text)
        self.assertIn(RULE_ID_UPDATE_TIMESTAMP, text)


class DashboardEscapingAndBoundsTests(DashboardTestCase):
    def test_rule_text_and_excerpts_are_escaped_and_bounded(self) -> None:
        self.store.save_reference(self.reference_seed(fixture_html()))
        self.store.add_rule(
            Rule(
                id="evil-rule",
                name='<script>alert("rule")</script>',
                kind="closure_block",
                enabled=True,
                parameters={"anchor": "Complet 2026"},
            )
        )
        long_current = "A" * 10_000
        long_reference = "B" * 10_000
        check = self.store.record_check(
            started_at="2026-11-05T10:00:00+00:00",
            finished_at="2026-11-05T10:00:01+00:00",
            outcome=OUTCOME_CHANGED,
            target_url=BASE_URL,
            duration_ms=1000,
            results=[
                RuleResult(
                    rule_id="evil-rule",
                    rule_name='<script>alert("rule")</script>',
                    kind="closure_block",
                    status=STATUS_TRIGGERED,
                    current_excerpt=long_current,
                    reference_excerpt=long_reference,
                    detail='<script>alert("detail")</script>',
                )
            ],
        )

        for path in ("/", f"/checks/{check.id}"):
            with self.subTest(path=path):
                response = self.get(path)

                self.assertEqual(response.status, 200)
                self.assertNotIn("<script>", response.text)
                self.assertIn("&lt;script&gt;", response.text)
                # 160-character excerpt budget: 159 characters plus an ellipsis.
                self.assertIn("A" * 159 + "…", response.text)
                self.assertNotIn("A" * 160, response.text)
                self.assertIn("B" * 159 + "…", response.text)
                self.assertNotIn("B" * 160, response.text)

    def test_operator_label_is_escaped_on_the_dashboard(self) -> None:
        config = make_config(
            self.directory,
            operator_label='<script>alert("operator")</script>',
        )
        app = create_app(config, store=self.store)

        response = request(app, "/")

        self.assertIn("&lt;script&gt;", response.text)
        self.assertNotIn("<script>", response.text)


class CheckDetailTests(DashboardTestCase):
    def test_each_past_check_keeps_its_own_detail_page(self) -> None:
        runs = self.run_checks(
            success(fixture_html()),
            success(altered_html()),
            failure(FailureCategory.TIMEOUT, "délai dépassé"),
        )
        first, second, third = (run.check_id for run in runs)

        initial = self.get(f"/checks/{first}")
        self.assertEqual(initial.status, 200)
        initial_text = visible(initial.text)
        self.assertIn("Détail du contrôle", initial_text)
        self.assertIn("Aucun changement", initial_text)
        self.assertIn("Référence initiale capturée", initial_text)
        self.assertIn("Non déclenchée", initial_text)
        self.assertNotIn("Changement détecté", initial_text)

        changed = self.get(f"/checks/{second}")
        changed_text = visible(changed.text)
        self.assertIn("Changement détecté", changed_text)
        self.assertIn("Déclenchée", changed_text)
        self.assertIn("Le bloc d'annonce de fermeture a disparu", changed_text)
        self.assertIn("Complet 2026", changed_text)

        failed = self.get(f"/checks/{third}")
        failed_text = visible(failed.text)
        self.assertIn("Échec du contrôle", failed_text)
        self.assertIn("timeout", failed_text)
        self.assertIn("Non évaluée", failed_text)

        for response in (initial, changed, failed):
            self.assertIn('href="/"', response.text)

    def test_unknown_or_invalid_check_id_is_a_404(self) -> None:
        for path in ("/checks/9999", "/checks/0"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, 404)
                self.assertIn("Contrôle introuvable", response.text)

        invalid = self.get("/checks/inconnu")
        self.assertEqual(invalid.status, 404)
        self.assertIn("Page introuvable", invalid.text)


class RefreezeHistoryTests(DashboardTestCase):
    def test_dashboard_shows_refreeze_history_and_notices(self) -> None:
        old = self.store.save_reference(self.reference_seed(fixture_html()))
        new, event = self.store.refreeze_reference(
            self.reference_seed(altered_html()),
            operator_label="opérateur test",
        )

        response = self.get("/?ok=refrozen")
        text = visible(response.text)

        self.assertIn("La référence a été refigée", text)
        self.assertIn("Réussite", text)
        self.assertIn(str(old.id), text)
        self.assertIn(str(new.id), text)
        self.assertEqual(event.new_reference_id, new.id)
        # Only reference metadata is rendered, never the captured page text.
        self.assertNotIn("Ouvert 2027", text)
        self.assertNotIn("Complet 2026", text)


class AcceptanceWalkthroughTests(DashboardTestCase):
    """End-to-end walkthrough against a local loopback page."""

    def test_initial_change_failure_refreeze_and_reset(self) -> None:
        events: list[ChangeEvent] = []
        server = LoopbackServer().start()
        try:
            server.route("/", respond_html(CLOSED_PAGE))
            config = make_config(
                self.directory,
                target_url=server.url("/"),
            )
            store = Store(config.database_path)
            service = CheckService(
                config, store, change_sink=events.append
            )
            app = create_app(config, store=store)

            first = service.run_once()
            self.assertEqual(first.outcome, OUTCOME_UNCHANGED)
            self.assertEqual(first.reason, REASON_REFERENCE_INITIALIZED)
            page = request(app, "/")
            self.assertIn("Référence initiale capturée", visible(page.text))
            self.assertIn("Non déclenchée", visible(page.text))

            server.route("/", respond_html(altered_html().encode("utf-8")))
            second = service.run_once()
            self.assertEqual(second.outcome, OUTCOME_CHANGED)
            triggered = [
                result.rule_id
                for result in second.results
                if result.status == STATUS_TRIGGERED
            ]
            self.assertEqual(triggered, [RULE_ID_CLOSURE_BLOCK])
            self.assertEqual(len(events), 1)

            changed_page = visible(request(app, "/").text)
            self.assertIn("Changement détecté", changed_page)
            self.assertIn("Déclenchée", changed_page)
            self.assertIn("Le bloc d'annonce de fermeture a disparu", changed_page)
            detail = visible(request(app, f"/checks/{second.check_id}").text)
            self.assertIn("Changement détecté", detail)

            # A confirmed refreeze pins the new content and resets the comparison.
            refreeze = RefreezeService(config, store)
            self.assertTrue(refreeze.refreeze().success)
            after_refreeze = service.run_once()
            self.assertEqual(after_refreeze.outcome, OUTCOME_UNCHANGED)
            self.assertIsNone(after_refreeze.reason)
            self.assertEqual(len(events), 1)
            dashboard = visible(request(app, "/").text)
            self.assertIn("Réussite", dashboard)
            self.assertIn("Aucun changement", dashboard)
        finally:
            server.stop()

        # Access failure is a control failure, never a content change.
        failed = service.run_once()
        self.assertEqual(failed.outcome, OUTCOME_CONTROL_FAILURE)
        self.assertEqual(failed.failure_category, "network_error")
        self.assertEqual(len(events), 1)

        failure_page = visible(request(app, "/").text)
        self.assertIn("Échec du contrôle", failure_page)
        self.assertIn("network_error", failure_page)
        self.assertIn("Non évaluée", failure_page)
        self.assertNotIn("Déclenchée", failure_page)


if __name__ == "__main__":
    unittest.main()
