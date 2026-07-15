"""Wire bound reference catalogs into deterministic order validation.

This module lives in the shared database domain so worker validation and
post-correction API revalidation execute one business-policy implementation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.catalog_line_validation import MaterialFacts, OrderLine, material_of, validate_lines
from soa_db.catalog_match_policy import resolve_match
from soa_db.catalog_matching import facts_of
from soa_db.catalog_selections import (
    CATALOG_FIELD_CONFIG,
    BoundCatalog,
    CatalogSelectionError,
    CatalogSelectionSource,
    CatalogSelectionStatus,
    record_catalog_selection,
    resolve_bound_catalog,
)
from soa_db.catalog_validation import OrderParties, party_of, validate_parties
from soa_db.catalogs import (
    CATALOG_VERSION_PINS_KEY,
    CatalogRecord,
    CatalogVersionPin,
    parse_catalog_version_pins,
)
from soa_db.documents import Document
from soa_db.duplicate_po import (
    DuplicateOverride,
    assess_po_duplicates,
    find_po_duplicates,
    get_po_duplicate_policy,
)
from soa_db.duplicate_policy import DuplicatePolicy, get_duplicate_policy
from soa_db.extracted_fields import ExtractedField
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun


@dataclass(frozen=True)
class BusinessValidationResult:
    findings: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]
    catalog_versions: Mapping[str, tuple[str, ...]]
    matches: int
    override_record: Mapping[str, Any] | None = None


def merge_business_validation(
    evaluation_summary: Mapping[str, Any],
    decision_json: Mapping[str, Any],
    result: BusinessValidationResult,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge business findings into both public summaries without drift."""

    evaluation = dict(evaluation_summary)
    decision = dict(decision_json)
    decision["reasons"] = list(decision.get("reasons", []))
    severity_counts = {"error": 0, "warning": 0}
    for finding in result.findings:
        severity = str(finding.get("severity", "error"))
        if severity in severity_counts:
            severity_counts[severity] += 1
    triggered = dict(evaluation.get("triggered_by_severity", {}))
    for severity, count in severity_counts.items():
        triggered[severity] = int(triggered.get(severity, 0)) + count
    evaluation["triggered_by_severity"] = triggered
    evaluation["blocking"] = bool(evaluation.get("blocking")) or severity_counts["error"] > 0
    evaluation["review_required"] = bool(evaluation.get("review_required")) or bool(result.findings)
    evaluation["business_validation"] = {
        "findings": len(result.findings),
        "errors": severity_counts["error"],
        "warnings": severity_counts["warning"],
        "matches": result.matches,
        "catalog_versions": dict(result.catalog_versions),
        "override_used": result.override_record is not None,
    }
    if result.findings:
        decision["route"] = "review_required"
        decision["reasons"].extend(result.findings)
    return evaluation, decision


def _value(row: ExtractedField | None) -> str | None:
    if row is None:
        return None
    value = row.normalized_value if row.normalized_value is not None else row.raw_value
    if isinstance(value, dict):
        amount = value.get("amount")
        return str(amount) if amount is not None else None
    return str(value) if value is not None else None


def _provided_value(value: Any | None) -> str | None:
    if isinstance(value, dict):
        amount = value.get("amount")
        return str(amount) if amount is not None else None
    return str(value) if value is not None else None


async def _bound_records(
    session: AsyncSession,
    context: OrganizationContext,
    document: Document,
    catalog_version_pins: Sequence[CatalogVersionPin] | None,
) -> tuple[dict[str, BoundCatalog], dict[str, list[str]], list[str]]:
    resolved: dict[str, BoundCatalog] = {}
    versions: dict[str, list[str]] = {}
    notes: list[str] = []
    for catalog_type in sorted({config.catalog_type for config in CATALOG_FIELD_CONFIG.values()}):
        bound, error = await resolve_bound_catalog(
            session,
            context,
            stream_id=document.stream_id,
            catalog_type=catalog_type,
            catalog_version_pins=catalog_version_pins,
        )
        if error is not None:
            raise CatalogSelectionError(error)
        if bound is None:
            continue
        resolved[catalog_type] = bound
        versions[catalog_type] = [str(bound.version.id)]
    return resolved, versions, notes


def _reason(
    code: str,
    message: str,
    field_key: str,
    row_index: int | None = None,
    *,
    severity: str = "error",
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
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
    effective_values: Mapping[tuple[str, int | None], Any | None] | None = None,
    catalog_version_pins: Sequence[CatalogVersionPin] | None = None,
    duplicate_override: DuplicateOverride | None = None,
) -> BusinessValidationResult:
    """Match catalogs and validate one effective, optionally corrected order."""
    by_key = {(row.field_key, row.row_index): row for row in rows}
    supplied = effective_values or {}
    if catalog_version_pins is None and CATALOG_VERSION_PINS_KEY in stream_config:
        catalog_version_pins = parse_catalog_version_pins(
            stream_config.get(CATALOG_VERSION_PINS_KEY)
        )

    def value(field_key: str, row_index: int | None = None) -> str | None:
        key = (field_key, row_index)
        if effective_values is not None and key in supplied:
            return _provided_value(supplied[key])
        return _value(by_key.get(key))

    catalogs, versions, notes = await _bound_records(
        session, context, document, catalog_version_pins
    )
    findings: list[dict[str, Any]] = []
    match_count = 0
    raw_order_date = value("order_date")
    try:
        as_of = (
            date.fromisoformat(raw_order_date) if raw_order_date else document.received_at.date()
        )
    except ValueError:
        as_of = document.received_at.date()

    customer_scope = catalogs.get("customers")
    customer_records = list(customer_scope.records) if customer_scope is not None else []
    selected_customer: CatalogRecord | None = None
    customer_row = by_key.get(("customer_name", None))
    customer_value = value("customer_name")
    if customer_records and customer_value:
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
            if record_selections and customer_row is not None:
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
        if record_selections and customer_row is not None:
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
        | {
            row_index
            for (field_key, row_index), field_value in supplied.items()
            if field_key.startswith("lines.") and row_index is not None and field_value is not None
        }
    )
    currency = value("currency")
    for row_index in row_indexes:
        sku_row = by_key.get(("lines.sku", row_index))
        sku = value("lines.sku", row_index)
        selected: MaterialFacts | None = None
        if product_records and sku:
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
                if record_selections and sku_row is not None:
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
            if record_selections and sku_row is not None:
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
                    quantity=value("lines.quantity", row_index),
                    uom=value("lines.uom", row_index),
                    unit_price=value("lines.unit_price", row_index),
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
        po_number=value("po_number"),
        customer=customer_value,
        order_date=value("order_date"),
    )
    duplicate_candidates = duplicate_search.candidates
    exact_duplicate_policy = get_duplicate_policy(stream_config)
    if document.duplicate_of is not None and exact_duplicate_policy is DuplicatePolicy.ALLOW:
        # The exact-byte duplicate has already been marked and audited at
        # intake. ``allow`` means that specific match is transparent all the
        # way through validation, including the independent PO-number check.
        # Other PO candidates still follow business_duplicate_policy.
        duplicate_candidates = tuple(
            candidate
            for candidate in duplicate_candidates
            if candidate.document_id != document.duplicate_of
            and candidate.content_sha256 != document.content_sha256
        )
        if len(duplicate_candidates) != len(duplicate_search.candidates):
            notes.append(
                "the intake-confirmed exact duplicate was excluded from business duplicate "
                "routing by duplicate_policy=allow"
            )
    duplicate_result = assess_po_duplicates(
        duplicate_candidates,
        policy=get_po_duplicate_policy(stream_config),
        override=duplicate_override,
    )
    findings.extend(finding.to_reason() for finding in duplicate_result.findings)
    notes.extend(duplicate_search.notes)
    notes.extend(duplicate_result.notes)
    return BusinessValidationResult(
        findings=tuple(findings),
        notes=tuple(notes),
        catalog_versions={key: tuple(value) for key, value in versions.items()},
        matches=match_count,
        override_record=(
            duplicate_override.to_record()
            if duplicate_override is not None
            and any(
                finding.code == "duplicate_po_overridden" for finding in duplicate_result.findings
            )
            else None
        ),
    )
