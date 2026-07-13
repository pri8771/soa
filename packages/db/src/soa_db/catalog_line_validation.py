"""Material, UOM, price, and package validation (CAT-012).

Line-item business validation once each line's SKU has been resolved to
a catalog material record (CAT-009/CAT-010). Pure and deterministic;
findings reuse the CAT-011 shape (stable code, written message, field
key, ROW INDEX) so the review flow surfaces them unchanged.

Semantics — stated, not implied:

- **numbers** are exact ``Decimal``s parsed from the normalized line
  values; NOTHING is rounded during comparison. The only rounding is
  presentational: tolerance bands in messages are quantized to 4
  decimal places, ROUND_HALF_UP.
- **currency** is compared literally (case-insensitive ISO code). A
  catalog price in another currency is a finding — the platform never
  invents an exchange rate.
- **effective dates** are inclusive on both ends and checked against
  the caller's ``as_of`` (normally the order date); no ``as_of`` means
  no enforcement, stated in the notes.
- **UOM conversion** uses the record's explicit factors only. The
  allowed set is the base UOM plus the declared conversions; anything
  else is an error, never coerced.

Attribute contract for ``products`` catalog records (all optional —
absence degrades to notes, contradiction to findings):

- ``customer_item_ids`` — list of the customer's own item numbers for
  this material (the customer item mapping);
- ``base_uom`` — the unit quantities are managed in (e.g. "EA");
- ``uom_conversions`` — mapping of UOM -> units of base per 1 of that
  UOM, as exact decimal strings (e.g. {"BOX": "12"});
- ``price`` / ``currency`` — the list price per base UOM and its ISO
  currency code;
- ``package_multiple`` — order quantities (in base UOM) must be a
  whole multiple of this.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from soa_db.catalog_matching import normalize_identifier
from soa_db.catalog_validation import ValidationFinding

_FOUR_PLACES = Decimal("0.0001")


class LinePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class LinePolicy:
    """Tolerances for CAT-012. The defaults are the platform baseline;
    streams override via configuration."""

    #: Allowed relative deviation of the line's unit price from the
    #: catalog price (0.01 = ±1%).
    price_tolerance_ratio: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if self.price_tolerance_ratio < 0:
            raise LinePolicyError("price_tolerance_ratio cannot be negative")


@dataclass(frozen=True)
class MaterialFacts:
    """The validation-relevant facts of one catalog material record."""

    source_id: str
    display_name: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    effective_from: date | None = None
    effective_to: date | None = None
    aliases: tuple[str, ...] = ()

    def effective_on(self, as_of: date) -> bool:
        if self.effective_from and as_of < self.effective_from:
            return False
        if self.effective_to and as_of > self.effective_to:
            return False
        return True


def material_of(record: object) -> MaterialFacts:
    """Adapt a CatalogRecord row (or anything record-shaped)."""
    return MaterialFacts(
        source_id=record.source_id,  # type: ignore[attr-defined]
        display_name=record.display_name,  # type: ignore[attr-defined]
        attributes=dict(record.attributes),  # type: ignore[attr-defined]
        effective_from=record.effective_from,  # type: ignore[attr-defined]
        effective_to=record.effective_to,  # type: ignore[attr-defined]
        aliases=tuple(record.aliases),  # type: ignore[attr-defined]
    )


@dataclass(frozen=True)
class OrderLine:
    """One line's normalized document values (PRC-008 output)."""

    row_index: int
    sku: str | None = None
    quantity: str | None = None
    uom: str | None = None
    unit_price: str | None = None
    #: The order's currency (header field, applied per line).
    currency: str | None = None


@dataclass(frozen=True)
class LineDerived:
    """What validation could derive for one line — exposed so exports
    and rules can reuse it instead of reconverting."""

    row_index: int
    base_uom: str | None = None
    #: Quantity converted to the base UOM (exact, unrounded).
    base_quantity: Decimal | None = None
    #: Unit price converted to per-base-UOM (exact, unrounded).
    base_unit_price: Decimal | None = None


@dataclass(frozen=True)
class LineValidation:
    findings: tuple[ValidationFinding, ...]
    notes: tuple[str, ...]
    derived: tuple[LineDerived, ...]

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None


def _identifier_forms(material: MaterialFacts) -> dict[str, str]:
    """Every identifier that maps to this material, normalized ->
    which attribute supplied it."""
    forms = {normalize_identifier(material.source_id): "source id"}
    for alias in material.aliases:
        forms.setdefault(normalize_identifier(alias), "alias")
    raw_items = material.attributes.get("customer_item_ids")
    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, str) and item.strip():
                forms.setdefault(normalize_identifier(item), "customer item id")
    return forms


def _conversions(
    material: MaterialFacts,
) -> tuple[str | None, dict[str, Decimal], list[str]]:
    """(base_uom, factors-per-uom including base=1, problems)."""
    problems: list[str] = []
    base = material.attributes.get("base_uom")
    base_uom = base.strip().upper() if isinstance(base, str) and base.strip() else None
    factors: dict[str, Decimal] = {}
    if base_uom is not None:
        factors[base_uom] = Decimal(1)
    raw = material.attributes.get("uom_conversions")
    if isinstance(raw, Mapping):
        for uom, factor_raw in raw.items():
            factor = _decimal(str(factor_raw))
            if not isinstance(uom, str) or factor is None or factor <= 0:
                problems.append(
                    f"conversion {uom!r} -> {factor_raw!r} is not a positive decimal "
                    "and was ignored"
                )
                continue
            factors[uom.strip().upper()] = factor
    elif raw is not None:
        problems.append("uom_conversions is not a mapping and was ignored")
    return base_uom, factors, problems


def _band(price: Decimal, ratio: Decimal) -> tuple[Decimal, Decimal]:
    low = (price * (1 - ratio)).quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)
    high = (price * (1 + ratio)).quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)
    return low, high


def validate_lines(
    lines: list[tuple[OrderLine, MaterialFacts | None]],
    *,
    catalog: list[MaterialFacts] | None = None,
    policy: LinePolicy | None = None,
    as_of: date | None = None,
) -> LineValidation:
    """Run every CAT-012 check the available data supports. ``catalog``
    (the full bound material list) enables the maps-elsewhere check on
    the customer item mapping; without it that check is skipped with a
    note."""
    effective_policy = policy or LinePolicy()
    findings: list[ValidationFinding] = []
    notes: list[str] = []
    derived: list[LineDerived] = []
    if as_of is None:
        notes.append("no order date given — effective windows were not enforced")

    for line, material in lines:
        row = line.row_index
        if material is None:
            findings.append(
                ValidationFinding(
                    code="material_unresolved",
                    severity="warning",
                    message=(
                        f"line {row}: {line.sku!r} resolved to no catalog material — "
                        "matching left this line for review"
                    ),
                    field_key="lines.sku",
                    row_index=row,
                )
            )
            derived.append(LineDerived(row_index=row))
            continue

        _check_item_mapping(line, material, catalog, findings, notes)

        if as_of is not None and not material.effective_on(as_of):
            findings.append(
                ValidationFinding(
                    code="material_out_of_effect",
                    severity="error",
                    message=(
                        f"line {row}: {material.display_name!r} ({material.source_id}) "
                        f"is out of effect on {as_of.isoformat()}"
                    ),
                    field_key="lines.sku",
                    row_index=row,
                )
            )

        line_derived = _check_uom_and_quantity(line, material, findings, notes)
        derived.append(line_derived)
        _check_package_multiple(line, material, line_derived, findings)
        _check_price(line, material, line_derived, effective_policy, findings, notes)

    return LineValidation(findings=tuple(findings), notes=tuple(notes), derived=tuple(derived))


def _check_item_mapping(
    line: OrderLine,
    material: MaterialFacts,
    catalog: list[MaterialFacts] | None,
    findings: list[ValidationFinding],
    notes: list[str],
) -> None:
    row = line.row_index
    if not line.sku or not line.sku.strip():
        notes.append(f"line {row}: no document SKU — the item mapping was not checked")
        return
    normalized = normalize_identifier(line.sku)
    via = _identifier_forms(material).get(normalized)
    if via is not None:
        notes.append(f"line {row}: {line.sku!r} maps to {material.source_id!r} via its {via}")
        return
    # The picked material does not carry this identifier. Does another
    # record claim it explicitly?
    if catalog is not None:
        for rival in catalog:
            if rival.source_id == material.source_id:
                continue
            if normalized in _identifier_forms(rival):
                findings.append(
                    ValidationFinding(
                        code="customer_item_maps_elsewhere",
                        severity="error",
                        message=(
                            f"line {row}: {line.sku!r} is {rival.source_id!r}'s "
                            f"identifier, but the line was resolved to "
                            f"{material.source_id!r}"
                        ),
                        field_key="lines.sku",
                        row_index=row,
                    )
                )
                return
    else:
        notes.append(f"line {row}: the maps-elsewhere check was skipped — no catalog list given")
    findings.append(
        ValidationFinding(
            code="customer_item_mapping_unverified",
            severity="warning",
            message=(
                f"line {row}: {line.sku!r} is not among {material.source_id!r}'s "
                "identifiers (source id, aliases, customer item ids) — the mapping "
                "came from fuzzy matching or a manual pick"
            ),
            field_key="lines.sku",
            row_index=row,
        )
    )


def _check_uom_and_quantity(
    line: OrderLine,
    material: MaterialFacts,
    findings: list[ValidationFinding],
    notes: list[str],
) -> LineDerived:
    row = line.row_index
    base_uom, factors, problems = _conversions(material)
    for problem in problems:
        notes.append(f"line {row}: {problem}")
    if base_uom is None:
        notes.append(
            f"line {row}: {material.source_id!r} declares no base UOM — "
            "UOM, package, and price checks were limited"
        )
        return LineDerived(row_index=row)

    quantity = _decimal(line.quantity)
    if line.quantity is not None and quantity is None:
        findings.append(
            ValidationFinding(
                code="quantity_not_numeric",
                severity="warning",
                message=f"line {row}: quantity {line.quantity!r} is not a number",
                field_key="lines.quantity",
                row_index=row,
            )
        )

    if line.uom is None or not line.uom.strip():
        notes.append(f"line {row}: no UOM on the line — the base UOM {base_uom!r} was assumed")
        uom = base_uom
    else:
        uom = line.uom.strip().upper()
    factor = factors.get(uom)
    if factor is None:
        allowed = ", ".join(sorted(factors))
        findings.append(
            ValidationFinding(
                code="uom_not_allowed",
                severity="error",
                message=(
                    f"line {row}: UOM {uom!r} is not allowed for "
                    f"{material.source_id!r} (allowed: {allowed})"
                ),
                field_key="lines.uom" if line.uom else "lines.quantity",
                row_index=row,
            )
        )
        return LineDerived(row_index=row, base_uom=base_uom)

    base_quantity = quantity * factor if quantity is not None else None
    unit_price = _decimal(line.unit_price)
    #: Price per base unit: 1 line-UOM costs unit_price and contains
    #: ``factor`` base units.
    base_unit_price = unit_price / factor if unit_price is not None else None
    return LineDerived(
        row_index=row,
        base_uom=base_uom,
        base_quantity=base_quantity,
        base_unit_price=base_unit_price,
    )


def _check_package_multiple(
    line: OrderLine,
    material: MaterialFacts,
    derived: LineDerived,
    findings: list[ValidationFinding],
) -> None:
    raw = material.attributes.get("package_multiple")
    if raw is None or derived.base_quantity is None:
        return
    multiple = _decimal(str(raw))
    if multiple is None or multiple <= 0:
        return
    remainder = derived.base_quantity % multiple
    if remainder != 0:
        row = line.row_index
        findings.append(
            ValidationFinding(
                code="package_multiple_violation",
                severity="error",
                message=(
                    f"line {row}: {derived.base_quantity} {derived.base_uom} is not a "
                    f"whole multiple of the package size {multiple} "
                    f"(remainder {remainder})"
                ),
                field_key="lines.quantity",
                row_index=row,
            )
        )


def _check_price(
    line: OrderLine,
    material: MaterialFacts,
    derived: LineDerived,
    policy: LinePolicy,
    findings: list[ValidationFinding],
    notes: list[str],
) -> None:
    row = line.row_index
    catalog_price = _decimal(
        str(material.attributes.get("price")) if material.attributes.get("price") else None
    )
    if catalog_price is None or catalog_price <= 0:
        notes.append(f"line {row}: {material.source_id!r} carries no price — not checked")
        return
    if derived.base_uom is None:
        return  # already noted: no base UOM limits the price check
    if derived.base_unit_price is None:
        if line.unit_price is not None and _decimal(line.unit_price) is None:
            findings.append(
                ValidationFinding(
                    code="unit_price_not_numeric",
                    severity="warning",
                    message=f"line {row}: unit price {line.unit_price!r} is not a number",
                    field_key="lines.unit_price",
                    row_index=row,
                )
            )
        else:
            notes.append(
                f"line {row}: the unit price was missing or could not be converted "
                "to the base UOM — the price was not checked"
            )
        return

    catalog_currency = material.attributes.get("currency")
    if (
        isinstance(catalog_currency, str)
        and catalog_currency.strip()
        and line.currency
        and catalog_currency.strip().upper() != line.currency.strip().upper()
    ):
        findings.append(
            ValidationFinding(
                code="price_currency_mismatch",
                severity="error",
                message=(
                    f"line {row}: the order is in {line.currency.strip().upper()} but "
                    f"{material.source_id!r}'s price is in "
                    f"{catalog_currency.strip().upper()} — no exchange rate is applied"
                ),
                field_key="lines.unit_price",
                row_index=row,
            )
        )
        return  # comparing across currencies would be meaningless

    deviation = abs(derived.base_unit_price - catalog_price) / catalog_price
    if deviation > policy.price_tolerance_ratio:
        low, high = _band(catalog_price, policy.price_tolerance_ratio)
        findings.append(
            ValidationFinding(
                code="price_out_of_tolerance",
                severity="error",
                message=(
                    f"line {row}: unit price {derived.base_unit_price} per "
                    f"{derived.base_uom} is outside the allowed band "
                    f"[{low}, {high}] around the catalog price {catalog_price} "
                    f"(tolerance ±{policy.price_tolerance_ratio})"
                ),
                field_key="lines.unit_price",
                row_index=row,
            )
        )
    elif derived.base_unit_price != catalog_price:
        notes.append(
            f"line {row}: unit price {derived.base_unit_price} differs from the "
            f"catalog price {catalog_price} but is within tolerance"
        )


__all__ = [
    "LineDerived",
    "LinePolicy",
    "LinePolicyError",
    "LineValidation",
    "MaterialFacts",
    "OrderLine",
    "material_of",
    "validate_lines",
]
