"""Compose a short French SMS that fits one GSM-7 segment.

The optional SMS channel must never silently drop the two required pieces of
information: the nature of the change and the link to the page. The
acknowledgement link is added only when the complete message still fits in a
single 160-septet message. If the two required pieces do not fit, composition
fails instead of splitting the message or truncating either of them.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

SINGLE_SMS_SEPTETS = 160

# GSM 03.38 default alphabet, excluding the escape character.
GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)

# Characters preceded by the escape character: they cost two septets.
GSM7_EXTENSION = frozenset("\f^{}\\[~]|€")

# Characters left untouched when normalising a URL. All belong to GSM 03.38,
# so a quoted URL remains countable in septets.
_URL_SAFE = "-._~:/?#[]@!$&'()*+,;=%"

_TRANSLITERATIONS = {
    "\u00a0": " ",
    "\u00ad": "",
    "\u2007": " ",
    "\u2009": " ",
    "\u200b": "",
    "\u202f": " ",
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u2032": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2033": '"',
    "\u00ab": '"',
    "\u00bb": '"',
    "\u2039": "'",
    "\u203a": "'",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u2026": "...",
    "\u2022": "-",
    "\u0153": "oe",
    "\u0152": "OE",
    "\u0110": "D",
    "\u0111": "d",
    "\u0141": "L",
    "\u0142": "l",
    "\u00d0": "D",
    "\u00f0": "d",
    "\u00de": "TH",
    "\u00fe": "th",
}


class SmsCompositionError(ValueError):
    """The message cannot be composed without losing required content."""


@dataclass(frozen=True)
class ComposedSms:
    """A composition result, always limited to one segment."""

    text: str
    septets: int
    includes_acknowledgement: bool

    @property
    def segment_count(self) -> int:
        return 1


def gsm7_septets(value: str) -> int:
    """Count the septets of an already normalised GSM-7 text."""

    return sum(2 if character in GSM7_EXTENSION else 1 for character in value)


def is_gsm7(value: str) -> bool:
    """Return whether every character belongs to the GSM 03.38 alphabet."""

    return all(
        character in GSM7_BASIC or character in GSM7_EXTENSION
        for character in value
    )


def normalize_gsm7(value: object) -> str:
    """Transliterate text to GSM 03.38.

    French accents missing from the default alphabet lose their diacritic and
    ``œ`` becomes ``oe``. Unknown characters become ``?`` so a message never
    silently switches to UCS-2 and overruns the single-segment limit.
    """

    raw = "" if value is None else str(value)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    normalized: list[str] = []
    for character in raw:
        if character in GSM7_BASIC or character in GSM7_EXTENSION:
            normalized.append(character)
            continue
        if character == "\t":
            normalized.append(" ")
            continue
        mapped = _TRANSLITERATIONS.get(character)
        if mapped is not None:
            normalized.append(mapped)
            continue

        decomposed = unicodedata.normalize("NFKD", character)
        replacement = "".join(
            part for part in decomposed if not unicodedata.combining(part)
        )
        if not replacement:
            if unicodedata.category(character) in {"Cc", "Cf"}:
                normalized.append(" ")
            else:
                normalized.append("?")
            continue

        for part in replacement:
            if part in GSM7_BASIC or part in GSM7_EXTENSION:
                normalized.append(part)
            elif part == "\t":
                normalized.append(" ")
            else:
                normalized.append("?")

    return "".join(normalized)


def compose_sms(
    nature: object,
    page_url: object,
    acknowledgement_url: object | None = None,
) -> ComposedSms:
    """Compose a single-segment SMS with the nature and the page link.

    The acknowledgement link is included only when the full message fits 160
    septets. If the required nature and page link do not fit together, an
    :class:`SmsCompositionError` is raised: the message is never split and
    required content is never silently dropped.
    """

    raw_nature = "" if nature is None else str(nature).strip()
    if not raw_nature:
        raise SmsCompositionError("The change nature is required")

    normalized_nature = normalize_gsm7(raw_nature).strip()
    if not normalized_nature:
        raise SmsCompositionError(
            "The change nature contains no valid SMS character"
        )

    page = _normalize_url(page_url, "page_url")
    acknowledgement = ""
    if acknowledgement_url is not None and str(acknowledgement_url).strip():
        acknowledgement = _normalize_url(
            acknowledgement_url, "acknowledgement_url"
        )

    base = f"{normalized_nature} - {page}"
    if acknowledgement:
        with_acknowledgement = (
            f"{base} - Acquittement : {acknowledgement}"
        )
        if gsm7_septets(with_acknowledgement) <= SINGLE_SMS_SEPTETS:
            return ComposedSms(
                text=with_acknowledgement,
                septets=gsm7_septets(with_acknowledgement),
                includes_acknowledgement=True,
            )

    septets = gsm7_septets(base)
    if septets <= SINGLE_SMS_SEPTETS:
        return ComposedSms(
            text=base,
            septets=septets,
            includes_acknowledgement=False,
        )

    raise SmsCompositionError(
        "The nature and page link do not fit in a single SMS"
    )


def _normalize_url(value: object, field_name: str) -> str:
    raw = "" if value is None else str(value).strip()
    if not raw:
        raise SmsCompositionError(f"{field_name} is required")

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise SmsCompositionError(
            f"{field_name} must be an absolute http or https URL"
        ) from exc

    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise SmsCompositionError(
            f"{field_name} must be an absolute http or https URL"
        )
    return quote(raw, safe=_URL_SAFE)
