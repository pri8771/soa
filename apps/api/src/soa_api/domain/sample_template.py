"""Sample sales-order stream template (GTM-004).

The baseline a NEW tenant starts from during onboarding: a ready-made
sales-order process/stream/schema/rules/provider policy plus small sample
catalogs, so a customer can process a document end-to-end before authoring
their own configuration.

Two properties the acceptance turns on:

- **versioned** — the template carries a :data:`SampleStreamTemplate.version`.
  Any change to its shape is a new version; documents and imports are
  attributable to the exact version they came from.
- **importable without silent mutation of existing tenants** —
  :func:`import_template` returns a DEEP, DETACHED copy stamped with the
  source version. The tenant then owns that copy: editing it never
  touches the template, and re-versioning the template never
  retroactively changes a tenant that already imported an earlier
  version. A test proves the copy is independent in both directions.

The schema is DERIVED from the shipped PRC-010 baseline
(:mod:`soa_rules.baseline`) — the same canonical field types, criticality,
and enum values the worker pipeline and mock provider read — so it cannot
drift from the fields the platform actually processes. The rules are
authored in the CFG-004 rule DSL that ``validate_rule_set`` accepts (a
restricted, type-checked subset — presence, comparison, boolean
combinators; NOT the richer worker-side evaluator AST in
``soa_rules.baseline``), and every authored rule carries test cases the
validator executes. The provider policy points at the local mock so the
sample runs with no external dependency; a real deployment swaps it for a
configured provider. The whole template is validated against the REAL CFG
validators in tests — a sample a new tenant would import must not be
something the platform's own validators reject.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from soa_rules.baseline import (
    BASELINE_VERSION,
    CANONICAL_CRITICALITY,
    CANONICAL_ENUM_VALUES,
    CANONICAL_FIELD_TYPES,
)

__all__ = [
    "BASELINE_SALES_ORDER_TEMPLATE",
    "SAMPLE_TEMPLATE_VERSION",
    "ImportedTemplate",
    "SampleCatalog",
    "SampleStreamTemplate",
    "import_template",
]

#: Bump when the template's shape changes. Tied to the baseline rule
#: version it derives from, plus a template revision, so a change to
#: either is a new template version.
SAMPLE_TEMPLATE_VERSION = f"sales-order/{BASELINE_VERSION}+t1"

# Human labels for the canonical keys; keys not listed fall back to a
# title-cased form of the key.
_LABELS: dict[str, str] = {
    "po_number": "PO number",
    "order_date": "Order date",
    "requested_delivery_date": "Requested delivery date",
    "customer_name": "Customer name",
    "currency": "Currency",
    "total_amount": "Order total",
    "delivery_terms": "Delivery terms",
    "lines": "Line items",
    "lines.sku": "SKU",
    "lines.description": "Description",
    "lines.quantity": "Quantity",
    "lines.unit_price": "Unit price",
    "lines.line_total": "Line total",
}

# Fields the baseline validates on presence, so the schema marks them
# required; everything else (optional delivery date, derivable line total)
# stays optional.
_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "po_number",
        "order_date",
        "total_amount",
        "customer_name",
        "currency",
        "lines",
        "lines.sku",
        "lines.quantity",
        "lines.unit_price",
    }
)


def _label(key: str) -> str:
    return _LABELS.get(key, key.rsplit(".", 1)[-1].replace("_", " ").capitalize())


def _field_entry(key: str, display_key: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "key": display_key,
        "label": _label(key),
        "type": CANONICAL_FIELD_TYPES[key],
        "required": key in _REQUIRED_KEYS,
    }
    criticality = CANONICAL_CRITICALITY.get(key)
    if criticality is not None:
        entry["criticality"] = criticality
    enum_values = CANONICAL_ENUM_VALUES.get(key)
    if enum_values is not None:
        entry["enum_values"] = list(enum_values)
    return entry


def _build_schema() -> dict[str, Any]:
    """Assemble the CFG-003 schema dict from the canonical maps: header
    fields as bare keys, the ``lines`` table with its ``lines.*`` columns
    nested."""
    header_keys = [key for key in CANONICAL_FIELD_TYPES if "." not in key and key != "lines"]
    column_keys = [key for key in CANONICAL_FIELD_TYPES if key.startswith("lines.")]

    fields: list[dict[str, Any]] = [_field_entry(key, key) for key in header_keys]
    table_entry: dict[str, Any] = {
        "key": "lines",
        "label": _label("lines"),
        "type": "table",
        "required": "lines" in _REQUIRED_KEYS,
        "columns": [_field_entry(key, key.split(".", 1)[1]) for key in column_keys],
    }
    fields.append(table_entry)
    return {"fields": fields}


def _required_rule(key: str, label: str, *, action: str) -> dict[str, Any]:
    """A presence rule in the CFG-004 DSL, with the two test cases the
    validator executes (absent -> triggered, present -> not)."""
    return {
        "key": f"required.{key}",
        "severity": "error",
        "action": action,
        "condition": {"op": "not", "arg": {"op": "is_present", "key": key}},
        "message": f"{label} is required",
        "test_cases": [
            {"values": {}, "expect_triggered": True},
            {"values": {key: "present"}, "expect_triggered": False},
        ],
    }


def _build_rules() -> dict[str, Any]:
    """The starter rule set in the CFG-004 DSL: required header fields and
    a delivery-date sanity check. Deliberately modest — a new tenant
    tightens it in the rule builder; the heavier reconciliation semantics
    live in the worker-side baseline, not in authored config."""
    return {
        "version": "1",
        "rules": [
            _required_rule("po_number", "PO number", action="block"),
            _required_rule("order_date", "Order date", action="block"),
            _required_rule("total_amount", "Order total", action="route_to_review"),
            _required_rule("customer_name", "Customer name", action="route_to_review"),
            _required_rule("currency", "Currency", action="route_to_review"),
            {
                "key": "dates.delivery_after_order",
                "severity": "error",
                "action": "route_to_review",
                # Guarded by is_present: the delivery date is optional, so
                # its absence makes the rule pass, not park an order in review.
                "condition": {
                    "op": "and",
                    "args": [
                        {"op": "is_present", "key": "requested_delivery_date"},
                        {
                            "op": "gt",
                            "left": {"op": "field", "key": "order_date"},
                            "right": {"op": "field", "key": "requested_delivery_date"},
                        },
                    ],
                },
                "message": "Requested delivery date is before the order date",
                "test_cases": [
                    {"values": {"order_date": "2026-02-01"}, "expect_triggered": False},
                    {
                        "values": {
                            "order_date": "2026-02-01",
                            "requested_delivery_date": "2026-01-01",
                        },
                        "expect_triggered": True,
                    },
                ],
            },
        ],
    }


# Small, synthetic sample catalogs so matching and the catalog validators
# have something to work against out of the box. Customers replace these
# with their own imports (CAT epic).
_SAMPLE_CUSTOMERS = (
    {"code": "CUST-1001", "name": "Alpine Retail Ltd", "country": "GB"},
    {"code": "CUST-1002", "name": "Iberia Foods S.A.", "country": "ES"},
)
_SAMPLE_MATERIALS = (
    {
        "sku": "MAT-0101",
        "description": "Wholegrain cereal 500g case",
        "uom": "CS",
        "price": "14.50",
    },
    {"sku": "MAT-0102", "description": "Oat drink 1L case", "uom": "CS", "price": "11.80"},
)
_SAMPLE_UOMS = (
    {"code": "EA", "name": "Each"},
    {"code": "CS", "name": "Case"},
)


@dataclass(frozen=True)
class SampleCatalog:
    kind: str
    records: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SampleStreamTemplate:
    """A versioned, importable onboarding template. The nested data
    (schema/rules/catalogs) is plain JSON-compatible dicts so an import is
    a straight deep copy into the tenant's own draft configuration."""

    version: str
    process_name: str
    stream_name: str
    stream_language: str
    schema: dict[str, Any]
    rules: dict[str, Any]
    provider_policy: dict[str, Any]
    catalogs: tuple[SampleCatalog, ...] = field(default_factory=tuple)


BASELINE_SALES_ORDER_TEMPLATE = SampleStreamTemplate(
    version=SAMPLE_TEMPLATE_VERSION,
    process_name="Sales Orders",
    stream_name="Sample sales-order stream",
    stream_language="en",
    schema=_build_schema(),
    rules=_build_rules(),
    provider_policy={"provider_name": "mock", "capabilities": ["ocr", "field_extraction"]},
    catalogs=(
        SampleCatalog(kind="customers", records=_SAMPLE_CUSTOMERS),
        SampleCatalog(kind="materials", records=_SAMPLE_MATERIALS),
        SampleCatalog(kind="uoms", records=_SAMPLE_UOMS),
    ),
)


@dataclass(frozen=True)
class ImportedTemplate:
    """A detached copy of a template for one tenant to own. It records
    ``source_template_version`` so the origin is attributable, but the
    tenant's later edits to this data never affect the template and a new
    template version never affects this copy."""

    source_template_version: str
    process_name: str
    stream_name: str
    stream_language: str
    schema: dict[str, Any]
    rules: dict[str, Any]
    provider_policy: dict[str, Any]
    catalogs: tuple[SampleCatalog, ...]


def import_template(template: SampleStreamTemplate) -> ImportedTemplate:
    """Produce a tenant-owned copy of ``template``. Every mutable payload
    is DEEP-copied so the returned configuration is fully independent of
    the shared template constant — importing never mutates existing
    tenants, and a tenant editing its copy never mutates the template."""
    return ImportedTemplate(
        source_template_version=template.version,
        process_name=template.process_name,
        stream_name=template.stream_name,
        stream_language=template.stream_language,
        schema=copy.deepcopy(template.schema),
        rules=copy.deepcopy(template.rules),
        provider_policy=copy.deepcopy(template.provider_policy),
        catalogs=tuple(
            SampleCatalog(kind=c.kind, records=copy.deepcopy(c.records)) for c in template.catalogs
        ),
    )
