"""The deterministic demo tenant defined by docs/DELIVERY_PLAN.md §7.3.

Everything here is synthetic. Aliases are the stable handles E2E tests use;
IDs derive from aliases via ``stable_id``.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from soa_fixtures.ids import stable_id


@dataclass(frozen=True)
class Fixture:
    alias: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> uuid.UUID:
        return stable_id(self.alias)


@dataclass(frozen=True)
class DocumentFixture(Fixture):
    """A synthetic purchase order with ground-truth expectations."""

    scenario: str = "success"
    text: str = ""
    expected_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DemoTenant:
    organization: Fixture
    workspace: Fixture
    process: Fixture
    streams: tuple[Fixture, ...]
    users: tuple[Fixture, ...]
    catalogs: tuple[Fixture, ...]
    schema: Fixture
    rules: Fixture
    provider_policy: Fixture
    documents: tuple[DocumentFixture, ...]


_CUSTOMERS = [
    {"code": "CUST-1001", "name": "Alpine Retail Ltd", "country": "GB"},
    {"code": "CUST-1002", "name": "Iberia Foods S.A.", "country": "ES"},
    {"code": "CUST-1003", "name": "Northgate Wholesale plc", "country": "GB"},
]

_SHIP_TOS = [
    {"code": "SHIP-2001", "customer": "CUST-1001", "city": "Manchester", "postal": "M17 1EH"},
    {"code": "SHIP-2002", "customer": "CUST-1001", "city": "Leeds", "postal": "LS9 8AH"},
    {"code": "SHIP-2003", "customer": "CUST-1002", "city": "Madrid", "postal": "28045"},
]

_MATERIALS = [
    {
        "sku": "MAT-0101",
        "description": "Wholegrain cereal 500g case",
        "uom": "CS",
        "price": "14.50",
    },
    {"sku": "MAT-0102", "description": "Oat drink 1L case", "uom": "CS", "price": "11.80"},
    {"sku": "MAT-0103", "description": "Dried fruit mix 2kg", "uom": "EA", "price": "9.20"},
]

_UOMS = [
    {"code": "EA", "name": "Each"},
    {"code": "CS", "name": "Case"},
    {"code": "PAL", "name": "Pallet"},
]


def _po_text(po_number: str, customer: str, lines: list[tuple[str, int, str]]) -> str:
    body = "\n".join(f"{sku}  qty {qty}  unit {uom}" for sku, qty, uom in lines)
    return f"PURCHASE ORDER {po_number}\nCustomer: {customer}\nCurrency: GBP\n{body}\n"


_DOCUMENTS = (
    DocumentFixture(
        alias="document:po-clean",
        scenario="success",
        text=_po_text("PO-90001", "Alpine Retail Ltd", [("MAT-0101", 40, "CS")]),
        expected_fields={"po_number": "PO-90001", "customer": "CUST-1001", "line_count": 1},
        data={"filename": "po-90001-alpine.pdf"},
    ),
    DocumentFixture(
        alias="document:po-low-confidence",
        scenario="low_confidence",
        text=_po_text("PO-9OOO2", "Alp1ne Reta1l", [("MAT-0102", 12, "CS")]),
        expected_fields={"po_number": "PO-90002", "customer": "CUST-1001", "line_count": 1},
        data={"filename": "po-90002-alpine-scan.pdf"},
    ),
    DocumentFixture(
        alias="document:po-validation-failure",
        scenario="validation_failure",
        text=_po_text("PO-90003", "Iberia Foods S.A.", [("MAT-0103", -5, "EA")]),
        expected_fields={"po_number": "PO-90003", "customer": "CUST-1002", "line_count": 1},
        data={"filename": "po-90003-iberia.pdf"},
    ),
    DocumentFixture(
        alias="document:po-multipage",
        scenario="multi_page_lines",
        text=_po_text(
            "PO-90004",
            "Northgate Wholesale plc",
            [("MAT-0101", 10, "CS"), ("MAT-0102", 20, "CS"), ("MAT-0103", 30, "EA")],
        ),
        expected_fields={"po_number": "PO-90004", "customer": "CUST-1003", "line_count": 3},
        data={"filename": "po-90004-northgate.pdf", "pages": 2},
    ),
    DocumentFixture(
        alias="document:po-duplicate",
        scenario="duplicate_po",
        text=_po_text("PO-90001", "Alpine Retail Ltd", [("MAT-0101", 40, "CS")]),
        expected_fields={"po_number": "PO-90001", "customer": "CUST-1001", "line_count": 1},
        data={"filename": "po-90001-alpine-resend.pdf"},
    ),
)


DEMO_TENANT = DemoTenant(
    organization=Fixture(alias="org:northstar", data={"name": "Northstar Distribution"}),
    workspace=Fixture(alias="workspace:europe", data={"name": "Europe"}),
    process=Fixture(alias="process:sales-orders", data={"name": "Sales Orders"}),
    streams=(
        Fixture(alias="stream:uk", data={"name": "United Kingdom", "language": "en-GB"}),
        Fixture(alias="stream:spain", data={"name": "Spain", "language": "es-ES"}),
    ),
    users=(
        Fixture(alias="user:admin", data={"email": "admin@northstar.example", "role": "admin"}),
        Fixture(
            alias="user:reviewer",
            data={"email": "reviewer@northstar.example", "role": "reviewer"},
        ),
        Fixture(
            alias="user:supervisor",
            data={"email": "supervisor@northstar.example", "role": "supervisor"},
        ),
        Fixture(
            alias="user:integration-admin",
            data={"email": "integrations@northstar.example", "role": "integration_admin"},
        ),
        Fixture(
            alias="user:auditor",
            data={"email": "auditor@northstar.example", "role": "auditor"},
        ),
    ),
    catalogs=(
        Fixture(alias="catalog:customers", data={"kind": "customers", "records": _CUSTOMERS}),
        Fixture(alias="catalog:ship-tos", data={"kind": "ship_tos", "records": _SHIP_TOS}),
        Fixture(alias="catalog:materials", data={"kind": "materials", "records": _MATERIALS}),
        Fixture(alias="catalog:uoms", data={"kind": "uoms", "records": _UOMS}),
    ),
    schema=Fixture(
        alias="schema:sales-order-v1",
        data={
            "fields": [
                {"path": "po_number", "type": "string", "required": True, "critical": True},
                {"path": "customer", "type": "string", "required": True, "critical": True},
                {"path": "currency", "type": "currency", "required": True, "critical": True},
                {"path": "lines[].sku", "type": "string", "required": True, "critical": True},
                {"path": "lines[].quantity", "type": "integer", "required": True, "critical": True},
                {"path": "lines[].uom", "type": "string", "required": True, "critical": False},
            ]
        },
    ),
    rules=Fixture(
        alias="rules:sales-order-v1",
        data={
            "rules": [
                {"id": "qty-positive", "when": "lines[].quantity <= 0", "then": "error"},
                {"id": "known-customer", "when": "customer not in catalog", "then": "review"},
                {"id": "duplicate-po", "when": "po_number already accepted", "then": "error"},
            ]
        },
    ),
    provider_policy=Fixture(
        alias="policy:mock-extraction",
        data={"extraction_provider": "mock", "ocr_provider": "none"},
    ),
    documents=_DOCUMENTS,
)
