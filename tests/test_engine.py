from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from app.detection.canonical import CanonicalSnapshot, canonicalize_page
from app.detection.engine import (
    STATUS_DISABLED,
    STATUS_NOT_EVALUATED,
    STATUS_NOT_TRIGGERED,
    STATUS_TRIGGERED,
    EvaluationError,
    evaluate,
    evaluate_rule,
)
from app.detection.rules import (
    DEFAULT_RULES,
    RULE_ID_CLOSURE_BLOCK,
    RULE_ID_NEW_ELEMENT,
    RULE_ID_UPDATE_TIMESTAMP,
    RULE_KIND_CLOSURE_BLOCK,
    RULE_KIND_UPDATE_TIMESTAMP,
    Rule,
    default_rules,
)

FIXTURES = Path(__file__).with_name("fixtures")
BASE_URL = "https://exemple.fr/calendrier"


def fixture_html() -> str:
    return (FIXTURES / "closed_page.html").read_text(encoding="utf-8")


def snapshot(html: str) -> CanonicalSnapshot:
    return canonicalize_page(html, base_url=BASE_URL)


def statuses(evaluation) -> dict[str, str]:
    return {result.rule_id: result.status for result in evaluation.results}


class BaselineRuleTests(unittest.TestCase):
    def test_untouched_closed_page_triggers_nothing(self) -> None:
        reference = snapshot(fixture_html())
        current = snapshot(fixture_html())

        evaluation = evaluate(reference, current, default_rules())

        self.assertFalse(evaluation.changed)
        self.assertEqual(evaluation.outcome, "unchanged")
        self.assertEqual(len(evaluation.results), 3)
        self.assertEqual(
            statuses(evaluation),
            {
                RULE_ID_CLOSURE_BLOCK: STATUS_NOT_TRIGGERED,
                RULE_ID_UPDATE_TIMESTAMP: STATUS_NOT_TRIGGERED,
                RULE_ID_NEW_ELEMENT: STATUS_NOT_TRIGGERED,
            },
        )
        self.assertEqual(evaluation.triggered_rule_ids, ())

    def test_baseline_results_expose_bounded_reference_excerpts(self) -> None:
        reference = snapshot(fixture_html())
        evaluation = evaluate(reference, reference, default_rules())

        closure = evaluation.result_for(RULE_ID_CLOSURE_BLOCK)
        timestamp = evaluation.result_for(RULE_ID_UPDATE_TIMESTAMP)
        self.assertIsNotNone(closure)
        self.assertIsNotNone(timestamp)
        self.assertIn("Complet 2026", closure.reference_excerpt)
        self.assertIn("mise à jour", timestamp.reference_excerpt)
        self.assertIn("2026", timestamp.current_excerpt)
        for result in evaluation.results:
            self.assertLessEqual(len(result.current_excerpt), 160)
            self.assertLessEqual(len(result.reference_excerpt), 160)

    def test_the_closed_page_lexical_words_do_not_trigger_a_rule(self) -> None:
        # The fixture contains "rendez-vous" and "calendrier" already.  Adding
        # more of that vocabulary outside the announcement block must not
        # trigger any default rule.
        html = fixture_html()
        self.assertIn("rendez-vous", html.casefold())
        self.assertIn("calendrier", html.casefold())
        changed = html.replace(
            "La prise de rendez-vous est momentanément fermée.",
            "La prise de rendez-vous est momentanément fermée. Calendrier.",
        )

        evaluation = evaluate(snapshot(html), snapshot(changed), default_rules())

        self.assertFalse(evaluation.changed)
        for result in evaluation.results:
            self.assertNotEqual(result.status, STATUS_TRIGGERED)


class ClosureBlockRuleTests(unittest.TestCase):
    def test_modified_announcement_triggers_only_the_closure_rule(self) -> None:
        html = fixture_html()
        changed = html.replace(
            "<strong>Complet 2026</strong>",
            "<strong>Complet 2026 — liste d'attente</strong>",
        )

        evaluation = evaluate(snapshot(html), snapshot(changed), default_rules())

        self.assertTrue(evaluation.changed)
        self.assertEqual(
            statuses(evaluation),
            {
                RULE_ID_CLOSURE_BLOCK: STATUS_TRIGGERED,
                RULE_ID_UPDATE_TIMESTAMP: STATUS_NOT_TRIGGERED,
                RULE_ID_NEW_ELEMENT: STATUS_NOT_TRIGGERED,
            },
        )
        result = evaluation.result_for(RULE_ID_CLOSURE_BLOCK)
        self.assertIn("liste d'attente", result.current_excerpt)
        self.assertIn("Complet 2026", result.reference_excerpt)

    def test_removed_announcement_block_is_reported_as_missing(self) -> None:
        html = fixture_html()
        changed = html.replace(
            "<p><strong>Complet 2026</strong></p>",
            "<p><strong>Fermé</strong></p>",
        )

        evaluation = evaluate(snapshot(html), snapshot(changed), default_rules())

        self.assertTrue(evaluation.changed)
        result = evaluation.result_for(RULE_ID_CLOSURE_BLOCK)
        self.assertEqual(result.status, STATUS_TRIGGERED)
        self.assertIn("disparu", result.detail)
        self.assertEqual(result.current_excerpt, "")
        self.assertIn("Complet 2026", result.reference_excerpt)

    def test_custom_anchor_is_compared_against_visible_blocks(self) -> None:
        reference = canonicalize_page(
            "<div><p>Fermé jusqu'à nouvel ordre</p></div>",
            closure_anchor="Fermé",
        )
        current = canonicalize_page(
            "<div><p>Ouvert</p></div>", closure_anchor="Fermé"
        )
        rule = Rule(
            id="custom-closure",
            name="Annonce personnalisée",
            kind=RULE_KIND_CLOSURE_BLOCK,
            enabled=True,
            parameters={"anchor": "Fermé"},
        )

        result = evaluate_rule(rule, reference, current)

        self.assertEqual(result.status, STATUS_TRIGGERED)

    def test_missing_reference_anchor_is_not_evaluated(self) -> None:
        plain = "<html><body><p>Page sans annonce</p></body></html>"
        evaluation = evaluate(snapshot(plain), snapshot(plain), default_rules())

        self.assertEqual(
            evaluation.result_for(RULE_ID_CLOSURE_BLOCK).status,
            STATUS_NOT_EVALUATED,
        )
        self.assertFalse(evaluation.changed)


class UpdateTimestampRuleTests(unittest.TestCase):
    def test_changed_timestamp_triggers_only_the_timestamp_rule(self) -> None:
        html = fixture_html()
        changed = html.replace("09 h 30", "10 h 15")

        evaluation = evaluate(snapshot(html), snapshot(changed), default_rules())

        self.assertTrue(evaluation.changed)
        self.assertEqual(
            statuses(evaluation),
            {
                RULE_ID_CLOSURE_BLOCK: STATUS_NOT_TRIGGERED,
                RULE_ID_UPDATE_TIMESTAMP: STATUS_TRIGGERED,
                RULE_ID_NEW_ELEMENT: STATUS_NOT_TRIGGERED,
            },
        )
        result = evaluation.result_for(RULE_ID_UPDATE_TIMESTAMP)
        self.assertIn("10 h 15", result.current_excerpt)
        self.assertIn("09 h 30", result.reference_excerpt)

    def test_missing_timestamp_is_reported(self) -> None:
        html = fixture_html()
        changed = html.replace(
            "Dernière mise à jour", "Dernière actualisation"
        )

        evaluation = evaluate(snapshot(html), snapshot(changed), default_rules())

        result = evaluation.result_for(RULE_ID_UPDATE_TIMESTAMP)
        self.assertEqual(result.status, STATUS_TRIGGERED)
        self.assertIn("disparu", result.detail)
        self.assertEqual(result.current_excerpt, "")

    def test_custom_label_and_window_are_used(self) -> None:
        reference = canonicalize_page(
            "<p>Ouverture prévue le 5 avril 2026</p>",
            timestamp_label="Ouverture prévue",
        )
        current = canonicalize_page(
            "<p>Ouverture prévue le 6 avril 2026</p>",
            timestamp_label="Ouverture prévue",
        )
        rule = Rule(
            id="custom-timestamp",
            name="Ouverture prévue",
            kind=RULE_KIND_UPDATE_TIMESTAMP,
            enabled=True,
            parameters={"label": "Ouverture prévue", "window": 160},
        )

        result = evaluate_rule(rule, reference, current)

        self.assertEqual(result.status, STATUS_TRIGGERED)
        self.assertIn("6 avril 2026", result.current_excerpt)

    def test_missing_reference_timestamp_is_not_evaluated(self) -> None:
        plain = "<html><body><p>Page sans horodatage</p></body></html>"
        evaluation = evaluate(snapshot(plain), snapshot(plain), default_rules())

        self.assertEqual(
            evaluation.result_for(RULE_ID_UPDATE_TIMESTAMP).status,
            STATUS_NOT_EVALUATED,
        )


class NewElementRuleTests(unittest.TestCase):
    def test_new_booking_link_triggers_only_the_new_element_rule(self) -> None:
        html = fixture_html()
        changed = html.replace(
            "</ul>",
            (
                '<li><a href="https://exemple.fr/reservation?periode=2027">'
                "Réserver pour 2027</a></li>\n  </ul>"
            ),
        )

        evaluation = evaluate(snapshot(html), snapshot(changed), default_rules())

        self.assertEqual(
            statuses(evaluation),
            {
                RULE_ID_CLOSURE_BLOCK: STATUS_NOT_TRIGGERED,
                RULE_ID_UPDATE_TIMESTAMP: STATUS_NOT_TRIGGERED,
                RULE_ID_NEW_ELEMENT: STATUS_TRIGGERED,
            },
        )
        self.assertTrue(evaluation.changed)
        result = evaluation.result_for(RULE_ID_NEW_ELEMENT)
        self.assertIn("lien", result.detail)
        self.assertIn("2027", result.current_excerpt)

    def test_new_dated_document_triggers_the_new_element_rule(self) -> None:
        html = fixture_html()
        changed = html.replace(
            "</ul>",
            (
                '<li><a href="/documents/calendrier-2027-01-05.pdf">'
                "Calendrier de janvier 2027 (PDF)</a></li>\n  </ul>"
            ),
        )

        evaluation = evaluate(snapshot(html), snapshot(changed), default_rules())

        result = evaluation.result_for(RULE_ID_NEW_ELEMENT)
        self.assertEqual(result.status, STATUS_TRIGGERED)
        self.assertIn("document", result.detail)
        self.assertIn("janvier 2027", result.current_excerpt)

    def test_new_time_element_triggers_the_new_element_rule(self) -> None:
        html = fixture_html()
        changed = html.replace(
            "</body>",
            (
                '<time datetime="2027-01-05T09:00:00+01:00">'
                "5 janvier 2027</time>\n</body>"
            ),
        )

        evaluation = evaluate(snapshot(html), snapshot(changed), default_rules())

        result = evaluation.result_for(RULE_ID_NEW_ELEMENT)
        self.assertEqual(result.status, STATUS_TRIGGERED)
        self.assertIn("élément daté", result.detail)
        self.assertIn("5 janvier 2027", result.current_excerpt)

    def test_only_configured_element_sources_are_watched(self) -> None:
        html = fixture_html()
        changed = html.replace(
            "</ul>",
            (
                '<li><a href="https://exemple.fr/reservation?periode=2027">'
                "Réserver pour 2027</a></li>\n  </ul>"
            ),
        )
        time_only = Rule(
            id="time-only",
            name="Éléments datés seulement",
            kind="new_element",
            enabled=True,
            parameters={"sources": ["time"]},
        )

        evaluation = evaluate(snapshot(html), snapshot(changed), [time_only])

        self.assertEqual(
            evaluation.results[0].status, STATUS_NOT_TRIGGERED
        )
        self.assertFalse(evaluation.changed)


class EvaluationCombinationTests(unittest.TestCase):
    def test_simultaneous_alterations_trigger_every_rule(self) -> None:
        html = fixture_html()
        changed = (
            html.replace(
                "<strong>Complet 2026</strong>",
                "<strong>Complet 2026 — complet</strong>",
            )
            .replace("09 h 30", "11 h 00")
            .replace(
                "</ul>",
                (
                    '<li><a href="https://exemple.fr/reservation?periode=2027">'
                    "Réserver pour 2027</a></li>\n  </ul>"
                ),
            )
        )

        evaluation = evaluate(snapshot(html), snapshot(changed), default_rules())

        self.assertTrue(evaluation.changed)
        self.assertEqual(
            set(evaluation.triggered_rule_ids),
            {
                RULE_ID_CLOSURE_BLOCK,
                RULE_ID_UPDATE_TIMESTAMP,
                RULE_ID_NEW_ELEMENT,
            },
        )

    def test_disabled_rule_keeps_its_result_but_does_not_change(self) -> None:
        html = fixture_html()
        changed = html.replace(
            "<strong>Complet 2026</strong>",
            "<strong>Complet 2026 — liste d'attente</strong>",
        )
        rules = [
            replace(DEFAULT_RULES[0], enabled=False),
            DEFAULT_RULES[1],
            DEFAULT_RULES[2],
        ]

        evaluation = evaluate(snapshot(html), snapshot(changed), rules)

        self.assertEqual(
            evaluation.result_for(RULE_ID_CLOSURE_BLOCK).status,
            STATUS_DISABLED,
        )
        self.assertFalse(evaluation.changed)

    def test_all_rules_disabled_never_reports_a_change(self) -> None:
        html = fixture_html()
        changed = html.replace("Complet 2026", "Ouvert 2027")
        rules = [replace(rule, enabled=False) for rule in DEFAULT_RULES]

        evaluation = evaluate(snapshot(html), snapshot(changed), rules)

        self.assertTrue(
            all(result.status == STATUS_DISABLED for result in evaluation.results)
        )
        self.assertFalse(evaluation.changed)

    def test_invalid_rule_is_not_evaluated_not_triggered(self) -> None:
        reference = snapshot(fixture_html())
        invalid = Rule(
            id="invalid",
            name="Règle invalide",
            kind=RULE_KIND_CLOSURE_BLOCK,
            enabled=True,
            parameters={"anchor": ""},
        )

        result = evaluate_rule(invalid, reference, reference)

        self.assertEqual(result.status, STATUS_NOT_EVALUATED)
        self.assertIn("invalide", result.detail.lower())

        mixed = evaluate(
            reference,
            reference,
            [
                {
                    "id": "broken",
                    "name": "Règle cassée",
                    "kind": "unknown",
                    "enabled": True,
                    "parameters": {},
                },
                DEFAULT_RULES[0],
            ],
        )
        self.assertEqual(mixed.results[0].status, STATUS_NOT_EVALUATED)
        self.assertEqual(mixed.results[1].status, STATUS_NOT_TRIGGERED)
        self.assertFalse(mixed.changed)

    def test_excerpts_respect_the_requested_bound(self) -> None:
        reference = canonicalize_page(
            "<div><p>Complet 2026 " + "a" * 500 + "</p></div>"
        )
        current = canonicalize_page(
            "<div><p>Complet 2026 " + "b" * 500 + "</p></div>"
        )

        evaluation = evaluate(
            reference, current, default_rules(), excerpt_limit=40
        )
        result = evaluation.result_for(RULE_ID_CLOSURE_BLOCK)

        self.assertEqual(result.status, STATUS_TRIGGERED)
        self.assertLessEqual(len(result.current_excerpt), 40)
        self.assertLessEqual(len(result.reference_excerpt), 40)
        self.assertNotIn("\n", result.current_excerpt)

    def test_snapshots_may_be_passed_as_json_or_mappings(self) -> None:
        reference = snapshot(fixture_html())
        current = snapshot(fixture_html())

        from_json = evaluate(
            reference.to_json(), current.to_json(), default_rules()
        )
        from_mapping = evaluate(
            reference.to_dict(), current.to_dict(), default_rules()
        )

        self.assertFalse(from_json.changed)
        self.assertFalse(from_mapping.changed)

        with self.assertRaises(EvaluationError):
            evaluate(reference, "<not-json>", default_rules())
        with self.assertRaises(EvaluationError):
            evaluate(42, current, default_rules())  # type: ignore[arg-type]
        with self.assertRaises(EvaluationError):
            evaluate(reference, current, default_rules(), excerpt_limit=0)

    def test_evaluation_serializes_to_a_plain_mapping(self) -> None:
        reference = snapshot(fixture_html())
        payload = evaluate(reference, reference, default_rules()).to_dict()

        self.assertFalse(payload["changed"])
        self.assertEqual(payload["outcome"], "unchanged")
        self.assertEqual(len(payload["results"]), 3)
        for result in payload["results"]:
            self.assertEqual(
                set(result),
                {
                    "rule_id",
                    "rule_name",
                    "kind",
                    "status",
                    "current_excerpt",
                    "reference_excerpt",
                    "detail",
                },
            )


if __name__ == "__main__":
    unittest.main()
