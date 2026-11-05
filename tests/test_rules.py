from __future__ import annotations

import json
import unittest

from app.detection.rules import (
    DEFAULT_RULE_IDS,
    DEFAULT_RULES,
    ELEMENT_SOURCES,
    RULE_KIND_CLOSURE_BLOCK,
    RULE_KIND_NEW_ELEMENT,
    RULE_KIND_UPDATE_TIMESTAMP,
    RULE_KINDS,
    Rule,
    RuleValidationError,
    decode_parameters,
    default_rules,
    normalize_parameters,
    normalize_rule,
    validate_parameters,
    validate_rule,
)


class DefaultRuleTests(unittest.TestCase):
    def test_three_stable_default_rules_are_enabled(self) -> None:
        self.assertEqual(len(DEFAULT_RULES), 3)
        self.assertEqual(
            tuple(rule.id for rule in DEFAULT_RULES), DEFAULT_RULE_IDS
        )
        self.assertEqual(len({rule.id for rule in DEFAULT_RULES}), 3)
        self.assertEqual(len({rule.name for rule in DEFAULT_RULES}), 3)
        self.assertEqual(
            {rule.kind for rule in DEFAULT_RULES}, set(RULE_KINDS)
        )
        for rule in DEFAULT_RULES:
            self.assertTrue(rule.enabled)
            validate_rule(rule)

    def test_default_parameters_are_the_expected_detection_signals(self) -> None:
        closure, timestamp, new_element = DEFAULT_RULES

        self.assertEqual(closure.kind, RULE_KIND_CLOSURE_BLOCK)
        self.assertEqual(closure.parameters, {"anchor": "Complet 2026"})

        self.assertEqual(timestamp.kind, RULE_KIND_UPDATE_TIMESTAMP)
        self.assertEqual(timestamp.parameters["label"], "mise à jour")
        self.assertGreaterEqual(timestamp.parameters["window"], 1)

        self.assertEqual(new_element.kind, RULE_KIND_NEW_ELEMENT)
        self.assertEqual(
            set(new_element.parameters["sources"]), set(ELEMENT_SOURCES)
        )

    def test_default_rules_are_not_naive_lexical_rules(self) -> None:
        serialized = json.dumps(
            [rule.to_dict() for rule in DEFAULT_RULES], ensure_ascii=False
        ).casefold()
        for forbidden in ("rendez-vous", "rendez vous", "calendrier"):
            self.assertNotIn(forbidden, serialized)

    def test_default_rules_are_fresh_copies(self) -> None:
        first = default_rules()
        second = default_rules()
        self.assertIsNot(first, second)
        self.assertIsNot(first[0].parameters, second[0].parameters)

        mutable = first[2].parameters["sources"]
        mutable.append("link")
        self.assertEqual(
            second[2].parameters["sources"], list(ELEMENT_SOURCES)
        )
        self.assertEqual(
            DEFAULT_RULES[2].parameters["sources"],
            list(ELEMENT_SOURCES),
        )

    def test_rule_json_and_row_round_trip(self) -> None:
        for rule in DEFAULT_RULES:
            self.assertEqual(Rule.from_dict(rule.to_dict()), rule)
            row = {
                "id": rule.id,
                "name": rule.name,
                "kind": rule.kind,
                "enabled": 1 if rule.enabled else 0,
                "parameters": rule.parameters_json(),
            }
            self.assertEqual(Rule.from_row(row), rule)

        decoded = decode_parameters({"anchor": "Complet 2026"})
        self.assertEqual(decoded, {"anchor": "Complet 2026"})


class RuleParameterValidationTests(unittest.TestCase):
    def test_closure_anchor_is_validated(self) -> None:
        for anchor in ("", "  ", "ab", 42, "Complet\n2026"):
            with self.subTest(anchor=anchor):
                with self.assertRaises(RuleValidationError):
                    normalize_parameters(
                        RULE_KIND_CLOSURE_BLOCK, {"anchor": anchor}
                    )

        with self.assertRaises(RuleValidationError):
            normalize_parameters(
                RULE_KIND_CLOSURE_BLOCK, {"anchor": "Complet 2026", "x": 1}
            )
        with self.assertRaises(RuleValidationError):
            normalize_parameters(RULE_KIND_CLOSURE_BLOCK, [])

    def test_bare_generic_lexical_parameters_are_rejected(self) -> None:
        for anchor in ("rendez-vous", "Rendez-Vous", "calendrier", "CALENDRIER"):
            with self.subTest(anchor=anchor):
                with self.assertRaises(RuleValidationError):
                    normalize_parameters(
                        RULE_KIND_CLOSURE_BLOCK, {"anchor": anchor}
                    )
                with self.assertRaises(RuleValidationError):
                    normalize_parameters(
                        RULE_KIND_UPDATE_TIMESTAMP, {"label": anchor}
                    )

        # A specific heading containing generic vocabulary is still allowed:
        # only a rule reduced to the generic word itself is refused.
        self.assertEqual(
            normalize_parameters(
                RULE_KIND_CLOSURE_BLOCK,
                {"anchor": "Complet 2026"},
            ),
            {"anchor": "Complet 2026"},
        )

    def test_timestamp_parameters_are_validated(self) -> None:
        self.assertEqual(
            normalize_parameters(
                RULE_KIND_UPDATE_TIMESTAMP, {"label": "  mise à jour  "}
            ),
            {"label": "mise à jour", "window": 160},
        )
        normalized = normalize_parameters(
            RULE_KIND_UPDATE_TIMESTAMP, {"label": "mise à jour", "window": "250"}
        )
        self.assertEqual(normalized["window"], 250)

        for window in (0, -1, True, 2.5, "beaucoup", 5000):
            with self.subTest(window=window):
                with self.assertRaises(RuleValidationError):
                    normalize_parameters(
                        RULE_KIND_UPDATE_TIMESTAMP,
                        {"label": "mise à jour", "window": window},
                    )

    def test_new_element_sources_are_validated(self) -> None:
        self.assertEqual(
            normalize_parameters(
                RULE_KIND_NEW_ELEMENT, {"sources": ["time", "link", "time"]}
            ),
            {"sources": ["time", "link"]},
        )
        self.assertEqual(
            normalize_parameters(
                RULE_KIND_NEW_ELEMENT, {"sources": "link, document"}
            ),
            {"sources": ["link", "document"]},
        )

        for sources in ([], ["video"], [1], "inconnu", None):
            with self.subTest(sources=sources):
                with self.assertRaises(RuleValidationError):
                    normalize_parameters(
                        RULE_KIND_NEW_ELEMENT, {"sources": sources}
                    )

    def test_rule_identity_kind_and_enabled_are_validated(self) -> None:
        valid = Rule(
            id="custom-rule",
            name="Règle personnalisée",
            kind=RULE_KIND_CLOSURE_BLOCK,
            enabled=True,
            parameters={"anchor": "Complet 2026"},
        )
        self.assertEqual(normalize_rule(valid).id, "custom-rule")

        invalid_rules = (
            Rule("", "Nom", RULE_KIND_CLOSURE_BLOCK, True, {"anchor": "ok!"}),
            Rule("Bad ID", "Nom", RULE_KIND_CLOSURE_BLOCK, True, {"anchor": "ok!"}),
            Rule("ok-id", "", RULE_KIND_CLOSURE_BLOCK, True, {"anchor": "ok!"}),
            Rule("ok-id", "Nom", "unknown", True, {}),
            Rule("ok-id", "Nom", RULE_KIND_CLOSURE_BLOCK, 1, {"anchor": "ok!"}),
        )
        for rule in invalid_rules:
            with self.subTest(rule=rule):
                with self.assertRaises(RuleValidationError):
                    normalize_rule(rule)

    def test_stored_parameter_json_failures_are_reported(self) -> None:
        with self.assertRaises(RuleValidationError):
            decode_parameters("{not json")
        with self.assertRaises(RuleValidationError):
            decode_parameters("[1, 2]")
        with self.assertRaises(RuleValidationError):
            decode_parameters(42)

    def test_validate_parameters_helper(self) -> None:
        self.assertIsNone(
            validate_parameters(
                RULE_KIND_NEW_ELEMENT, {"sources": ["link", "document", "time"]}
            )
        )


if __name__ == "__main__":
    unittest.main()
