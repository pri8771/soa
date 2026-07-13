"""Named-normalizer registry (PRC-008).

Schema field definitions (CFG-003) reference normalizers by NAME; this
registry is what those names mean. Adding a normalizer here is an
interface change — stored canonical values depend on these semantics, so
changed behaviour needs a new name, not an edit.
"""

from collections.abc import Callable, Sequence
from typing import Any

from soa_normalize.context import DEFAULT_CONTEXT, NormalizationContext, NormalizationError
from soa_normalize.dates import normalize_date_iso
from soa_normalize.numbers import normalize_currency_code, normalize_decimal, normalize_money
from soa_normalize.text import (
    normalize_boolean,
    normalize_enum,
    normalize_identifier,
    normalize_whitespace,
)
from soa_normalize.units import normalize_uom

Normalizer = Callable[[str, NormalizationContext], Any]

NORMALIZERS: dict[str, Normalizer] = {
    "trim": normalize_whitespace,
    "identifier": normalize_identifier,
    "date_iso": normalize_date_iso,
    "decimal": normalize_decimal,
    "money": normalize_money,
    "currency_code": normalize_currency_code,
    "uom": normalize_uom,
    "boolean": normalize_boolean,
}


def normalize(
    kind: str,
    raw: str,
    *,
    context: NormalizationContext = DEFAULT_CONTEXT,
    enum_values: Sequence[str] | None = None,
) -> Any:
    """Dispatch by registry name. ``enum`` is special-cased because it
    needs the schema's allowed values; every other normalizer takes only
    the raw string and the context."""
    if kind == "enum":
        if enum_values is None:
            raise NormalizationError("enum normalization requires the allowed values")
        return normalize_enum(raw, enum_values, context)
    normalizer = NORMALIZERS.get(kind)
    if normalizer is None:
        raise NormalizationError(f"unknown normalizer {kind!r}")
    return normalizer(raw, context)
