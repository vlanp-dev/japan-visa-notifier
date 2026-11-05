from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.detection.canonical import canonicalize_page
from app.detection.engine import RuleResult
from app.detection.rules import (
    DEFAULT_RULE_IDS,
    RULE_ID_CLOSURE_BLOCK,
    RULE_KIND_CLOSURE_BLOCK,
    Rule,
)
from app.detection.store import ReferenceSeed, Store, utc_now_iso
from app.db import connect

BASE_URL = "https://exemple.fr/calendrier"


def make_snapshot(text: str):
    return canonicalize_page(
        f"<html><body><p>{text}</p></body></html>",
        base_url=BASE_URL,
    )


def make_seed(text: str = "Complet 2026", **overrides) -> ReferenceSeed:
    values = {
        "target_url": BASE_URL,
        "snapshot": make_snapshot(text),
        "operator_label": "opérateur",
        "captured_at": utc_now_iso(),
    }
    values.update(overrides)
    return ReferenceSeed(**values)


class StoreRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database_path = str(
            Path(self._directory.name) / "detection.sqlite3"
        )
        self.store = Store(self.database_path)

    def test_seeding_defaults_is_idempotent(self) -> None:
        first = self.store.seed_default_rules()
        second = self.store.seed_default_rules()

        rules = self.store.list_rules()
        self.assertEqual(first, 3)
        self.assertEqual(second, 0)
        self.assertEqual({rule.id for rule in rules}, set(DEFAULT_RULE_IDS))
        self.assertTrue(all(rule.enabled for rule in rules))

    def test_seeding_never_overwrites_operator_edits(self) -> None:
        self.store.seed_default_rules()
        original = self.store.get_rule(RULE_ID_CLOSURE_BLOCK)
        assert original is not None
        edited = self.store.update_rule(
            replace(
                original,
                name="Annonce suivie par l'opérateur",
                enabled=False,
                parameters={"anchor": "Complet 2026"},
            )
        )

        self.store.seed_default_rules()

        reloaded = self.store.get_rule(RULE_ID_CLOSURE_BLOCK)
        self.assertEqual(reloaded, edited)
        self.assertFalse(reloaded.enabled)
        self.assertEqual(
            reloaded.name, "Annonce suivie par l'opérateur"
        )

    def test_add_update_and_toggle_rules(self) -> None:
        self.store.seed_default_rules()
        custom = Rule(
            id="custom-closure",
            name="Annonce locale",
            kind=RULE_KIND_CLOSURE_BLOCK,
            enabled=True,
            parameters={"anchor": "Fermé jusqu'au 3 mars"},
        )
        self.store.add_rule(custom)
        self.assertEqual(self.store.get_rule("custom-closure"), custom)

        updated = self.store.update_rule(
            replace(
                custom,
                name="Annonce locale mise à jour",
                parameters={"anchor": "Fermé jusqu'au 4 mars"},
            )
        )
        self.assertEqual(
            self.store.get_rule("custom-closure"), updated
        )

        disabled = self.store.set_rule_enabled("custom-closure", False)
        self.assertFalse(disabled.enabled)
        self.assertEqual(len(self.store.list_rules()), 4)

    def test_unknown_rule_cannot_be_updated(self) -> None:
        with self.assertRaises(KeyError):
            self.store.update_rule(
                Rule(
                    id="missing",
                    name="Manquante",
                    kind=RULE_KIND_CLOSURE_BLOCK,
                    parameters={"anchor": "Complet 2026"},
                )
            )
        with self.assertRaises(KeyError):
            self.store.set_rule_enabled("missing", False)


class StoreReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database_path = str(
            Path(self._directory.name) / "detection.sqlite3"
        )
        self.store = Store(self.database_path)

    def test_save_reference_supersedes_the_previous_one(self) -> None:
        first = self.store.save_reference(make_seed("Complet 2026"))
        second = self.store.save_reference(make_seed("Ouvert 2027"))

        self.assertTrue(first.is_current)
        self.assertEqual(second.previous_reference_id, first.id)
        self.assertTrue(second.is_current)

        superseded = self.store.get_reference(first.id)
        self.assertIsNotNone(superseded)
        assert superseded is not None
        self.assertFalse(superseded.is_current)

        current = self.store.current_reference()
        assert current is not None
        self.assertEqual(current.id, second.id)
        self.assertEqual(
            current.content_hash, make_snapshot("Ouvert 2027").digest()
        )
        self.assertEqual(len(self.store.list_references()), 2)

    def test_record_check_and_results_are_atomic(self) -> None:
        results = (
            RuleResult(
                rule_id="r1",
                rule_name="Bloc",
                kind="closure_block",
                status="not_triggered",
                detail="identique",
            ),
        )
        check = self.store.record_check(
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            outcome="unchanged",
            reason="reference_initialized",
            target_url=BASE_URL,
            results=results,
            new_reference=make_seed(),
        )

        self.assertIsNotNone(check.reference_id)
        self.assertIsNotNone(self.store.current_reference())
        stored = self.store.results_for_check(check.id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].rule_id, "r1")
        latest = self.store.latest_check()
        assert latest is not None
        self.assertEqual(latest.id, check.id)

    def test_checks_and_results_survive_reopening_the_store(self) -> None:
        check = self.store.record_check(
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            outcome="changed",
            target_url=BASE_URL,
            duration_ms=12,
            results=(
                RuleResult(
                    rule_id="r1",
                    rule_name="Bloc",
                    kind="closure_block",
                    status="triggered",
                    current_excerpt="Ouvert",
                    reference_excerpt="Complet 2026",
                    detail="changé",
                ),
            ),
        )

        reopened = Store(self.database_path)

        latest = reopened.latest_check()
        assert latest is not None
        self.assertEqual(latest.id, check.id)
        results = reopened.results_for_check(check.id)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "triggered")
        self.assertEqual(results[0].reference_excerpt, "Complet 2026")

    def test_refreeze_events_are_listed_newest_first(self) -> None:
        reference = self.store.save_reference(make_seed())
        first = self.store.record_refreeze_event(
            success=False,
            operator_label="opérateur",
            message="récupération impossible",
        )
        second = self.store.record_refreeze_event(
            success=True,
            operator_label="opérateur",
            new_reference_id=reference.id,
        )

        events = self.store.list_refreeze_events()
        self.assertEqual([event.id for event in events], [second.id, first.id])
        self.assertFalse(events[1].success)
        self.assertTrue(events[0].success)


class StoreValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database_path = str(
            Path(self._directory.name) / "detection.sqlite3"
        )
        self.store = Store(self.database_path)

    def test_invalid_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.list_checks(0)
        with self.assertRaises(ValueError):
            self.store.list_references(-1)
        with self.assertRaises(ValueError):
            self.store.list_refreeze_events(True)  # type: ignore[arg-type]

    def test_only_one_current_reference_is_persisted(self) -> None:
        self.store.save_reference(make_seed("A"))
        self.store.save_reference(make_seed("B"))
        connection = connect(self.database_path)
        try:
            current = connection.execute(
                "SELECT COUNT(*) FROM page_references WHERE is_current = 1"
            ).fetchone()[0]
            total = connection.execute(
                "SELECT COUNT(*) FROM page_references"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(current, 1)
        self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()
