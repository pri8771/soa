"""Extraction-to-canonical mapping (CAN-003).

Pure function from the run's SELECTED values (the effective value per
field after review: latest correction, else the extraction's canonical
value) to a canonical order payload with per-field provenance
references. No I/O and no guessing: a value the mapper cannot place or
parse is a named error, and the caller (the approval service) turns
those errors into a clear refusal — a payload that cannot be built
cleanly must never be approved or exported half-right.

The output is validated against the versioned schema before it is
returned, so a mapper bug cannot emit an invalid payload.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from soa_canonical import CanonicalValidationError, validate_order
from soa_canonical.models import CanonicalOrder, LineItem, Money, ProvenanceEntry


@dataclass(frozen=True)
class CatalogReference:
    """ERP-neutral master-data identity selected for a source value."""

    catalog_id: str
    catalog_version_id: str
    catalog_record_id: str
    source_id: str
    display_name: str


@dataclass(frozen=True)
class SourceValue:
    """One selected value with its provenance: where it came from and,
    when a reviewer authored it, who."""

    value: Any
    origin: Literal["extracted", "corrected", "derived"] = "extracted"
    page_number: int | None = None
    quote: str | None = None
    actor: str | None = None
    catalog: CatalogReference | None = None


class CanonicalMappingError(Exception):
    """The selected values cannot form a canonical order. ``errors``
    names every problem by its canonical path."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        preview = "; ".join(errors[:5])
        more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
        super().__init__(f"cannot build the canonical order: {preview}{more}")


def _text(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _money(raw: Any, fallback_currency: str | None, path: str, errors: list[str]) -> Money | None:
    """Accept soa_normalize money dicts ({amount, currency|None}) or bare
    decimal strings; the currency falls back to the order's currency."""
    if raw is None:
        return None
    amount: Any = raw
    currency: Any = None
    if isinstance(raw, Mapping):
        amount = raw.get("amount")
        currency = raw.get("currency")
    currency = currency or fallback_currency
    amount_text = _text(amount)
    if amount_text is None:
        errors.append(f"{path}: amount is missing")
        return None
    if currency is None:
        errors.append(f"{path}: no currency on the value and no order currency to fall back to")
        return None
    return Money(amount=amount_text, currency=str(currency))


def map_sales_order(
    *,
    header: Mapping[str, SourceValue],
    lines: Sequence[Mapping[str, SourceValue]],
    document_id: str,
    run_id: str,
    document_sha256: str | None = None,
    received_at: str | None = None,
) -> CanonicalOrder:
    """Map the run's selected values to a canonical order.

    ``header`` keys are the flat canonical field keys the pipeline uses
    (po_number, order_date, customer_name, currency, total_amount,
    requested_delivery_date); each ``lines`` entry maps lines.* keys for
    one row, already in display order. Raises CanonicalMappingError
    listing EVERY problem."""
    errors: list[str] = []
    provenance: dict[str, ProvenanceEntry] = {}

    def note_provenance(path: str, source: SourceValue | None) -> None:
        if source is None or source.value is None:
            return
        entry = ProvenanceEntry(origin=source.origin)
        if source.page_number is not None:
            entry["page_number"] = source.page_number
        if source.quote is not None:
            entry["quote"] = source.quote
        if source.actor is not None:
            entry["actor"] = source.actor
        provenance[path] = entry

    def header_text(key: str, path: str, *, required: bool) -> str | None:
        source = header.get(key)
        text = _text(source.value) if source else None
        if text is None:
            if required:
                errors.append(f"{path}: required value is missing")
            return None
        note_provenance(path, source)
        return text

    po_number = header_text("po_number", "identifiers.po_number", required=True)
    order_date = header_text("order_date", "dates.order_date", required=True)
    currency = header_text("currency", "terms.currency", required=True)
    customer_source = header.get("customer_name")
    customer_name = header_text("customer_name", "parties.buyer.name", required=False)
    if customer_source is not None and customer_source.catalog is not None:
        customer_name = customer_source.catalog.display_name
    requested_delivery = header_text(
        "requested_delivery_date", "dates.requested_delivery_date", required=False
    )

    total_source = header.get("total_amount")
    grand_total: Money | None = None
    if total_source is None or total_source.value is None:
        errors.append("totals.grand_total: required value is missing")
    else:
        grand_total = _money(total_source.value, currency, "totals.grand_total", errors)
        note_provenance("totals.grand_total", total_source)

    line_items: list[LineItem] = []
    catalog_line_extensions: list[dict[str, Any]] = []
    for position, row in enumerate(lines):
        path = f"line_items[{position}]"
        quantity_source = row.get("lines.quantity")
        quantity = _text(quantity_source.value) if quantity_source else None
        if quantity is None:
            errors.append(f"{path}.quantity: required value is missing")
        total_cell = row.get("lines.line_total")
        line_total = (
            _money(total_cell.value, currency, f"{path}.line_total", errors)
            if total_cell and total_cell.value is not None
            else None
        )
        if line_total is None and not any(f"{path}.line_total" in e for e in errors):
            errors.append(f"{path}.line_total: required value is missing")
        if quantity is None or line_total is None:
            continue
        item = LineItem(line_number=position + 1, quantity=quantity, line_total=line_total)
        note_provenance(f"{path}.quantity", quantity_source)
        note_provenance(f"{path}.line_total", total_cell)
        sku_source = row.get("lines.sku")
        sku = _text(sku_source.value) if sku_source else None
        if sku_source is not None and sku_source.catalog is not None:
            sku = sku_source.catalog.source_id
        if sku is not None:
            item["sku"] = sku
            note_provenance(f"{path}.sku", sku_source)
        description_source = row.get("lines.description")
        description = _text(description_source.value) if description_source else None
        if description is not None:
            item["description"] = description
            note_provenance(f"{path}.description", description_source)
        price_source = row.get("lines.unit_price")
        if price_source is not None and price_source.value is not None:
            unit_price = _money(price_source.value, currency, f"{path}.unit_price", errors)
            if unit_price is not None:
                item["unit_price"] = unit_price
                note_provenance(f"{path}.unit_price", price_source)
        if sku_source is not None and sku_source.catalog is not None:
            catalog_line_extensions.append(
                {
                    "line_number": position + 1,
                    "catalog_id": sku_source.catalog.catalog_id,
                    "catalog_version_id": sku_source.catalog.catalog_version_id,
                    "catalog_record_id": sku_source.catalog.catalog_record_id,
                    "source_id": sku_source.catalog.source_id,
                }
            )
        line_items.append(item)
    if not lines:
        errors.append("line_items: the order has no line items")

    if errors:
        raise CanonicalMappingError(sorted(errors))
    assert po_number and order_date and currency and grand_total  # narrowed above

    # This mapper targets schema 1.0.0 by construction (the generated
    # Literal type pins it); a new schema version gets a new mapper path.
    order: CanonicalOrder = {
        "schema_version": "1.0.0",
        "identifiers": {"po_number": po_number},
        "dates": {"order_date": order_date},
        "terms": {"currency": currency},
        "totals": {"grand_total": grand_total},
        "line_items": line_items,
        "source": {"document_id": document_id, "run_id": run_id},
        "provenance": provenance,
    }
    if requested_delivery is not None:
        order["dates"]["requested_delivery_date"] = requested_delivery
    if customer_name is not None:
        buyer: dict[str, Any] = {"name": customer_name}
        if customer_source is not None and customer_source.catalog is not None:
            buyer["identifiers"] = [
                {"scheme": "customer-account", "value": customer_source.catalog.source_id}
            ]
            note_provenance("parties.buyer.identifiers[0].value", customer_source)
        order["parties"] = {"buyer": buyer}  # type: ignore[typeddict-item]
    catalog_extension: dict[str, Any] = {}
    if customer_source is not None and customer_source.catalog is not None:
        catalog_extension["customer"] = {
            "catalog_id": customer_source.catalog.catalog_id,
            "catalog_version_id": customer_source.catalog.catalog_version_id,
            "catalog_record_id": customer_source.catalog.catalog_record_id,
            "source_id": customer_source.catalog.source_id,
        }
    if catalog_line_extensions:
        catalog_extension["line_items"] = catalog_line_extensions
    if catalog_extension:
        order["extensions"] = {"x_soa_catalog": catalog_extension}
    if document_sha256 is not None:
        order["source"]["document_sha256"] = document_sha256
    if received_at is not None:
        order["source"]["received_at"] = received_at

    # The mapper can never emit an invalid payload: schema violations
    # (bad dates, malformed decimals, wrong currency codes) become
    # mapping errors with their JSON paths.
    try:
        validate_order(dict(order))
    except CanonicalValidationError as invalid:
        raise CanonicalMappingError(invalid.errors) from None
    return order
