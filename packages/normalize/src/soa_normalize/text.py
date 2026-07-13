"""Text-level normalizers: whitespace, identifiers, booleans, enums."""

import unicodedata
from collections.abc import Sequence

from soa_normalize.context import DEFAULT_CONTEXT, NormalizationContext, NormalizationError

#: Zero-width and BOM characters that OCR and copy/paste smuggle in.
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def normalize_whitespace(raw: str, context: NormalizationContext = DEFAULT_CONTEXT) -> str:
    """NFC-normalize, drop zero-width characters, collapse every
    whitespace run (including NBSP and friends) to a single space, and
    strip the ends. A whitespace-only input becomes "" — honestly empty."""
    cleaned = unicodedata.normalize("NFC", raw).translate(_ZERO_WIDTH)
    return " ".join(cleaned.split())


def normalize_identifier(raw: str, context: NormalizationContext = DEFAULT_CONTEXT) -> str:
    """Canonical identifier for matching (PO numbers, SKUs): NFKC folds
    width/compatibility forms, zero-width characters vanish, whitespace
    collapses, and the result is uppercased. Empty input is an error —
    an identifier that says nothing identifies nothing."""
    cleaned = unicodedata.normalize("NFKC", raw).translate(_ZERO_WIDTH)
    collapsed = " ".join(cleaned.split()).upper()
    if not collapsed:
        raise NormalizationError("identifier is empty after normalization")
    return collapsed


_TRUE_TOKENS = frozenset({"true", "yes", "y", "1", "x", "✓", "✔", "on", "checked"})
_FALSE_TOKENS = frozenset({"false", "no", "n", "0", "off", "unchecked", "-", "—"})


def normalize_boolean(raw: str, context: NormalizationContext = DEFAULT_CONTEXT) -> bool:
    token = normalize_whitespace(raw).casefold()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise NormalizationError(f"{token[:20]!r} is not a recognized boolean marker")


def normalize_enum(
    raw: str,
    allowed: Sequence[str],
    context: NormalizationContext = DEFAULT_CONTEXT,
) -> str:
    """Match the raw text to one of the schema's allowed values —
    whitespace- and case-insensitively, nothing fuzzier. The returned
    value is always the schema's own spelling."""
    if not allowed:
        raise NormalizationError("enum has no allowed values configured")
    token = normalize_whitespace(raw).casefold()
    for value in allowed:
        if normalize_whitespace(value).casefold() == token:
            return value
    raise NormalizationError("value does not match any allowed enum value")
