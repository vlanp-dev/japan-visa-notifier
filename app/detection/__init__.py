"""Detection layer package.

Canonical content extraction and disciplined page retrieval live here.  Later
work items add rules, the check loop and operator interfaces below this
package.
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
from .fetch import (
    MAX_REDIRECTS,
    FailureCategory,
    FetchFailure,
    Fetcher,
    FetchResult,
    fetch_page,
)

__all__ = [
    "DEFAULT_CLOSURE_ANCHOR",
    "DEFAULT_TIMESTAMP_LABEL",
    "MAX_REDIRECTS",
    "CanonicalSnapshot",
    "CanonicalizationError",
    "ContentDecodingError",
    "FailureCategory",
    "FetchFailure",
    "Fetcher",
    "FetchResult",
    "SnapshotFormatError",
    "canonicalize_page",
    "decode_html",
    "fetch_page",
    "single_line_excerpt",
]
