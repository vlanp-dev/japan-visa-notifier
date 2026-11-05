"""Detection layer package.

Canonical content extraction, disciplined page retrieval and rule evaluation
live here.  Later work items add the check loop and operator interfaces below
this package.
"""

from .canonical import (
    DEFAULT_CLOSURE_ANCHOR,
    DEFAULT_TIMESTAMP_LABEL,
    CanonicalSnapshot,
    CanonicalizationError,
    ContentDecodingError,
    SnapshotFormatError,
    canonicalize_page,
    decode_html,
    single_line_excerpt,
)
from .engine import (
    STATUS_DISABLED,
    STATUS_NOT_EVALUATED,
    STATUS_NOT_TRIGGERED,
    STATUS_TRIGGERED,
    Evaluation,
    EvaluationError,
    RuleResult,
    evaluate,
    evaluate_rule,
    evaluate_rules,
)
from .fetch import (
    MAX_REDIRECTS,
    FailureCategory,
    FetchFailure,
    Fetcher,
    FetchResult,
    fetch_page,
)
from .rules import (
    DEFAULT_RULE_IDS,
    DEFAULT_RULES,
    ELEMENT_SOURCES,
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

__all__ = [
    "DEFAULT_CLOSURE_ANCHOR",
    "DEFAULT_RULE_IDS",
    "DEFAULT_RULES",
    "DEFAULT_TIMESTAMP_LABEL",
    "ELEMENT_SOURCES",
    "MAX_REDIRECTS",
    "RULE_KINDS",
    "STATUS_DISABLED",
    "STATUS_NOT_EVALUATED",
    "STATUS_NOT_TRIGGERED",
    "STATUS_TRIGGERED",
    "CanonicalSnapshot",
    "CanonicalizationError",
    "ContentDecodingError",
    "Evaluation",
    "EvaluationError",
    "FailureCategory",
    "FetchFailure",
    "Fetcher",
    "FetchResult",
    "Rule",
    "RuleResult",
    "RuleValidationError",
    "SnapshotFormatError",
    "canonicalize_page",
    "decode_html",
    "decode_parameters",
    "default_rules",
    "evaluate",
    "evaluate_rule",
    "evaluate_rules",
    "fetch_page",
    "normalize_parameters",
    "normalize_rule",
    "single_line_excerpt",
    "validate_parameters",
    "validate_rule",
]
