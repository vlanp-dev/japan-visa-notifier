from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app.config import Config
from app.detection.canonical import canonicalize_page
from app.detection.fetch import FetchResult
from app.detection.rules import (
    RULE_ID_CLOSURE_BLOCK,
    RULE_ID_NEW_ELEMENT,
    RULE_ID_UPDATE_TIMESTAMP,
    RULE_KIND_CLOSURE_BLOCK,
    RULE_KIND_NEW_ELEMENT,
    RULE_KIND_UPDATE_TIMESTAMP,
)
from app.detection.service import (
    OUTCOME_CHANGED,
    OUTCOME_UNCHANGED,
    ChangeEvent,
    CheckService,
)
from app.detection.store import ReferenceSeed, Store, utc_now_iso
from app.main import create_app
from app.security import CSRF_COOKIE
from app.web import escape
from tests.support import request

BASE_URL = "https://exemple.fr/calendrier"
FIXTURES = Path(__file__).with_name("fixtures")


def fixture_html() -> str:
    return (FIXTURES / "closed_page.html").read_text(encoding="utf-8")


def snapshot_of(html: str):
    return canonicalize_page(html, base_url=BASE_URL)


def make_config(directory: str) -> Config:
    return Config.from_mapping(
        {
            "target_url": BASE_URL,
            "database_path": str(Path(directory) / "detection.sqlite3"),
            "operator_label": "opérateur",
            "interval_seconds": 900,
        }
    )


class StaticFetcher:
    """Return the same retrieval result for every cycle."""

    def __init__(self, result: FetchResult):
        self.result = result
        self.calls = 0

    def fetch(self) -> FetchResult:
        self.calls += 1
        return self.result


def success(html: str) -> FetchResult:
    return FetchResult(
        snapshot=snapshot_of(html),
        http_status=200,
        etag='"closed-2026"',
        last_modified="Wed, 04 Nov 2026 09:30:00 GMT",
        final_url=BASE_URL,
    )


class RuleAdminTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name
        self.config = make_config(self.directory)
        self.store = Store(self.config.database_path)
        self.store.seed_default_rules()
        self.open_app()

    def open_app(self) -> None:
        """(Re)build the application against the shared database."""

        self.app = create_app(self.config, store=self.store)
        first = request(self.app, "/rules")
        self.assertEqual(first.status, 200)
        self.token = first.cookies[CSRF_COOKIE]

    def post(self, path: str, fields: dict, *, with_token: bool = True):
        data = dict(fields)
        if with_token:
            data["csrf_token"] = self.token
        body = urlencode(data, doseq=True).encode("utf-8")
        return request(
            self.app,
            path,
            method="POST",
            body=body,
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={self.token}",
        )

    def rule_fields(self, **overrides) -> dict:
        values = {
            "name": "Nouvelle règle",
            "kind": RULE_KIND_CLOSURE_BLOCK,
            "enabled": "1",
            "anchor": "Fermé jusqu'au 3 mars",
            "label": "mise à jour",
            "window": "160",
            "sources": ["link", "document", "time"],
        }
        values.update(overrides)
        return values

    def rules_state(self):
        return [rule.to_dict() for rule in self.store.list_rules()]


class ListingTests(RuleAdminTestCase):
    def test_list_shows_kind_state_and_parameters(self) -> None:
        response = request(
            self.app, "/rules", cookie=f"{CSRF_COOKIE}={self.token}"
        )

        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.header("Content-Type") or "")
        for expected in (
            "Règles de détection",
            escape("Bloc d'annonce de fermeture"),
            "Horodatage de mise à jour",
            "Nouvel élément absent de la référence",
            "closure_block",
            "update_timestamp",
            "new_element",
            "Active",
            "Complet 2026",
            "mise à jour",
            "liens",
            'name="csrf_token"',
            f'value="{self.token}"',
            'href="/rules/new"',
        ):
            self.assertIn(expected, response.text)
        for rule_id in (
            RULE_ID_CLOSURE_BLOCK,
            RULE_ID_UPDATE_TIMESTAMP,
            RULE_ID_NEW_ELEMENT,
        ):
            self.assertIn(rule_id, response.text)

    def test_new_rule_form_is_available(self) -> None:
        response = request(self.app, "/rules/new")

        self.assertEqual(response.status, 200)
        self.assertIn("Ajouter une règle", response.text)
        self.assertIn('name="kind"', response.text)
        self.assertIn('name="anchor"', response.text)
        self.assertIn('name="window"', response.text)
        self.assertIn('value="link"', response.text)

    def test_edit_form_is_prefilled(self) -> None:
        response = request(self.app, f"/rules/{RULE_ID_CLOSURE_BLOCK}/edit")

        self.assertEqual(response.status, 200)
        self.assertIn("Complet 2026", response.text)
        self.assertIn(
            f'value="{escape("Bloc d\'annonce de fermeture")}"',
            response.text,
        )
        self.assertIn("Modifier la règle", response.text)

    def test_unknown_rule_returns_404(self) -> None:
        self.assertEqual(
            request(self.app, "/rules/does-not-exist/edit").status, 404
        )
        self.assertEqual(
            self.post("/rules/does-not-exist/disable", {}).status, 404
        )


class AddRuleTests(RuleAdminTestCase):
    def test_add_each_supported_kind(self) -> None:
        cases = (
            (
                self.rule_fields(
                    name="Annonce locale",
                    kind=RULE_KIND_CLOSURE_BLOCK,
                    anchor="Fermé jusqu'au 3 mars",
                ),
                {"anchor": "Fermé jusqu'au 3 mars"},
            ),
            (
                self.rule_fields(
                    name="Horodatage local",
                    kind=RULE_KIND_UPDATE_TIMESTAMP,
                    label="dernière actualisation",
                    window="300",
                ),
                {"label": "dernière actualisation", "window": 300},
            ),
            (
                self.rule_fields(
                    name="Nouveaux documents",
                    kind=RULE_KIND_NEW_ELEMENT,
                    sources=["document", "time"],
                ),
                {"sources": ["document", "time"]},
            ),
        )
        for fields, expected_parameters in cases:
            with self.subTest(kind=fields["kind"]):
                response = self.post("/rules", fields)

                self.assertEqual(response.status, 303)
                self.assertEqual(response.header("Location"), "/rules?ok=added")

                rule = next(
                    r for r in self.store.list_rules() if r.name == fields["name"]
                )
                self.assertEqual(rule.kind, fields["kind"])
                self.assertTrue(rule.enabled)
                self.assertEqual(dict(rule.parameters), expected_parameters)

        listing = request(self.app, "/rules")
        self.assertIn("La règle a été ajoutée", request(
            self.app, "/rules?ok=added"
        ).text)
        for name in ("Annonce locale", "Horodatage local", "Nouveaux documents"):
            self.assertIn(name, listing.text)

    def test_added_rule_can_start_disabled(self) -> None:
        fields = self.rule_fields(name="Règle inactive")
        fields.pop("enabled")

        response = self.post("/rules", fields)

        self.assertEqual(response.status, 303)
        rule = next(
            r for r in self.store.list_rules() if r.name == "Règle inactive"
        )
        self.assertFalse(rule.enabled)

    def test_generated_identifiers_are_safe_and_unique(self) -> None:
        first = self.post("/rules", self.rule_fields(name="Été 2026"))
        second = self.post("/rules", self.rule_fields(name="Ete 2026"))

        self.assertEqual(first.status, 303)
        self.assertEqual(second.status, 303)
        ids = {
            rule.id
            for rule in self.store.list_rules()
            if rule.name in {"Été 2026", "Ete 2026"}
        }
        self.assertEqual(ids, {"ete-2026", "ete-2026-2"})

    def test_rule_names_and_parameters_are_escaped(self) -> None:
        response = self.post(
            "/rules",
            self.rule_fields(
                name="<script>alert('x')</script>",
                anchor="Fermé <b>2026</b>",
            ),
        )
        self.assertEqual(response.status, 303)

        listing = request(self.app, "/rules")

        self.assertNotIn("<script>", listing.text)
        self.assertIn("&lt;script&gt;", listing.text)
        self.assertIn("&lt;b&gt;2026&lt;/b&gt;", listing.text)


class EditAndToggleTests(RuleAdminTestCase):
    def test_edit_parameters_and_enabled_state(self) -> None:
        fields = self.rule_fields(
            name="Bloc suivi par l'opérateur",
            kind=RULE_KIND_CLOSURE_BLOCK,
            anchor="Fermeture prolongée",
        )
        fields.pop("enabled")

        response = self.post(f"/rules/{RULE_ID_CLOSURE_BLOCK}", fields)

        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("Location"), "/rules?ok=updated")
        rule = self.store.get_rule(RULE_ID_CLOSURE_BLOCK)
        assert rule is not None
        self.assertEqual(rule.name, "Bloc suivi par l'opérateur")
        self.assertEqual(dict(rule.parameters), {"anchor": "Fermeture prolongée"})
        self.assertFalse(rule.enabled)

        listing = request(self.app, "/rules?ok=updated")
        self.assertIn(escape("Bloc suivi par l'opérateur"), listing.text)
        self.assertIn("La règle a été mise à jour", listing.text)

    def test_edit_keeps_the_existing_kind_even_if_tampered(self) -> None:
        fields = self.rule_fields(
            name="Horodatage suivi",
            kind=RULE_KIND_NEW_ELEMENT,
            label="dernière mise à jour",
            window="250",
        )

        response = self.post(f"/rules/{RULE_ID_UPDATE_TIMESTAMP}", fields)

        self.assertEqual(response.status, 303)
        rule = self.store.get_rule(RULE_ID_UPDATE_TIMESTAMP)
        assert rule is not None
        self.assertEqual(rule.kind, RULE_KIND_UPDATE_TIMESTAMP)
        self.assertEqual(
            dict(rule.parameters),
            {"label": "dernière mise à jour", "window": 250},
        )

    def test_disable_then_enable(self) -> None:
        disabled = self.post(f"/rules/{RULE_ID_UPDATE_TIMESTAMP}/disable", {})
        self.assertEqual(disabled.status, 303)
        self.assertEqual(disabled.header("Location"), "/rules?ok=disabled")
        rule = self.store.get_rule(RULE_ID_UPDATE_TIMESTAMP)
        assert rule is not None
        self.assertFalse(rule.enabled)

        enabled = self.post(f"/rules/{RULE_ID_UPDATE_TIMESTAMP}/enable", {})
        self.assertEqual(enabled.status, 303)
        self.assertEqual(enabled.header("Location"), "/rules?ok=enabled")
        rule = self.store.get_rule(RULE_ID_UPDATE_TIMESTAMP)
        assert rule is not None
        self.assertTrue(rule.enabled)

    def test_rule_changes_survive_an_application_restart(self) -> None:
        self.post("/rules", self.rule_fields(name="Règle persistante"))
        self.post(
            f"/rules/{RULE_ID_CLOSURE_BLOCK}",
            self.rule_fields(
                name="Bloc persistant",
                anchor="Fermeture durable",
            ),
        )
        self.post(f"/rules/{RULE_ID_NEW_ELEMENT}/disable", {})

        self.store = Store(self.config.database_path)
        self.open_app()
        listing = request(self.app, "/rules")

        self.assertIn("Règle persistante", listing.text)
        self.assertIn("Bloc persistant", listing.text)
        reloaded = self.store.get_rule(RULE_ID_CLOSURE_BLOCK)
        assert reloaded is not None
        self.assertEqual(dict(reloaded.parameters), {"anchor": "Fermeture durable"})
        new_element = self.store.get_rule(RULE_ID_NEW_ELEMENT)
        assert new_element is not None
        self.assertFalse(new_element.enabled)


class ValidationTests(RuleAdminTestCase):
    def test_invalid_input_persists_nothing(self) -> None:
        before = self.rules_state()
        invalid_cases = (
            ("empty name", self.rule_fields(name="   ")),
            ("short anchor", self.rule_fields(anchor="ab")),
            ("generic anchor", self.rule_fields(anchor="rendez-vous")),
            (
                "unknown kind",
                self.rule_fields(kind="rss", anchor="Annonce locale"),
            ),
            (
                "invalid window",
                self.rule_fields(
                    kind=RULE_KIND_UPDATE_TIMESTAMP,
                    label="mise à jour",
                    window="abc",
                ),
            ),
            (
                "no element source",
                self.rule_fields(
                    kind=RULE_KIND_NEW_ELEMENT, sources=[],
                ),
            ),
            (
                "duplicate name",
                self.rule_fields(name="  Bloc d'annonce de fermeture  "),
            ),
        )

        for label, fields in invalid_cases:
            with self.subTest(case=label):
                response = self.post("/rules", fields)
                self.assertEqual(response.status, 400)
                self.assertEqual(self.rules_state(), before)

        self.assertEqual(self.store.list_checks(), [])
        self.assertEqual(self.store.list_refreeze_events(), [])

    def test_invalid_edit_persists_nothing(self) -> None:
        before = self.rules_state()

        response = self.post(
            f"/rules/{RULE_ID_UPDATE_TIMESTAMP}",
            self.rule_fields(
                name="Horodatage",
                kind=RULE_KIND_UPDATE_TIMESTAMP,
                label="mise à jour",
                window="0",
            ),
        )

        self.assertEqual(response.status, 400)
        self.assertIn("La fenêtre doit être un entier", response.text)
        self.assertEqual(self.rules_state(), before)

    def test_duplicate_name_on_edit_is_rejected(self) -> None:
        before = self.rules_state()

        response = self.post(
            f"/rules/{RULE_ID_UPDATE_TIMESTAMP}",
            self.rule_fields(
                name="  bloc D'ANNONCE de fermeture  ",
                kind=RULE_KIND_UPDATE_TIMESTAMP,
                label="mise à jour",
                window="160",
            ),
        )

        self.assertEqual(response.status, 400)
        self.assertIn("Une autre règle porte déjà ce nom", response.text)
        self.assertEqual(self.rules_state(), before)

    def test_generic_parameter_is_reported_in_french(self) -> None:
        response = self.post(
            "/rules", self.rule_fields(anchor="Calendrier")
        )

        self.assertEqual(response.status, 400)
        self.assertIn("mot générique", response.text)
        self.assertEqual(
            len(self.store.list_rules()), 3
        )

    def test_csrf_is_required_on_every_post(self) -> None:
        before = self.rules_state()
        body = urlencode(self.rule_fields(), doseq=True).encode("utf-8")

        missing_cookie = request(
            self.app,
            "/rules",
            method="POST",
            body=body,
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={self.token}",
        )
        missing_field = request(
            self.app,
            "/rules",
            method="POST",
            body=b"name=Invalide&kind=closure_block&anchor=Annonce+locale",
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={self.token}",
        )
        wrong_field = request(
            self.app,
            "/rules",
            method="POST",
            body=b"csrf_token=" + b"y" * 43,
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={self.token}",
        )
        toggle_without_token = request(
            self.app,
            f"/rules/{RULE_ID_CLOSURE_BLOCK}/disable",
            method="POST",
            body=b"",
            content_type="application/x-www-form-urlencoded",
            cookie=f"{CSRF_COOKIE}={self.token}",
        )

        for response in (
            missing_cookie,
            missing_field,
            wrong_field,
            toggle_without_token,
        ):
            self.assertEqual(response.status, 403)
        self.assertEqual(self.rules_state(), before)


class IsolationTests(RuleAdminTestCase):
    def _capture_reference(self):
        return self.store.save_reference(
            ReferenceSeed(
                target_url=BASE_URL,
                snapshot=snapshot_of(fixture_html()),
                operator_label="opérateur",
                captured_at=utc_now_iso(),
            )
        )

    def test_rule_changes_never_touch_the_reference_or_create_checks(self) -> None:
        reference = self._capture_reference()

        self.post("/rules", self.rule_fields(name="Règle ajoutée"))
        self.post(
            f"/rules/{RULE_ID_CLOSURE_BLOCK}",
            self.rule_fields(name="Bloc modifié", anchor="Complet 2026"),
        )
        self.post(f"/rules/{RULE_ID_UPDATE_TIMESTAMP}/disable", {})
        self.post(f"/rules/{RULE_ID_UPDATE_TIMESTAMP}/enable", {})

        current = self.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, reference.id)
        self.assertEqual(current.content_hash, reference.content_hash)
        self.assertEqual(current.normalized_snapshot, reference.normalized_snapshot)
        self.assertEqual(len(self.store.list_references()), 1)
        self.assertEqual(self.store.list_checks(), [])
        self.assertIsNone(self.store.latest_check())
        self.assertEqual(self.store.list_refreeze_events(), [])

    def test_rule_change_takes_effect_on_the_next_check_only(self) -> None:
        self._capture_reference()
        changed_html = fixture_html().replace(
            "<strong>Complet 2026</strong>",
            "<strong>Complet 2026 — liste d'attente</strong>",
        )
        events: list[ChangeEvent] = []
        fetcher = StaticFetcher(success(changed_html))
        service = CheckService(
            self.config,
            self.store,
            fetcher=fetcher,  # type: ignore[arg-type]
            change_sink=events.append,
        )

        self.post(f"/rules/{RULE_ID_CLOSURE_BLOCK}/disable", {})
        # Administrating rules must not run a check or raise an alert.
        self.assertEqual(fetcher.calls, 0)
        self.assertEqual(events, [])

        disabled_run = service.run_once()
        self.assertEqual(disabled_run.outcome, OUTCOME_UNCHANGED)
        statuses = {result.rule_id: result.status for result in disabled_run.results}
        self.assertEqual(statuses[RULE_ID_CLOSURE_BLOCK], "disabled")
        self.assertEqual(events, [])

        self.post(f"/rules/{RULE_ID_CLOSURE_BLOCK}/enable", {})
        enabled_run = service.run_once()

        self.assertEqual(enabled_run.outcome, OUTCOME_CHANGED)
        self.assertIn(RULE_ID_CLOSURE_BLOCK, enabled_run.triggered_rule_ids)
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
