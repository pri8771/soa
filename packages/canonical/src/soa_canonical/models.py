"""GENERATED from the canonical order schema — DO NOT EDIT.

Source: packages/canonical/src/soa_canonical/schemas/ (version 1.0.0)
Regenerate: uv run python -m soa_canonical.generate
The drift test (packages/canonical/tests/test_generation.py) fails CI on edits."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class Money(TypedDict):
    amount: str
    currency: str


class ExternalReference(TypedDict):
    scheme: str
    value: str


class Address(TypedDict):
    line1: NotRequired[str | None]
    line2: NotRequired[str | None]
    city: NotRequired[str | None]
    region: NotRequired[str | None]
    postal_code: NotRequired[str | None]
    country: NotRequired[str | None]


class PartyContact(TypedDict):
    email: NotRequired[str | None]
    phone: NotRequired[str | None]


class Party(TypedDict):
    name: str
    address: NotRequired[Address | None]
    contact: NotRequired[PartyContact | None]
    identifiers: NotRequired[list[ExternalReference]]


class LineItem(TypedDict):
    line_number: int
    sku: NotRequired[str | None]
    description: NotRequired[str | None]
    quantity: str
    unit_of_measure: NotRequired[str | None]
    unit_price: NotRequired[Money | None]
    line_total: Money
    requested_delivery_date: NotRequired[str | None]
    notes: NotRequired[list[Note]]


class Note(TypedDict):
    text: str
    author: NotRequired[str | None]
    visibility: Literal["internal", "external"]


class ProvenanceEntry(TypedDict):
    origin: Literal["extracted", "corrected", "derived"]
    page_number: NotRequired[int | None]
    quote: NotRequired[str | None]
    actor: NotRequired[str | None]


class CanonicalOrderIdentifiers(TypedDict):
    po_number: str
    sales_order_reference: NotRequired[str | None]
    external_references: NotRequired[list[ExternalReference]]


class CanonicalOrderParties(TypedDict):
    buyer: NotRequired[Party | None]
    seller: NotRequired[Party | None]
    ship_to: NotRequired[Party | None]
    bill_to: NotRequired[Party | None]


class CanonicalOrderDates(TypedDict):
    order_date: str
    requested_delivery_date: NotRequired[str | None]
    promised_delivery_date: NotRequired[str | None]


class CanonicalOrderTermsIncoterms(TypedDict):
    code: str
    location: NotRequired[str | None]


class CanonicalOrderTerms(TypedDict):
    currency: str
    payment_terms: NotRequired[str | None]
    incoterms: NotRequired[CanonicalOrderTermsIncoterms | None]


class CanonicalOrderTotals(TypedDict):
    subtotal: NotRequired[Money | None]
    discount_total: NotRequired[Money | None]
    tax_total: NotRequired[Money | None]
    shipping_total: NotRequired[Money | None]
    grand_total: Money


class CanonicalOrderSource(TypedDict):
    document_id: str
    run_id: str
    document_sha256: NotRequired[str | None]
    received_at: NotRequired[str | None]


class CanonicalOrder(TypedDict):
    schema_version: Literal["1.0.0"]
    identifiers: CanonicalOrderIdentifiers
    parties: NotRequired[CanonicalOrderParties]
    dates: CanonicalOrderDates
    terms: CanonicalOrderTerms
    totals: CanonicalOrderTotals
    line_items: list[LineItem]
    notes: NotRequired[list[Note]]
    source: CanonicalOrderSource
    provenance: NotRequired[dict[str, ProvenanceEntry]]
    extensions: NotRequired[dict[str, Any]]
