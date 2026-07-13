"""Deterministic canonical normalization (PRC-008).

Turns raw extracted strings into canonical typed values. Three promises,
enforced everywhere:

- the RAW value is never mutated — normalizers are pure functions that
  return a new canonical value (callers store both, see PRC-007);
- the same input always produces the same output — no clocks, no
  randomness, no environment;
- nothing is guessed — genuinely ambiguous input (``03/04/2026``,
  ``1,234``) raises :class:`NormalizationError` unless the caller
  supplies explicit locale/currency context that resolves it.
"""

from soa_normalize.context import NormalizationContext, NormalizationError
from soa_normalize.dates import normalize_date_iso
from soa_normalize.numbers import normalize_currency_code, normalize_decimal, normalize_money
from soa_normalize.registry import NORMALIZERS, normalize
from soa_normalize.text import (
    normalize_boolean,
    normalize_enum,
    normalize_identifier,
    normalize_whitespace,
)
from soa_normalize.units import normalize_uom

__all__ = [
    "NORMALIZERS",
    "NormalizationContext",
    "NormalizationError",
    "normalize",
    "normalize_boolean",
    "normalize_currency_code",
    "normalize_date_iso",
    "normalize_decimal",
    "normalize_enum",
    "normalize_identifier",
    "normalize_money",
    "normalize_uom",
    "normalize_whitespace",
]
