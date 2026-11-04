"""Detection layer package.

Canonical content extraction lives here.  Later work items add fetching, rules,
the check loop and operator interfaces below this package.
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

__all__ = [
    "DEFAULT_CLOSURE_ANCHOR",
    "DEFAULT_TIMESTAMP_LABEL",
    "CanonicalSnapshot",
    "CanonicalizationError",
    "ContentDecodingError",
    "SnapshotFormatError",
    "canonicalize_page",
    "decode_html",
    "single_line_excerpt",
]
