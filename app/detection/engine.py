"""Independent rule evaluation against a frozen reference snapshot.

Every enabled rule is evaluated on its own, then the outcomes are combined with
a logical OR: a check reports a content change as soon as one rule triggers.
A rule that cannot be evaluated (for example because its baseline anchor is
missing from the reference) never counts as a change.

Page content is untrusted data.  Evaluation only compares already-canonicalized
values; it never executes content, follows a link found in the page or treats
text as an instruction.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .canonical import (
    DEFAULT_EXCERPT_LIMIT,
    TIMESTAMP_PATTERN,
    CanonicalSnapshot,
    SnapshotFormatError,
    single_line_excerpt,
)
from .rules import (
    RULE_KIND_CLOSURE_BLOCK,
    RULE_KIND_NEW_ELEMENT,
    RULE_KIND_UPDATE_TIMESTAMP,
    Rule,
    RuleValidationError,
    normalize_rule,
)

STATUS_TRIGGERED = "triggered"
STATUS_NOT_TRIGGERED = "not_triggered"
STATUS_DISABLED = "disabled"
STATUS_NOT_EVALUATED = "not_evaluated"

RULE_STATUSES: tuple[str, ...] = (
    STATUS_TRIGGERED,
    STATUS_NOT_TRIGGERED,
    STATUS_DISABLED,
    STATUS_NOT_EVALUATED,
)

#: Maximum number of new elements listed in a single rule detail.
_MAX_LISTED_ADDITIONS = 3

_SOURCE_LABELS = {
    "document": "document",
    "link": "lien",
    "time": "élément daté",
}


class EvaluationError(ValueError):
    """Raised when evaluation input is not usable."""


@dataclass(frozen=True)
class RuleResult:
    """Outcome of one rule for one check."""

    rule_id: str
    rule_name: str
    kind: str
    status: str
    current_excerpt: str = ""
    reference_excerpt: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "kind": self.kind,
            "status": self.status,
            "current_excerpt": self.current_excerpt,
            "reference_excerpt": self.reference_excerpt,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Evaluation:
    """Per-rule results and their OR combination."""

    results: tuple[RuleResult, ...]

    @property
    def changed(self) -> bool:
        return any(result.status == STATUS_TRIGGERED for result in self.results)

    @property
    def outcome(self) -> str:
        return "changed" if self.changed else "unchanged"

    @property
    def triggered_rule_ids(self) -> tuple[str, ...]:
        return tuple(
            result.rule_id
            for result in self.results
            if result.status == STATUS_TRIGGERED
        )

    def result_for(self, rule_id: str) -> RuleResult | None:
        for result in self.results:
            if result.rule_id == rule_id:
                return result
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "outcome": self.outcome,
            "results": [result.to_dict() for result in self.results],
        }


def evaluate(
    reference: CanonicalSnapshot | Mapping[str, Any] | str | bytes,
    current: CanonicalSnapshot | Mapping[str, Any] | str | bytes,
    rules: Iterable[Rule | Mapping[str, Any]],
    *,
    excerpt_limit: int = DEFAULT_EXCERPT_LIMIT,
) -> Evaluation:
    """Evaluate every rule against ``reference`` and ``current``.

    ``reference`` and ``current`` may be snapshots or their JSON representation.
    Binding to the OR happens here: :attr:`Evaluation.changed` is true when at
    least one rule result is ``triggered``.  Disabled and not-evaluated rules
    never contribute a change.
    """

    if isinstance(excerpt_limit, bool) or not isinstance(excerpt_limit, int):
        raise EvaluationError("excerpt_limit must be an integer")
    if excerpt_limit < 1:
        raise EvaluationError("excerpt_limit must be at least 1")

    baseline = _coerce_snapshot(reference, "reference")
    observed = _coerce_snapshot(current, "current")

    results: list[RuleResult] = []
    for raw_rule in rules:
        rule = _coerce_rule(raw_rule)
        results.append(
            _evaluate_rule(rule, baseline, observed, excerpt_limit)
        )
    return Evaluation(results=tuple(results))


def evaluate_rules(
    reference: CanonicalSnapshot | Mapping[str, Any] | str | bytes,
    current: CanonicalSnapshot | Mapping[str, Any] | str | bytes,
    rules: Iterable[Rule | Mapping[str, Any]],
    *,
    excerpt_limit: int = DEFAULT_EXCERPT_LIMIT,
) -> Evaluation:
    """Alias for :func:`evaluate` used by callers that name the rules."""

    return evaluate(
        reference,
        current,
        rules,
        excerpt_limit=excerpt_limit,
    )


def evaluate_rule(
    rule: Rule | Mapping[str, Any],
    reference: CanonicalSnapshot | Mapping[str, Any] | str | bytes,
    current: CanonicalSnapshot | Mapping[str, Any] | str | bytes,
    *,
    excerpt_limit: int = DEFAULT_EXCERPT_LIMIT,
) -> RuleResult:
    """Evaluate a single rule; useful for focused checks."""

    if isinstance(excerpt_limit, bool) or not isinstance(excerpt_limit, int):
        raise EvaluationError("excerpt_limit must be an integer")
    if excerpt_limit < 1:
        raise EvaluationError("excerpt_limit must be at least 1")
    return _evaluate_rule(
        _coerce_rule(rule),
        _coerce_snapshot(reference, "reference"),
        _coerce_snapshot(current, "current"),
        excerpt_limit,
    )


def _evaluate_rule(
    rule: Rule,
    reference: CanonicalSnapshot,
    current: CanonicalSnapshot,
    excerpt_limit: int,
) -> RuleResult:
    try:
        normalized = normalize_rule(rule)
    except RuleValidationError as exc:
        return RuleResult(
            rule_id=str(rule.id),
            rule_name=str(rule.name),
            kind=str(rule.kind),
            status=STATUS_NOT_EVALUATED,
            detail=f"Règle invalide : {exc}",
        )

    if not normalized.enabled:
        return RuleResult(
            rule_id=normalized.id,
            rule_name=normalized.name,
            kind=normalized.kind,
            status=STATUS_DISABLED,
            detail="Règle désactivée par l'opérateur.",
        )

    if normalized.kind == RULE_KIND_CLOSURE_BLOCK:
        return _evaluate_closure_block(
            normalized, reference, current, excerpt_limit
        )
    if normalized.kind == RULE_KIND_UPDATE_TIMESTAMP:
        return _evaluate_update_timestamp(
            normalized, reference, current, excerpt_limit
        )
    if normalized.kind == RULE_KIND_NEW_ELEMENT:
        return _evaluate_new_element(
            normalized, reference, current, excerpt_limit
        )
    return RuleResult(
        rule_id=normalized.id,
        rule_name=normalized.name,
        kind=normalized.kind,
        status=STATUS_NOT_EVALUATED,
        detail=f"Type de règle non pris en charge : {normalized.kind}",
    )


def _evaluate_closure_block(
    rule: Rule,
    reference: CanonicalSnapshot,
    current: CanonicalSnapshot,
    excerpt_limit: int,
) -> RuleResult:
    anchor = str(rule.parameters["anchor"])
    reference_hits = _closure_hits(reference, anchor, excerpt_limit)
    current_hits = _closure_hits(current, anchor, excerpt_limit)

    if not reference_hits:
        return _result(
            rule,
            STATUS_NOT_EVALUATED,
            detail=(
                "Ancre d'annonce absente de la référence : "
                "aucune comparaison possible."
            ),
        )

    reference_fingerprints = {fingerprint for fingerprint, _ in reference_hits}
    current_fingerprints = {fingerprint for fingerprint, _ in current_hits}
    reference_excerpt = reference_hits[0][1]

    if not current_hits:
        return _result(
            rule,
            STATUS_TRIGGERED,
            reference_excerpt=reference_excerpt,
            detail="Le bloc d'annonce de fermeture a disparu du contenu actuel.",
        )

    if reference_fingerprints != current_fingerprints:
        return _result(
            rule,
            STATUS_TRIGGERED,
            current_excerpt=current_hits[0][1],
            reference_excerpt=reference_excerpt,
            detail="Le bloc d'annonce de fermeture a changé.",
        )

    return _result(
        rule,
        STATUS_NOT_TRIGGERED,
        current_excerpt=current_hits[0][1],
        reference_excerpt=reference_excerpt,
        detail="Bloc d'annonce de fermeture identique à la référence.",
    )


def _evaluate_update_timestamp(
    rule: Rule,
    reference: CanonicalSnapshot,
    current: CanonicalSnapshot,
    excerpt_limit: int,
) -> RuleResult:
    label = str(rule.parameters["label"])
    window = int(rule.parameters["window"])

    reference_hits = _timestamp_hits(reference, label, window, excerpt_limit)
    current_hits = _timestamp_hits(current, label, window, excerpt_limit)

    if not reference_hits:
        return _result(
            rule,
            STATUS_NOT_EVALUATED,
            detail=(
                "Horodatage absent de la référence : "
                "aucune comparaison possible."
            ),
        )

    reference_fingerprints = {fingerprint for fingerprint, _ in reference_hits}
    current_fingerprints = {fingerprint for fingerprint, _ in current_hits}
    reference_excerpt = reference_hits[0][1]

    if not current_hits:
        return _result(
            rule,
            STATUS_TRIGGERED,
            reference_excerpt=reference_excerpt,
            detail="L'horodatage de mise à jour a disparu du contenu actuel.",
        )

    if reference_fingerprints != current_fingerprints:
        return _result(
            rule,
            STATUS_TRIGGERED,
            current_excerpt=current_hits[0][1],
            reference_excerpt=reference_excerpt,
            detail="L'horodatage de mise à jour a changé.",
        )

    return _result(
        rule,
        STATUS_NOT_TRIGGERED,
        current_excerpt=current_hits[0][1],
        reference_excerpt=reference_excerpt,
        detail="Horodatage de mise à jour identique à la référence.",
    )


def _evaluate_new_element(
    rule: Rule,
    reference: CanonicalSnapshot,
    current: CanonicalSnapshot,
    excerpt_limit: int,
) -> RuleResult:
    sources = tuple(str(source) for source in rule.parameters["sources"])
    additions: list[tuple[str, Any]] = []

    for source in sources:
        reference_fingerprints = {
            item.fingerprint for item in _source_items(reference, source)
        }
        for item in _source_items(current, source):
            if item.fingerprint not in reference_fingerprints:
                additions.append((source, item))

    if not additions:
        return _result(
            rule,
            STATUS_NOT_TRIGGERED,
            detail="Aucun nouvel élément par rapport à la référence.",
        )

    labels = sorted({_SOURCE_LABELS[source] for source, _ in additions})
    excerpts = [
        f"{_SOURCE_LABELS[source]} : {item.excerpt}"
        for source, item in additions[:_MAX_LISTED_ADDITIONS]
    ]
    if len(additions) > _MAX_LISTED_ADDITIONS:
        excerpts.append(f"et {len(additions) - _MAX_LISTED_ADDITIONS} autre(s)")

    return _result(
        rule,
        STATUS_TRIGGERED,
        current_excerpt=single_line_excerpt(
            " | ".join(excerpts), excerpt_limit
        ),
        detail="Nouvel élément absent de la référence : " + ", ".join(labels) + ".",
    )


def _closure_hits(
    snapshot: CanonicalSnapshot,
    anchor: str,
    excerpt_limit: int,
) -> list[tuple[str, str]]:
    """Return ``(fingerprint, excerpt)`` for blocks containing ``anchor``.

    The canonicalizer already extracted closure candidates for its configured
    anchor, so those are preferred.  When an operator configures a different
    anchor, the visible text blocks are used as a deterministic fallback.
    """

    folded = anchor.casefold()
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()

    for block in snapshot.closure_blocks:
        if folded not in block.excerpt.casefold():
            continue
        if block.fingerprint in seen:
            continue
        seen.add(block.fingerprint)
        hits.append(
            (
                block.fingerprint,
                single_line_excerpt(block.excerpt, excerpt_limit),
            )
        )
    if hits:
        return hits

    for line in snapshot.text.splitlines():
        normalized = line.strip()
        if not normalized or folded not in normalized.casefold():
            continue
        fingerprint = _fingerprint("closure", normalized)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        hits.append(
            (
                fingerprint,
                single_line_excerpt(normalized, excerpt_limit),
            )
        )
    return hits


def _timestamp_hits(
    snapshot: CanonicalSnapshot,
    label: str,
    window: int,
    excerpt_limit: int,
) -> list[tuple[str, str]]:
    """Return ``(fingerprint, excerpt)`` for timestamps next to ``label``."""

    folded = label.casefold()
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()

    for candidate in snapshot.timestamps:
        if candidate.label.casefold() != folded:
            continue
        if candidate.fingerprint in seen:
            continue
        seen.add(candidate.fingerprint)
        hits.append(
            (
                candidate.fingerprint,
                single_line_excerpt(candidate.excerpt, excerpt_limit),
            )
        )
    if hits:
        return hits

    # A rule may use a label or window different from the canonicalizer's
    # configuration.  Fall back to the canonical visible text so operator edits
    # remain effective.
    for line in snapshot.text.splitlines():
        lowered = line.casefold()
        start = lowered.find(folded)
        while start != -1:
            window_text = line[start + len(label) : start + len(label) + window]
            match = TIMESTAMP_PATTERN.search(window_text)
            if match is not None:
                value = _normalize_value(match.group(0))
                if value:
                    fingerprint = _fingerprint("timestamp", value)
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        hits.append(
                            (
                                fingerprint,
                                single_line_excerpt(line, excerpt_limit),
                            )
                        )
            start = lowered.find(folded, start + 1)
        if hits:
            # One displayed timestamp per label occurrence is enough; keep
            # scanning other lines only if none was found yet.
            break
    return hits


def _source_items(snapshot: CanonicalSnapshot, source: str) -> tuple[Any, ...]:
    if source == "link":
        return snapshot.links
    if source == "document":
        return snapshot.documents
    if source == "time":
        return snapshot.times
    return ()


def _result(
    rule: Rule,
    status: str,
    *,
    current_excerpt: str = "",
    reference_excerpt: str = "",
    detail: str = "",
) -> RuleResult:
    return RuleResult(
        rule_id=rule.id,
        rule_name=rule.name,
        kind=rule.kind,
        status=status,
        current_excerpt=current_excerpt,
        reference_excerpt=reference_excerpt,
        detail=detail,
    )


def _coerce_snapshot(
    value: CanonicalSnapshot | Mapping[str, Any] | str | bytes,
    name: str,
) -> CanonicalSnapshot:
    if isinstance(value, CanonicalSnapshot):
        return value
    try:
        if isinstance(value, Mapping):
            return CanonicalSnapshot.from_dict(value)
        if isinstance(value, (str, bytes)):
            return CanonicalSnapshot.from_json(value)
    except SnapshotFormatError as exc:
        raise EvaluationError(f"{name} snapshot is invalid: {exc}") from exc
    raise EvaluationError(
        f"{name} must be a CanonicalSnapshot, a mapping or snapshot JSON"
    )


def _coerce_rule(value: Rule | Mapping[str, Any]) -> Rule:
    if isinstance(value, Rule):
        return value
    if isinstance(value, Mapping):
        # Validation is deliberately deferred to ``_evaluate_rule`` so one
        # malformed persisted rule becomes ``not_evaluated`` instead of
        # aborting every other rule's result.
        return Rule(
            id=value.get("id", ""),
            name=value.get("name", ""),
            kind=value.get("kind", ""),
            enabled=value.get("enabled", True),
            parameters=value.get("parameters", {}),
        )
    raise EvaluationError("rule must be a Rule or a mapping")


def _normalize_value(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def _fingerprint(*parts: str) -> str:
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "Evaluation",
    "EvaluationError",
    "RULE_STATUSES",
    "RuleResult",
    "STATUS_DISABLED",
    "STATUS_NOT_EVALUATED",
    "STATUS_NOT_TRIGGERED",
    "STATUS_TRIGGERED",
    "evaluate",
    "evaluate_rule",
    "evaluate_rules",
]
