"""Wire bound reference catalogs into deterministic order validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.catalog_line_validation import MaterialFacts, OrderLine, material_of, validate_lines
from soa_db.catalog_match_policy import resolve_match
from soa_db.catalog_matching import facts_of
from soa_db.catalog_selections import (
    BoundCatalog,
    CatalogSelectionSource,
    CatalogSelectionStatus,
    record_catalog_selection,
)
from soa_db.catalog_validation import OrderParties, party_of, validate_parties
from soa_db.catalogs import (
    CatalogBindingRepository,
    CatalogRecord,
    CatalogRecordRepository,
    CatalogRepository,
    resolve_catalog_version,
)
from soa_db.documents import Document
from soa_db.duplicate_po import assess_po_duplicates, find_po_duplicates, get_po_duplicate_policy
from soa_db.extracted_fields import ExtractedField
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun


@dataclass(frozen=True)
class BusinessValidationResult:
    findings: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]
    catalog_versions: Mapping[str, tuple[str, ...]]
    matches: int


def _value(row: ExtractedField | None) -> str | None:
    if row is None:
        return None
    value = row.normalized_value if row.normalized_value is not None else row.raw_value
    if isinstance(value, dict):
        amount = value.get("amount")
        return str(amount) if amount is not None else None
    return str(value) if value is not None else None


def _order_date(rows: Mapping[tuple[str, int | None], ExtractedField]) -> date | None:
    raw = _value(rows.get(("order_date", None)))
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def _bound_records(
    session: AsyncSession,
    context: OrganizationContext,
    document: Document,
) -> tuple[dict[str, BoundCatalog], dict[str, list[str]], list[str]]:
    candidates: dict[str, list[BoundCatalog]] = {}
    versions: dict[str, list[str]] = {}
    notes: list[str] = []
    bindings = await CatalogBindingRepository(session, context).list_for_stream(document.stream_id)
    for binding in bindings:
        catalog = await CatalogRepository(session, context).get(binding.catalog_id)
        version = await resolve_catalog_version(session, context, binding=binding)
        if catalog is None or version is None:
            notes.append(f"catalog binding {binding.id} resolves to no usable version")
            continue
        candidates.setdefault(catalog.catalog_type, []).append(
            BoundCatalog(
                catalog=catalog,
                version=version,
                records=tuple(
                    await CatalogRecordRepository(session, context).list_for_version(version.id)
                ),
            )
        )
        versions.setdefault(catalog.catalog_type, []).append(str(version.id))
    resolved: dict[str, BoundCatalog] = {}
    for catalog_type, scopes in candidates.items():
        if len(scopes) != 1:
            notes.append(
                f"{len(scopes)} {catalog_type} catalogs are bound; exactly one is required"
            )
            continue
        resolved[catalog_type] = scopes[0]
    return resolved, versions, notes


def _reason(
    code: str, message: str, field_key: str, row_index: int | None = None
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "field_key": field_key,
        "row_index": row_index,
        "rule_key": f"catalog.{code}",
    }


async def validate_order_business_data(
    session: AsyncSession,
    context: OrganizationContext,
    run: ProcessingRun,
    document: Document,
    rows: list[ExtractedField],
    *,
    stream_config: Mapping[str, Any],
    record_selections: bool = True,
) -> BusinessValidationResult:
    """Match bound catalogs, retain every decision, and run business checks."""
    by_key = {(row.field_key, row.row_index): row for row in rows}
    catalogs, versions, notes = await _bound_records(session, context, document)
    findings: list[dict[str, Any]] = []
    match_count = 0
    as_of = _order_date(by_key) or document.received_at.date()

    customer_scope = catalogs.get("customers")
    customer_records = list(customer_scope.records) if customer_scope is not None else []
    selected_customer: CatalogRecord | None = None
    customer_row = by_key.get(("customer_name", None))
    customer_value = _value(customer_row)
    if customer_records and customer_row is not None and customer_value:
        decision = resolve_match(
            customer_value,
            [facts_of(record) for record in customer_records],
            field_type="customer",
            as_of=as_of,
        )
        selected_record = None
        if decision.selected_source_id is not None:
            selected_record = next(
                record
                for record in customer_records
                if record.source_id == decision.selected_source_id
            )
        retained_decision = decision.to_record()
        if customer_scope is not None:
            retained_decision.update(
                {
                    "catalog_id": str(customer_scope.catalog.id),
                    "catalog_version_id": str(customer_scope.version.id),
                    "selected_record_id": (
                        str(selected_record.id) if selected_record is not None else None
                    ),
                }
            )
            if record_selections:
                await record_catalog_selection(
                    session,
                    context,
                    document_id=document.id,
                    run_id=run.id,
                    task_id=None,
                    field_key="customer_name",
                    row_index=None,
                    status=(
                        CatalogSelectionStatus.SELECTED
                        if selected_record is not None
                        else CatalogSelectionStatus.NEEDS_REVIEW
                    ),
                    selection_source=CatalogSelectionSource.MACHINE,
                    catalog=customer_scope.catalog,
                    version=customer_scope.version,
                    record=selected_record,
                    matched_value=customer_value,
                    selected_by="worker",
                    decision=retained_decision,
                )
        customer_row.catalog_match_json = retained_decision
        match_count += 1
        if selected_record is not None:
            selected_customer = selected_record
        else:
            findings.append(
                _reason(
                    "customer_unresolved",
                    f"customer {customer_value!r} was not unambiguously matched: "
                    f"{'; '.join(decision.reasons)}",
                    "customer_name",
                )
            )
    if customer_records:
        party_result = validate_parties(
            OrderParties(sold_to=party_of(selected_customer) if selected_customer else None),
            as_of=as_of,
        )
        findings.extend(finding.to_reason() for finding in party_result.findings)
        notes.extend(party_result.notes)

    product_scope = catalogs.get("products")
    product_records = list(product_scope.records) if product_scope is not None else []
    material_facts: list[MaterialFacts] = [material_of(record) for record in product_records]
    lines: list[tuple[OrderLine, MaterialFacts | None]] = []
    row_indexes = sorted(
        {
            row.row_index
            for row in rows
            if row.field_key.startswith("lines.") and row.row_index is not None
        }
    )
    currency = _value(by_key.get(("currency", None)))
    for row_index in row_indexes:
        sku_row = by_key.get(("lines.sku", row_index))
        sku = _value(sku_row)
        selected: MaterialFacts | None = None
        if product_records and sku_row is not None and sku:
            decision = resolve_match(
                sku,
                [facts_of(record) for record in product_records],
                field_type="material",
                as_of=as_of,
            )
            selected_record = None
            if decision.selected_source_id is not None:
                selected_record = next(
                    record
                    for record in product_records
                    if record.source_id == decision.selected_source_id
                )
            retained_decision = decision.to_record()
            if product_scope is not None:
                retained_decision.update(
                    {
                        "catalog_id": str(product_scope.catalog.id),
                        "catalog_version_id": str(product_scope.version.id),
                        "selected_record_id": (
                            str(selected_record.id) if selected_record is not None else None
                        ),
                    }
                )
                if record_selections:
                    await record_catalog_selection(
                        session,
                        context,
                        document_id=document.id,
                        run_id=run.id,
                        task_id=None,
                        field_key="lines.sku",
                        row_index=row_index,
                        status=(
                            CatalogSelectionStatus.SELECTED
                            if selected_record is not None
                            else CatalogSelectionStatus.NEEDS_REVIEW
                        ),
                        selection_source=CatalogSelectionSource.MACHINE,
                        catalog=product_scope.catalog,
                        version=product_scope.version,
                        record=selected_record,
                        matched_value=sku,
                        selected_by="worker",
                        decision=retained_decision,
                    )
            sku_row.catalog_match_json = retained_decision
            match_count += 1
            if selected_record is not None:
                selected = next(
                    material
                    for material in material_facts
                    if material.source_id == selected_record.source_id
                )
            else:
                findings.append(
                    _reason(
                        "material_unresolved",
                        f"line {row_index}: SKU {sku!r} was not unambiguously matched: "
                        f"{'; '.join(decision.reasons)}",
                        "lines.sku",
                        row_index,
                    )
                )
        lines.append(
            (
                OrderLine(
                    row_index=row_index,
                    sku=sku,
                    quantity=_value(by_key.get(("lines.quantity", row_index))),
                    uom=_value(by_key.get(("lines.uom", row_index))),
                    unit_price=_value(by_key.get(("lines.unit_price", row_index))),
                    currency=currency,
                ),
                selected,
            )
        )
    if product_records and lines:
        line_result = validate_lines(lines, catalog=material_facts, as_of=as_of)
        findings.extend(
            finding.to_reason()
            for finding in line_result.findings
            if finding.code != "material_unresolved"
        )
        notes.extend(line_result.notes)

    duplicate_search = await find_po_duplicates(
        session,
        context,
        stream_id=document.stream_id,
        exclude_document_id=document.id,
        po_number=_value(by_key.get(("po_number", None))),
        customer=customer_value,
        order_date=_value(by_key.get(("order_date", None))),
    )
    duplicate_result = assess_po_duplicates(
        duplicate_search.candidates,
        policy=get_po_duplicate_policy(stream_config),
    )
    findings.extend(finding.to_reason() for finding in duplicate_result.findings)
    notes.extend(duplicate_search.notes)
    notes.extend(duplicate_result.notes)
    return BusinessValidationResult(
        findings=tuple(findings),
        notes=tuple(notes),
        catalog_versions={key: tuple(value) for key, value in versions.items()},
        matches=match_count,
    )
