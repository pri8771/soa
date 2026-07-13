"""Canonical payload construction at approval time (CAN-003).

Gathers the run's SELECTED values — the latest correction per field,
else the extraction's canonical value — with their provenance (which
page, which quote, who corrected), hands them to the pure mapper in
``soa_canonical.mapping``, and stores the result immutably per
(run, schema version). Mapping errors propagate to the approval
service, where they BLOCK the approval with every problem named.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_canonical import CURRENT_VERSION
from soa_canonical.mapping import SourceValue, map_sales_order
from soa_canonical.models import CanonicalOrder
from soa_db.canonical_payloads import CanonicalPayload, record_canonical_payload
from soa_db.corrections import FieldCorrection, FieldCorrectionRepository, latest_corrections
from soa_db.documents import Document
from soa_db.extracted_fields import ExtractedField, ExtractedFieldRepository
from soa_db.repository import OrganizationContext


def _source_value(
    field: ExtractedField | None, correction: FieldCorrection | None
) -> SourceValue | None:
    """The selected value + provenance for one field position."""
    if correction is not None:
        value = (
            correction.corrected_normalized_value
            if correction.corrected_normalized_value is not None
            else correction.corrected_raw_value
        )
        return SourceValue(value=value, origin="corrected", actor=correction.corrected_by)
    if field is None:
        return None
    value = field.normalized_value if field.normalized_value is not None else field.raw_value
    spans = field.evidence_spans()
    first = spans[0] if spans else None
    return SourceValue(
        value=value,
        origin="extracted",
        page_number=first.page_number if first else None,
        quote=first.quote if first else None,
    )


async def build_canonical_order(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document: Document,
    run_id: uuid.UUID,
) -> CanonicalOrder:
    """Assemble the mapper input from the run and map it. Raises
    soa_canonical.mapping.CanonicalMappingError when the selected values
    cannot form a canonical order."""
    fields = await ExtractedFieldRepository(session, context).list_for_run(run_id)
    corrections = latest_corrections(
        await FieldCorrectionRepository(session, context).list_for_run(run_id)
    )

    header: dict[str, SourceValue] = {}
    rows: dict[int, dict[str, SourceValue]] = {}
    covered: set[tuple[str, int | None]] = set()
    for field in fields:
        correction = corrections.get((field.field_key, field.row_index))
        covered.add((field.field_key, field.row_index))
        selected = _source_value(field, correction)
        if selected is None:
            continue
        if field.row_index is None:
            header[field.field_key] = selected
        else:
            rows.setdefault(field.row_index, {})[field.field_key] = selected
    # Rows the reviewer ADDED exist only as corrections.
    for (key, row_index), correction in corrections.items():
        if (key, row_index) in covered:
            continue
        selected = _source_value(None, correction)
        if selected is None:
            continue
        if row_index is None:
            header[key] = selected
        else:
            rows.setdefault(row_index, {})[key] = selected

    # A row whose every selected value is None was cleared by review.
    lines: list[dict[str, SourceValue]] = [
        cells
        for _, cells in sorted(rows.items())
        if any(cell.value is not None for cell in cells.values())
    ]

    return map_sales_order(
        header=header,
        lines=lines,
        document_id=str(document.id),
        run_id=str(run_id),
        document_sha256=document.content_sha256,
        received_at=document.received_at.isoformat() if document.received_at else None,
    )


async def persist_canonical_order(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document: Document,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None,
    order: CanonicalOrder,
    actor_id: str,
) -> CanonicalPayload:
    payload: dict[str, Any] = dict(order)
    return await record_canonical_payload(
        session,
        context,
        document_id=document.id,
        run_id=run_id,
        task_id=task_id,
        schema_version=CURRENT_VERSION,
        payload=payload,
        actor_id=actor_id,
    )
