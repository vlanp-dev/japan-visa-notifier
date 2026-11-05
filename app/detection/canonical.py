"""Deterministic canonicalization of retrieved page content.

The page is untrusted data.  This module only turns it into a stable,
comparable snapshot: it never executes the content, never interprets it as an
instruction and never follows a URL found in the page.  HTML is parsed with the
standard :mod:`html.parser` only.

A snapshot contains:

* ``text``: normalized visible text, one block per line;
* ``blocks``: a parameter-independent block tree (tag, ordered text/child
  parts, parent index) from which closure announcements and timestamps can be
  re-extracted with the *current* rule parameters;
* ``closure_blocks``: the closure announcement regions found with the default
  anchor, kept as a convenience view;
* ``timestamps``: displayed date/time values found next to the default label,
  kept as a convenience view;
* ``links``: stable fingerprints of absolute HTTP(S) links;
* ``documents``: stable fingerprints of document links (PDF, office files);
* ``times``: stable fingerprints of ``<time>`` elements.

Collections preserve document order.  Serialization is deterministic, so two
identical pages produce byte-identical JSON.  Evaluation re-extracts rules from
``blocks`` so editing a rule never requires refreezing the reference.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

SNAPSHOT_VERSION = 2

DEFAULT_CLOSURE_ANCHOR = "Complet 2026"
DEFAULT_TIMESTAMP_LABEL = "mise à jour"
DEFAULT_TIMESTAMP_WINDOW = 160
DEFAULT_EXCERPT_LIMIT = 160

#: A closure announcement is the smallest enclosing region around the anchor
#: whose visible text stays within this bound.  The page structure itself is
#: stored, so this bound is independent of any rule parameter.
MAX_CLOSURE_REGION_CHARS = 400

#: Structural roots are never used as an announcement region: they contain the
#: whole page and would make every change look like an announcement change.
ROOT_BLOCK_TAGS = frozenset({"html", "body"})

#: Tags whose subtree is never visible text.
SKIP_ELEMENTS = frozenset({"head", "noscript", "script", "style", "template"})

VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Elements that delimit a text block.  ``html`` and ``body`` are included so a
#: page whose anchor only appears in inline content still yields a candidate.
BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "caption",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "html",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_MONTHS = (
    "janvier|février|fevrier|mars|avril|mai|juin|juillet|"
    "août|aout|septembre|octobre|novembre|décembre|decembre"
)
_DATE_WORD = rf"\d{{1,2}}(?:er)?\s+(?:{_MONTHS})\s+\d{{4}}"
_DATE_NUMERIC = r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
_DATE_ISO = r"\d{4}-\d{2}-\d{2}"
_TIME = r"\d{1,2}\s*[h:]\s*\d{2}(?:\s*(?:min|minutes?))?"

#: A displayed French date with an optional time.  The pattern deliberately
#: recognises several common notations instead of a single site-specific one.
TIMESTAMP_PATTERN = re.compile(
    rf"(?P<date>{_DATE_ISO}|{_DATE_WORD}|{_DATE_NUMERIC})"
    rf"(?:\s*(?:[àa]|,|-)?\s*(?P<time>{_TIME}))?",
    re.IGNORECASE,
)

_DOCUMENT_SUFFIXES = (
    ".csv",
    ".doc",
    ".docx",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".xls",
    ".xlsx",
)
_DOCUMENT_TEXT_PATTERN = re.compile(
    r"\b(?:csv|docx?|ods|odt|pdf|pptx?|rtf|xlsx?)\b",
    re.IGNORECASE,
)

# Characters that are invisible but still present in parsed text.
_ZERO_WIDTH = frozenset({"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"})
_NON_BREAKING_SPACES = frozenset({"\u00a0", "\u2007", "\u202f"})

_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


class CanonicalizationError(ValueError):
    """Base class for content that cannot be canonicalized safely."""


class ContentDecodingError(CanonicalizationError):
    """The response body could not be decoded with the declared charset."""


class SnapshotFormatError(CanonicalizationError):
    """A persisted snapshot does not match the canonical schema."""


@dataclass(frozen=True)
class ClosureCandidate:
    """Nearest block containing the closure anchor."""

    tag: str
    fingerprint: str
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "fingerprint": self.fingerprint,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClosureCandidate":
        _require_mapping(data, "closure block")
        return cls(
            tag=_field_str(data, "tag", "closure block"),
            fingerprint=_field_str(data, "fingerprint", "closure block"),
            excerpt=_field_str(data, "excerpt", "closure block"),
        )


@dataclass(frozen=True)
class TimestampCandidate:
    """Displayed timestamp found near the configured label."""

    label: str
    value: str
    fingerprint: str
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "fingerprint": self.fingerprint,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TimestampCandidate":
        _require_mapping(data, "timestamp")
        return cls(
            label=_field_str(data, "label", "timestamp"),
            value=_field_str(data, "value", "timestamp"),
            fingerprint=_field_str(data, "fingerprint", "timestamp"),
            excerpt=_field_str(data, "excerpt", "timestamp"),
        )


@dataclass(frozen=True)
class LinkFingerprint:
    """Stable identity of an absolute HTTP(S) link."""

    fingerprint: str
    url: str
    text: str
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "url": self.url,
            "text": self.text,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LinkFingerprint":
        _require_mapping(data, "link")
        return cls(
            fingerprint=_field_str(data, "fingerprint", "link"),
            url=_field_str(data, "url", "link"),
            text=_field_str(data, "text", "link"),
            excerpt=_field_str(data, "excerpt", "link"),
        )


@dataclass(frozen=True)
class DocumentFingerprint:
    """Stable identity of a document link, including its displayed date."""

    fingerprint: str
    url: str
    date: str
    text: str
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "url": self.url,
            "date": self.date,
            "text": self.text,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DocumentFingerprint":
        _require_mapping(data, "document")
        return cls(
            fingerprint=_field_str(data, "fingerprint", "document"),
            url=_field_str(data, "url", "document"),
            date=_field_str(data, "date", "document"),
            text=_field_str(data, "text", "document"),
            excerpt=_field_str(data, "excerpt", "document"),
        )


@dataclass(frozen=True)
class TimeFingerprint:
    """Stable identity of a ``<time>`` element."""

    fingerprint: str
    value: str
    text: str
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "value": self.value,
            "text": self.text,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TimeFingerprint":
        _require_mapping(data, "time")
        return cls(
            fingerprint=_field_str(data, "fingerprint", "time"),
            value=_field_str(data, "value", "time"),
            text=_field_str(data, "text", "time"),
            excerpt=_field_str(data, "excerpt", "time"),
        )


@dataclass(frozen=True)
class Block:
    """One text block, stored independently of any rule parameter.

    ``parts`` preserves document order.  A string is an inline text run and an
    integer is the index of a nested block; joining them reconstructs the
    block's visible text.  ``parent`` is the index of the nearest enclosing
    block, or ``-1``.  Because blocks are allocated in pre-order, a child index
    is always greater than its parent's.
    """

    tag: str
    parts: tuple[str | int, ...]
    parent: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "parts": list(self.parts),
            "parent": self.parent,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Block":
        _require_mapping(data, "block")
        tag = _field_str(data, "tag", "block")
        parent = data.get("parent")
        if isinstance(parent, bool) or not isinstance(parent, int):
            raise SnapshotFormatError("block.parent must be an integer")
        raw_parts = data.get("parts")
        if not isinstance(raw_parts, list):
            raise SnapshotFormatError("block.parts must be a list")
        parts: list[str | int] = []
        for part in raw_parts:
            if isinstance(part, str):
                parts.append(part)
            elif (
                isinstance(part, bool)
                or not isinstance(part, int)
                or part < 0
            ):
                raise SnapshotFormatError(
                    "block.parts entries must be text or block indices"
                )
            else:
                parts.append(part)
        return cls(tag=tag, parts=tuple(parts), parent=parent)


@dataclass(frozen=True)
class CanonicalSnapshot:
    """Deterministic, JSON-serializable view of one page retrieval."""

    text: str
    blocks: tuple[Block, ...] = ()
    closure_blocks: tuple[ClosureCandidate, ...] = ()
    timestamps: tuple[TimestampCandidate, ...] = ()
    links: tuple[LinkFingerprint, ...] = ()
    documents: tuple[DocumentFingerprint, ...] = ()
    times: tuple[TimeFingerprint, ...] = ()
    version: int = SNAPSHOT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "text": self.text,
            "blocks": [item.to_dict() for item in self.blocks],
            "closure_blocks": [item.to_dict() for item in self.closure_blocks],
            "timestamps": [item.to_dict() for item in self.timestamps],
            "links": [item.to_dict() for item in self.links],
            "documents": [item.to_dict() for item in self.documents],
            "times": [item.to_dict() for item in self.times],
        }

    def extract_closure_candidates(
        self,
        anchor: str,
        excerpt_limit: int = DEFAULT_EXCERPT_LIMIT,
    ) -> tuple[ClosureCandidate, ...]:
        """Re-extract announcements for ``anchor`` from the stored blocks."""

        return closure_candidates_for(
            self.blocks, anchor, excerpt_limit=excerpt_limit
        )

    def extract_timestamp_candidates(
        self,
        label: str,
        window: int,
        excerpt_limit: int = DEFAULT_EXCERPT_LIMIT,
    ) -> tuple[TimestampCandidate, ...]:
        """Re-extract timestamps for ``label``/``window`` from stored blocks."""

        return timestamp_candidates_for(
            self.blocks,
            label,
            window,
            excerpt_limit=excerpt_limit,
        )

    def to_json(self) -> str:
        """Serialize deterministically so identical pages compare byte-equal."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        """SHA-256 of the canonical JSON, used as a content hash."""

        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CanonicalSnapshot":
        _require_mapping(data, "snapshot")
        version = data.get("version", SNAPSHOT_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise SnapshotFormatError("snapshot version must be an integer")
        if version != SNAPSHOT_VERSION:
            raise SnapshotFormatError(f"unsupported snapshot version: {version}")

        text = _field_str(data, "text", "snapshot")
        blocks = _candidates(data, "blocks", Block.from_dict)
        _validate_block_links(blocks)
        return cls(
            text=text,
            blocks=blocks,
            closure_blocks=_candidates(
                data, "closure_blocks", ClosureCandidate.from_dict
            ),
            timestamps=_candidates(
                data, "timestamps", TimestampCandidate.from_dict
            ),
            links=_candidates(data, "links", LinkFingerprint.from_dict),
            documents=_candidates(
                data, "documents", DocumentFingerprint.from_dict
            ),
            times=_candidates(data, "times", TimeFingerprint.from_dict),
            version=version,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "CanonicalSnapshot":
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SnapshotFormatError(
                    "snapshot JSON is not valid UTF-8"
                ) from exc
        if not isinstance(raw, str):
            raise SnapshotFormatError("snapshot JSON must be text")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SnapshotFormatError("snapshot JSON is malformed") from exc
        return cls.from_dict(data)


class _Element:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(
        self,
        tag: str,
        attrs: Mapping[str, str],
        parent: "_Element | None" = None,
    ):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children: list[str | _Element] = []
        self.parent = parent


class _PageParser(HTMLParser):
    """Build a lightweight tree while ignoring comments and declarations."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("#document", {})
        self._stack: list[_Element] = [self.root]

    def _append(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        push: bool,
    ) -> None:
        cleaned: dict[str, str] = {}
        for name, value in attrs:
            key = (name or "").strip().lower()
            if not key:
                continue
            cleaned[key] = "" if value is None else str(value)
        element = _Element(tag.lower(), cleaned, self._stack[-1])
        self._stack[-1].children.append(element)
        if push and element.tag not in VOID_ELEMENTS:
            self._stack.append(element)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._append(tag, attrs, push=True)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._append(tag, attrs, push=False)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_ELEMENTS:
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return
        # Unmatched end tags are ignored: HTML is routinely malformed.

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)

    def handle_comment(self, data: str) -> None:
        return None

    def handle_decl(self, decl: str) -> None:
        return None

    def handle_pi(self, data: str) -> None:
        return None

    def unknown_decl(self, data: str) -> None:
        return None


def decode_html(data: bytes | str, charset: str | None = None) -> str:
    """Decode an HTML body safely.

    A byte-order mark wins over the declared charset.  Otherwise the declared
    charset is used, falling back to strict UTF-8.  Undecodable bytes raise
    :class:`ContentDecodingError` so the caller can record a control failure
    rather than guessing at the content.
    """

    if isinstance(data, str):
        return data.lstrip("\ufeff")
    if not isinstance(data, (bytes, bytearray)):
        raise ContentDecodingError("page content must be text or bytes")

    raw = bytes(data)
    encoding = None
    for bom, bom_encoding in _BOM_ENCODINGS:
        if raw.startswith(bom):
            encoding = bom_encoding
            break

    if encoding is None:
        if charset is not None:
            if not isinstance(charset, str) or not charset.strip():
                raise ContentDecodingError("declared charset is invalid")
            encoding = charset.strip()
            try:
                codecs.lookup(encoding)
            except LookupError as exc:
                raise ContentDecodingError(
                    f"unknown declared charset: {encoding}"
                ) from exc
        else:
            encoding = "utf-8"

    try:
        return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise ContentDecodingError(
            f"page content cannot be decoded as {encoding}"
        ) from exc


def canonicalize_page(
    content: bytes | str,
    *,
    charset: str | None = None,
    base_url: str | None = None,
    closure_anchor: str = DEFAULT_CLOSURE_ANCHOR,
    timestamp_label: str = DEFAULT_TIMESTAMP_LABEL,
    timestamp_window: int = DEFAULT_TIMESTAMP_WINDOW,
    excerpt_limit: int = DEFAULT_EXCERPT_LIMIT,
) -> CanonicalSnapshot:
    """Turn one page retrieval into a deterministic canonical snapshot.

    ``base_url`` is only used to resolve relative links; it never causes any
    request.  ``closure_anchor`` and ``timestamp_label`` are the configurable
    detection parameters.  ``timestamp_window`` bounds how many characters
    after the label are searched for a date.
    """

    if isinstance(timestamp_window, bool) or not isinstance(timestamp_window, int):
        raise CanonicalizationError("timestamp_window must be an integer")
    if timestamp_window < 1:
        raise CanonicalizationError("timestamp_window must be at least 1")
    if isinstance(excerpt_limit, bool) or not isinstance(excerpt_limit, int):
        raise CanonicalizationError("excerpt_limit must be an integer")
    if excerpt_limit < 1:
        raise CanonicalizationError("excerpt_limit must be at least 1")
    if not isinstance(closure_anchor, str):
        raise CanonicalizationError("closure_anchor must be a string")
    if not isinstance(timestamp_label, str):
        raise CanonicalizationError("timestamp_label must be a string")
    if base_url is not None and not isinstance(base_url, str):
        raise CanonicalizationError("base_url must be a string")

    text = decode_html(content, charset).lstrip("\ufeff")
    parser = _PageParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # html.parser is lenient, but never trust input
        raise CanonicalizationError("page HTML could not be parsed") from exc

    render_cache: dict[int, str] = {}
    root = parser.root
    visible_text = _canonical_text(_render_cached(root, render_cache))

    blocks = _build_blocks(root)

    # A 200 response that parses to nothing usable is a control failure, not a
    # content change: there is no meaningful state to compare.  A page with
    # only bare text and no block structure is treated the same way.
    if not visible_text or not blocks:
        raise CanonicalizationError("page has no usable visible structure")

    block_texts = reconstruct_block_texts(blocks)

    anchor = _normalize_inline(closure_anchor)
    closure_blocks = closure_candidates_for(
        blocks,
        anchor,
        excerpt_limit=excerpt_limit,
        texts=block_texts,
    )
    timestamps = timestamp_candidates_for(
        blocks,
        _normalize_inline(timestamp_label),
        timestamp_window,
        excerpt_limit=excerpt_limit,
        texts=block_texts,
    )
    links, documents = _link_candidates(
        root,
        base_url,
        render_cache,
        excerpt_limit,
    )
    times = _time_candidates(root, render_cache, excerpt_limit)

    return CanonicalSnapshot(
        text=visible_text,
        blocks=blocks,
        closure_blocks=closure_blocks,
        timestamps=timestamps,
        links=links,
        documents=documents,
        times=times,
    )


def single_line_excerpt(
    value: str,
    limit: int = DEFAULT_EXCERPT_LIMIT,
) -> str:
    """Return a bounded, single-line, whitespace-collapsed excerpt."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if value is None:
        value = ""
    cleaned = _normalize_inline(value if isinstance(value, str) else str(value))
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _build_blocks(root: _Element) -> tuple[Block, ...]:
    """Build a parameter-independent block tree from the parsed document.

    Each block stores its inline text runs and nested block indices in document
    order.  Text belongs to exactly one block, so the snapshot stays bounded by
    the page size instead of duplicating every ancestor's rendered text.
    """

    blocks: list[Block] = []

    def walk(node: _Element, parent_index: int) -> list[str | int]:
        parts: list[str | int] = []
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            text = _normalize_inline("".join(buffer))
            buffer.clear()
            if text:
                parts.append(text)

        for child in node.children:
            if isinstance(child, str):
                buffer.append(
                    _normalize_characters(child, keep_newlines=False)
                )
                continue
            if child.tag in SKIP_ELEMENTS:
                continue
            if child.tag in BLOCK_ELEMENTS:
                flush()
                index = len(blocks)
                # Reserve the slot before descending so children always have a
                # greater index than their parent.
                blocks.append(Block(tag=child.tag, parts=(), parent=parent_index))
                child_parts = walk(child, index)
                blocks[index] = Block(
                    tag=child.tag,
                    parts=tuple(child_parts),
                    parent=parent_index,
                )
                parts.append(index)
                continue
            if child.tag == "br":
                buffer.append(" ")
                continue
            # Inline element: splice its content into this block in order.
            for part in walk(child, parent_index):
                if isinstance(part, int):
                    flush()
                    parts.append(part)
                else:
                    buffer.append(part)
        flush()
        return parts

    walk(root, -1)
    return tuple(blocks)


def reconstruct_block_texts(
    blocks: tuple[Block, ...],
) -> tuple[str, ...]:
    """Return the visible text of every block, children included."""

    texts = [""] * len(blocks)
    # Children always have a greater index than their parent, so one reverse
    # pass is enough and no recursion is needed.
    for index in range(len(blocks) - 1, -1, -1):
        pieces: list[str] = []
        for part in blocks[index].parts:
            if isinstance(part, int):
                if 0 <= part < len(texts):
                    pieces.append(texts[part])
            else:
                pieces.append(part)
        texts[index] = _normalize_inline(" ".join(pieces))
    return tuple(texts)


def closure_candidates_for(
    blocks: tuple[Block, ...],
    anchor: str,
    *,
    excerpt_limit: int = DEFAULT_EXCERPT_LIMIT,
    texts: tuple[str, ...] | None = None,
) -> tuple[ClosureCandidate, ...]:
    """Find announcement regions for ``anchor`` in a stored block tree.

    The region is the largest ancestor of an anchor-bearing block whose text
    stays within :data:`MAX_CLOSURE_REGION_CHARS`; this includes a meaningful
    sentence that sits in a sibling paragraph of the announcement container.
    """

    if not anchor or not blocks:
        return ()
    block_texts = texts if texts is not None else reconstruct_block_texts(blocks)
    matching = _matching_block_indices(block_texts, anchor)
    anchor_pattern = re.compile(re.escape(anchor), re.IGNORECASE)
    seen: set[str] = set()
    candidates: list[ClosureCandidate] = []
    for index in _innermost_indices(blocks, matching):
        region = _closure_region_index(blocks, block_texts, matching, index)
        text = block_texts[region]
        fingerprint = _fingerprint("closure", text)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        match = anchor_pattern.search(text)
        start = match.start() if match else 0
        # Start just before the anchor and keep the following sentence(s): the
        # announcement meaning usually sits after the closing headline.
        excerpt = single_line_excerpt(
            text[max(0, start - 40) : max(0, start - 40) + excerpt_limit],
            excerpt_limit,
        )
        candidates.append(
            ClosureCandidate(
                tag=blocks[region].tag,
                fingerprint=fingerprint,
                excerpt=excerpt,
            )
        )
    return tuple(candidates)


def timestamp_candidates_for(
    blocks: tuple[Block, ...],
    label: str,
    window: int,
    *,
    excerpt_limit: int = DEFAULT_EXCERPT_LIMIT,
    texts: tuple[str, ...] | None = None,
) -> tuple[TimestampCandidate, ...]:
    """Find timestamps next to ``label`` in a stored block tree.

    Both the reference and the current snapshot are re-extracted with the
    rule's current label and window, so editing them takes effect on the next
    check without refreezing the reference.
    """

    if not label or not blocks:
        return ()
    block_texts = texts if texts is not None else reconstruct_block_texts(blocks)
    matching = _matching_block_indices(block_texts, label)
    label_pattern = re.compile(re.escape(label), re.IGNORECASE)
    seen: set[str] = set()
    candidates: list[TimestampCandidate] = []
    for index in _innermost_indices(blocks, matching):
        text = block_texts[index]
        for match in label_pattern.finditer(text):
            start = match.end()
            window_text = text[start : start + window]
            timestamp = TIMESTAMP_PATTERN.search(window_text)
            if timestamp is None:
                continue
            value = _normalize_inline(timestamp.group(0))
            if not value:
                continue
            fingerprint = _fingerprint("timestamp", value)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            context_start = max(0, match.start() - 30)
            context_end = min(len(text), start + timestamp.end() + 30)
            candidates.append(
                TimestampCandidate(
                    label=label,
                    value=value,
                    fingerprint=fingerprint,
                    excerpt=single_line_excerpt(
                        text[context_start:context_end], excerpt_limit
                    ),
                )
            )
    return tuple(candidates)


def _matching_block_indices(
    texts: tuple[str, ...],
    needle: str,
) -> set[int]:
    folded = needle.casefold()
    return {
        index
        for index, text in enumerate(texts)
        if folded in text.casefold()
    }


def _innermost_indices(
    blocks: tuple[Block, ...],
    matching: set[int],
) -> list[int]:
    innermost = set(matching)
    for index in matching:
        parent = blocks[index].parent
        while parent != -1:
            innermost.discard(parent)
            parent = blocks[parent].parent
    return [index for index in range(len(blocks)) if index in innermost]


def _closure_region_index(
    blocks: tuple[Block, ...],
    texts: tuple[str, ...],
    matching: set[int],
    index: int,
) -> int:
    current = index
    while True:
        parent = blocks[current].parent
        if parent == -1 or parent not in matching:
            return current
        if blocks[parent].tag in ROOT_BLOCK_TAGS:
            return current
        if len(texts[parent]) > MAX_CLOSURE_REGION_CHARS:
            return current
        current = parent


def _link_candidates(
    root: _Element,
    base_url: str | None,
    render_cache: dict[int, str],
    excerpt_limit: int,
) -> tuple[tuple[LinkFingerprint, ...], tuple[DocumentFingerprint, ...]]:
    links: list[LinkFingerprint] = []
    documents: list[DocumentFingerprint] = []
    seen_links: set[str] = set()
    seen_documents: set[str] = set()

    for element in _iter_elements(root):
        if element.tag != "a":
            continue
        href = element.attrs.get("href")
        if not href:
            continue
        url = _canonical_http_url(href, base_url)
        if url is None:
            continue
        text = _normalize_inline(_render_cached(element, render_cache))
        link_fingerprint = _fingerprint("link", url)
        if link_fingerprint not in seen_links:
            seen_links.add(link_fingerprint)
            links.append(
                LinkFingerprint(
                    fingerprint=link_fingerprint,
                    url=url,
                    text=text,
                    excerpt=single_line_excerpt(
                        _link_excerpt_text(text, url), excerpt_limit
                    ),
                )
            )
        if not _looks_like_document(url, text):
            continue
        date = _document_date(url, text)
        document_fingerprint = _fingerprint("document", url, date)
        if document_fingerprint in seen_documents:
            continue
        seen_documents.add(document_fingerprint)
        documents.append(
            DocumentFingerprint(
                fingerprint=document_fingerprint,
                url=url,
                date=date,
                text=text,
                excerpt=single_line_excerpt(
                    _link_excerpt_text(text, url), excerpt_limit
                ),
            )
        )
    return tuple(links), tuple(documents)


def _time_candidates(
    root: _Element,
    render_cache: dict[int, str],
    excerpt_limit: int,
) -> tuple[TimeFingerprint, ...]:
    seen: set[str] = set()
    candidates: list[TimeFingerprint] = []
    for element in _iter_elements(root):
        if element.tag != "time":
            continue
        text = _normalize_inline(_render_cached(element, render_cache))
        value = _normalize_inline(element.attrs.get("datetime", "")) or text
        if not value:
            continue
        fingerprint = _fingerprint("time", value)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append(
            TimeFingerprint(
                fingerprint=fingerprint,
                value=value,
                text=text,
                excerpt=single_line_excerpt(
                    text or value, excerpt_limit
                ),
            )
        )
    return tuple(candidates)


def _iter_elements(element: _Element):
    for child in element.children:
        if not isinstance(child, _Element):
            continue
        if child.tag in SKIP_ELEMENTS:
            continue
        yield child
        yield from _iter_elements(child)


def _render_cached(node: str | _Element, cache: dict[int, str]) -> str:
    if isinstance(node, str):
        # Source formatting newlines are mere whitespace.  Only block
        # boundaries below introduce a real line separator.
        return _normalize_characters(node, keep_newlines=False)
    key = id(node)
    cached = cache.get(key)
    if cached is not None:
        return cached
    if node.tag in SKIP_ELEMENTS:
        result = ""
    elif node.tag == "br":
        result = "\n"
    else:
        inner = "".join(
            _render_cached(child, cache) for child in node.children
        )
        if node.tag in BLOCK_ELEMENTS:
            result = f"\n{inner}\n"
        else:
            result = inner
    cache[key] = result
    return result


def _canonical_text(raw: str) -> str:
    cleaned = _normalize_characters(raw, keep_newlines=True)
    lines = (_normalize_inline(line) for line in cleaned.split("\n"))
    return "\n".join(line for line in lines if line)


def _normalize_inline(raw: str) -> str:
    cleaned = _normalize_characters(raw, keep_newlines=False)
    return re.sub(r" +", " ", cleaned).strip()


def _normalize_characters(raw: str, *, keep_newlines: bool) -> str:
    result: list[str] = []
    for character in unicodedata.normalize("NFC", raw):
        if character in _NON_BREAKING_SPACES:
            result.append(" ")
        elif character in _ZERO_WIDTH:
            continue
        elif character == "\n" and keep_newlines:
            result.append("\n")
        elif character.isspace():
            result.append(" ")
        elif unicodedata.category(character).startswith("C"):
            continue
        else:
            result.append(character)
    return "".join(result)


def _canonical_http_url(raw: str, base_url: str | None) -> str | None:
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        if base_url:
            candidate = urljoin(base_url, candidate)
        parts = urlsplit(candidate)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not host:
        return None

    host = host.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    return urlunsplit(
        (scheme, netloc, parts.path or "/", parts.query, "")
    )


def _looks_like_document(url: str, text: str) -> bool:
    try:
        path = urlsplit(url).path.casefold()
    except ValueError:
        path = ""
    if path.endswith(_DOCUMENT_SUFFIXES):
        return True
    return _DOCUMENT_TEXT_PATTERN.search(text) is not None


def _document_date(url: str, text: str) -> str:
    for source in (text, unquote(url)):
        match = TIMESTAMP_PATTERN.search(source)
        if match is not None:
            return _normalize_inline(match.group(0))
    return ""


def _link_excerpt_text(text: str, url: str) -> str:
    return f"{text} — {url}" if text else url


def _fingerprint(*parts: str) -> str:
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _require_mapping(data: Any, name: str) -> None:
    if not isinstance(data, Mapping):
        raise SnapshotFormatError(f"{name} must be a JSON object")


def _field_str(data: Mapping[str, Any], name: str, owner: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise SnapshotFormatError(f"{owner}.{name} must be a string")
    return value


def _candidates(
    data: Mapping[str, Any],
    name: str,
    factory,
) -> tuple[Any, ...]:
    values = data.get(name, [])
    if not isinstance(values, list):
        raise SnapshotFormatError(f"snapshot.{name} must be a list")
    return tuple(factory(item) for item in values)


def _validate_block_links(blocks: tuple[Block, ...]) -> None:
    """Reject persisted block references that could not come from a page.

    Children must point forward (pre-order allocation) and parents must be
    earlier blocks, which rules out cycles and out-of-range lookups.
    """

    total = len(blocks)
    for index, block in enumerate(blocks):
        if block.parent != -1 and not 0 <= block.parent < index:
            raise SnapshotFormatError("block.parent is out of range")
        for part in block.parts:
            if isinstance(part, int) and not index < part < total:
                raise SnapshotFormatError("block.parts index is out of range")
