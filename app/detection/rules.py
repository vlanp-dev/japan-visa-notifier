"""Rule definitions used to compare a page against a frozen reference.

Rules are declarative, independent and persisted with a stable identifier.
Three kinds are supported:

``closure_block``
    A closure/announcement block identified by an anchor string must still be
    present and unchanged.

``update_timestamp``
    The date/time displayed next to a label (for example ``mise à jour``) must
    still be present and unchanged.

``new_element``
    No link, document or ``<time>`` fingerprint may appear that was absent from
    the reference.

Defaults deliberately avoid lexical tests on generic words such as
``rendez-vous`` or ``calendrier``: the closed page already contains them, so a
rule based only on those words would fire immediately.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .canonical import (
    DEFAULT_CLOSURE_ANCHOR,
    DEFAULT_TIMESTAMP_LABEL,
    DEFAULT_TIMESTAMP_WINDOW,
)

RULE_KIND_CLOSURE_BLOCK = "closure_block"
RULE_KIND_UPDATE_TIMESTAMP = "update_timestamp"
RULE_KIND_NEW_ELEMENT = "new_element"

#: Every supported rule kind, in display order.
RULE_KINDS: tuple[str, ...] = (
    RULE_KIND_CLOSURE_BLOCK,
    RULE_KIND_UPDATE_TIMESTAMP,
    RULE_KIND_NEW_ELEMENT,
)

#: Element families the ``new_element`` rule can watch.
ELEMENT_SOURCES: tuple[str, ...] = ("document", "link", "time")

#: Default rules use stable ids so an operator edit never creates a clone.
RULE_ID_CLOSURE_BLOCK = "r1-closure-block"
RULE_ID_UPDATE_TIMESTAMP = "r2-update-timestamp"
RULE_ID_NEW_ELEMENT = "r3-new-element"

DEFAULT_RULE_IDS: tuple[str, ...] = (
    RULE_ID_CLOSURE_BLOCK,
    RULE_ID_UPDATE_TIMESTAMP,
    RULE_ID_NEW_ELEMENT,
)

#: Generic vocabulary that must never be the sole trigger of a default rule.
#: A parameter equal to one of these words alone is rejected as a naive
#: lexical rule; specific headings such as ``Complet 2026`` remain allowed.
GENERIC_LEXICAL_TOKENS: tuple[str, ...] = (
    "calendrier",
    "rendez-vous",
    "rendez vous",
)

MAX_RULE_ID_LENGTH = 64
MAX_RULE_NAME_LENGTH = 120
MAX_PARAMETER_TEXT_LENGTH = 160
MIN_PARAMETER_TEXT_LENGTH = 3
MAX_TIMESTAMP_WINDOW = 2000

_RULE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


class RuleValidationError(ValueError):
    """Raised when a rule or one of its parameters is invalid."""

    def __init__(self, errors: list[str] | str):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Rule:
    """One persisted detection rule.

    ``parameters`` is deliberately a plain mapping so it can be stored as JSON;
    :func:`normalize_rule` returns a validated copy with a normalized parameter
    mapping and should be used before persisting or evaluating a rule.
    """

    id: str
    name: str
    kind: str
    enabled: bool = True
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "parameters": copy.deepcopy(dict(self.parameters)),
        }

    def parameters_json(self) -> str:
        """Serialize parameters deterministically for the ``rules`` table."""

        return json.dumps(
            dict(self.parameters),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Rule":
        if not isinstance(data, Mapping):
            raise RuleValidationError("rule must be a JSON object")
        return normalize_rule(
            cls(
                id=data.get("id", ""),
                name=data.get("name", ""),
                kind=data.get("kind", ""),
                enabled=data.get("enabled", True),
                parameters=data.get("parameters", {}),
            )
        )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Rule":
        """Build a rule from a ``rules`` table row (or equivalent mapping)."""

        return normalize_rule(
            cls(
                id=row["id"],
                name=row["name"],
                kind=row["kind"],
                enabled=bool(row["enabled"]),
                parameters=decode_parameters(row["parameters"]),
            )
        )


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        id=RULE_ID_CLOSURE_BLOCK,
        name="Bloc d'annonce de fermeture",
        kind=RULE_KIND_CLOSURE_BLOCK,
        enabled=True,
        parameters={"anchor": DEFAULT_CLOSURE_ANCHOR},
    ),
    Rule(
        id=RULE_ID_UPDATE_TIMESTAMP,
        name="Horodatage de mise à jour",
        kind=RULE_KIND_UPDATE_TIMESTAMP,
        enabled=True,
        parameters={
            "label": DEFAULT_TIMESTAMP_LABEL,
            "window": DEFAULT_TIMESTAMP_WINDOW,
        },
    ),
    Rule(
        id=RULE_ID_NEW_ELEMENT,
        name="Nouvel élément absent de la référence",
        kind=RULE_KIND_NEW_ELEMENT,
        enabled=True,
        parameters={"sources": list(ELEMENT_SOURCES)},
    ),
)


def default_rules() -> tuple[Rule, ...]:
    """Return fresh copies of the three built-in rules.

    Copies are returned so a caller cannot mutate the module-level defaults or
    leak parameter changes between checks.
    """

    return tuple(
        Rule(
            id=rule.id,
            name=rule.name,
            kind=rule.kind,
            enabled=rule.enabled,
            parameters=copy.deepcopy(dict(rule.parameters)),
        )
        for rule in DEFAULT_RULES
    )


def normalize_rule(rule: Rule) -> Rule:
    """Validate a rule and return a normalized copy.

    Raises :class:`RuleValidationError` listing every problem found.
    """

    errors: list[str] = []

    identifier = _validated_identity(rule.id, errors)
    name = _validated_name(rule.name, errors)
    kind = _validated_kind(rule.kind, errors)
    enabled = rule.enabled
    if not isinstance(enabled, bool):
        errors.append("enabled must be a boolean")

    parameters: dict[str, Any] = {}
    if kind is not None:
        try:
            parameters = normalize_parameters(kind, rule.parameters)
        except RuleValidationError as exc:
            errors.extend(exc.errors)

    if errors:
        raise RuleValidationError(errors)

    return Rule(
        id=identifier,
        name=name,
        kind=kind,
        enabled=enabled,
        parameters=parameters,
    )


def validate_rule(rule: Rule) -> None:
    """Validate a rule without returning a normalized copy."""

    normalize_rule(rule)


def validate_parameters(kind: str, parameters: Mapping[str, Any]) -> None:
    """Validate and normalize parameters for ``kind``."""

    normalize_parameters(kind, parameters)


def normalize_parameters(
    kind: str, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    """Return validated, normalized parameters for a rule kind.

    Unknown keys are rejected: a typo in a parameter name must not silently
    disable part of a detection rule.
    """

    if kind not in RULE_KINDS:
        raise RuleValidationError(f"unsupported rule kind: {kind!r}")
    if not isinstance(parameters, Mapping):
        raise RuleValidationError("parameters must be a mapping")

    if kind == RULE_KIND_CLOSURE_BLOCK:
        return _normalize_closure_parameters(parameters)
    if kind == RULE_KIND_UPDATE_TIMESTAMP:
        return _normalize_timestamp_parameters(parameters)
    return _normalize_new_element_parameters(parameters)


def decode_parameters(raw: Any) -> dict[str, Any]:
    """Decode the ``parameters`` column value into a plain dictionary."""

    if isinstance(raw, Mapping):
        return dict(raw)
    if raw is None:
        return {}
    if not isinstance(raw, (str, bytes)):
        raise RuleValidationError("stored rule parameters must be JSON text")
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuleValidationError("stored rule parameters are not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuleValidationError("stored rule parameters must be a JSON object")
    return decoded


def _normalize_closure_parameters(
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    _reject_unknown_keys(parameters, {"anchor"}, errors)
    anchor = _text_parameter(
        parameters.get("anchor"),
        "anchor",
        errors,
        minimum=MIN_PARAMETER_TEXT_LENGTH,
    )
    if anchor is not None and _is_generic_lexical(anchor):
        errors.append(
            "anchor must not be a generic word such as 'rendez-vous' or "
            "'calendrier'"
        )
    if errors:
        raise RuleValidationError(errors)
    return {"anchor": anchor}


def _normalize_timestamp_parameters(
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    _reject_unknown_keys(parameters, {"label", "window"}, errors)
    label = _text_parameter(
        parameters.get("label"),
        "label",
        errors,
        minimum=MIN_PARAMETER_TEXT_LENGTH,
    )
    if label is not None and _is_generic_lexical(label):
        errors.append(
            "label must not be a generic word such as 'rendez-vous' or "
            "'calendrier'"
        )

    window = parameters.get("window", DEFAULT_TIMESTAMP_WINDOW)
    if isinstance(window, bool):
        errors.append("window must be an integer")
        window = DEFAULT_TIMESTAMP_WINDOW
    elif isinstance(window, str):
        try:
            window = int(window.strip(), 10)
        except ValueError:
            errors.append("window must be an integer")
            window = DEFAULT_TIMESTAMP_WINDOW
    elif not isinstance(window, int):
        errors.append("window must be an integer")
        window = DEFAULT_TIMESTAMP_WINDOW
    if not 1 <= window <= MAX_TIMESTAMP_WINDOW:
        errors.append(f"window must be between 1 and {MAX_TIMESTAMP_WINDOW}")

    if errors:
        raise RuleValidationError(errors)
    return {"label": label, "window": window}


def _normalize_new_element_parameters(
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    _reject_unknown_keys(parameters, {"sources"}, errors)

    raw_sources = parameters.get("sources", list(ELEMENT_SOURCES))
    sources: list[str] = []
    if isinstance(raw_sources, str):
        # Allow a comma-separated form as a convenience for form submissions.
        raw_sources = [part for part in raw_sources.split(",") if part.strip()]
    if not isinstance(raw_sources, (list, tuple)):
        errors.append("sources must be a list")
    else:
        seen: set[str] = set()
        for value in raw_sources:
            if not isinstance(value, str):
                errors.append("sources must contain strings")
                continue
            source = value.strip().lower()
            if source not in ELEMENT_SOURCES:
                errors.append(
                    f"unsupported element source: {value!r} "
                    f"(expected one of {', '.join(ELEMENT_SOURCES)})"
                )
                continue
            if source not in seen:
                seen.add(source)
                sources.append(source)
        if not sources and not errors:
            errors.append("sources must contain at least one element family")

    if errors:
        raise RuleValidationError(errors)
    return {"sources": sources}


def _validated_identity(value: Any, errors: list[str]) -> str:
    if not isinstance(value, str):
        errors.append("id must be a string")
        return ""
    identifier = value.strip()
    if not identifier:
        errors.append("id must not be empty")
    elif len(identifier) > MAX_RULE_ID_LENGTH:
        errors.append(f"id must be at most {MAX_RULE_ID_LENGTH} characters")
    elif not _RULE_ID_PATTERN.match(identifier):
        errors.append(
            "id must start with a letter or digit and contain only "
            "lowercase letters, digits, '.', '_' or '-'"
        )
    return identifier


def _validated_name(value: Any, errors: list[str]) -> str:
    if not isinstance(value, str):
        errors.append("name must be a string")
        return ""
    name = value.strip()
    if not name:
        errors.append("name must not be empty")
    elif len(name) > MAX_RULE_NAME_LENGTH:
        errors.append(f"name must be at most {MAX_RULE_NAME_LENGTH} characters")
    elif _CONTROL_PATTERN.search(name):
        errors.append("name must not contain control characters")
    return name


def _validated_kind(value: Any, errors: list[str]) -> str | None:
    if not isinstance(value, str):
        errors.append("kind must be a string")
        return None
    kind = value.strip()
    if kind not in RULE_KINDS:
        errors.append(
            f"unsupported rule kind: {value!r} "
            f"(expected one of {', '.join(RULE_KINDS)})"
        )
        return None
    return kind


def _text_parameter(
    value: Any,
    name: str,
    errors: list[str],
    *,
    minimum: int,
) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{name} must be a string")
        return None
    text = value.strip()
    if len(text) < minimum:
        errors.append(f"{name} must be at least {minimum} characters")
    elif len(text) > MAX_PARAMETER_TEXT_LENGTH:
        errors.append(
            f"{name} must be at most {MAX_PARAMETER_TEXT_LENGTH} characters"
        )
    elif _CONTROL_PATTERN.search(text):
        errors.append(f"{name} must not contain control characters")
    return text


def _reject_unknown_keys(
    parameters: Mapping[str, Any],
    allowed: set[str],
    errors: list[str],
) -> None:
    for key in parameters:
        if key not in allowed:
            errors.append(f"unknown parameter: {key!r}")


def _is_generic_lexical(value: str) -> bool:
    folded = re.sub(r"\s+", " ", value.casefold()).strip()
    return folded in GENERIC_LEXICAL_TOKENS


__all__ = [
    "DEFAULT_RULE_IDS",
    "DEFAULT_RULES",
    "ELEMENT_SOURCES",
    "GENERIC_LEXICAL_TOKENS",
    "MAX_TIMESTAMP_WINDOW",
    "RULE_ID_CLOSURE_BLOCK",
    "RULE_ID_NEW_ELEMENT",
    "RULE_ID_UPDATE_TIMESTAMP",
    "RULE_KIND_CLOSURE_BLOCK",
    "RULE_KIND_NEW_ELEMENT",
    "RULE_KIND_UPDATE_TIMESTAMP",
    "RULE_KINDS",
    "Rule",
    "RuleValidationError",
    "decode_parameters",
    "default_rules",
    "normalize_parameters",
    "normalize_rule",
    "validate_parameters",
    "validate_rule",
]
