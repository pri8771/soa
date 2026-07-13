"""Duplicate PO business validation (CAT-013).

Exact-content duplicates (same bytes) are ING-006's job at intake.
This module catches the BUSINESS duplicate: the same customer sending
the same PO number again in a different file — a retyped order, a
second scan, or a legitimate revision. It has two halves:

- **discovery** (``find_po_duplicates``) — other documents in the same
  stream whose EFFECTIVE po_number (latest reviewer correction, else
  the latest run's extraction) matches the incoming one under the
  CAT-006 identifier normalization. Customer and order-date values are
  compared the same way: a candidate with a DIFFERENT customer is not
  a duplicate (excluded, with a note); a candidate whose customer or
  date cannot be compared stays, with the uncertainty recorded on it.
- **policy** (``assess_po_duplicates``) — pure and stream-configurable
  (``business_duplicate_policy``: warn | block | allow, default warn).
  Findings reuse the CAT-011 shape. Status matters: candidates whose
  document was REJECTED never warn or block — resubmitting a rejected
  order is the normal fix, noted rather than flagged. A BLOCK can be
  overridden for a legitimate revision with a WRITTEN reason
  (``DuplicateOverride``): the error downgrades to a warning that
  names the override, and ``override.to_record()`` is the audit
  payload the caller must store.

Date semantics: order dates are compared as trimmed strings of the
normalized values — same date strengthens the duplicate reading,
a different date is called out as a possible revision. No date is
parsed or guessed here.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.catalog_matching import normalize_identifier, normalize_text
from soa_db.catalog_validation import ValidationFinding
from soa_db.corrections import FieldCorrection
from soa_db.documents import Document, DocumentState
from soa_db.extracted_fields import ExtractedField
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun

#: Documents scanned per lookup before the search caps out (noted).
DEFAULT_SCAN_LIMIT = 5000


class DuplicatePoPolicy(StrEnum):
    #: Surface duplicates as warnings; a human decides (default).
    WARN = "warn"
    #: Duplicates block approval unless overridden with a reason.
    BLOCK = "block"
    #: Duplicates are recorded in the notes only.
    ALLOW = "allow"


DEFAULT_PO_POLICY = DuplicatePoPolicy.WARN


def get_po_duplicate_policy(stream_config: Mapping[str, object]) -> DuplicatePoPolicy:
    raw = stream_config.get("business_duplicate_policy")
    try:
        return DuplicatePoPolicy(str(raw))
    except ValueError:
        return DEFAULT_PO_POLICY


class DuplicateOverrideError(ValueError):
    pass


@dataclass(frozen=True)
class DuplicateOverride:
    """An authorized human's decision that this apparent duplicate is a
    legitimate revision. The reason is required and recorded."""

    actor_id: str
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise DuplicateOverrideError("overriding a duplicate block needs a written reason")

    def to_record(self) -> dict[str, Any]:
        """The audit payload the caller stores with the override."""
        return {
            "kind": "duplicate_po_override",
            "actor_id": self.actor_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PoDuplicateCandidate:
    document_id: uuid.UUID
    document_state: str
    received_at: datetime
    po_number: str
    #: True/False when the candidate's customer was compared; None when
    #: it could not be (missing on either side).
    customer_matched: bool | None
    #: Same for the order date.
    order_date_matched: bool | None


@dataclass(frozen=True)
class PoDuplicateSearch:
    candidates: tuple[PoDuplicateCandidate, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class PoDuplicateAssessment:
    findings: tuple[ValidationFinding, ...]
    notes: tuple[str, ...]


async def _effective_header_values(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    field_key: str,
    exclude_document_id: uuid.UUID,
    scan_limit: int,
    document_ids: set[uuid.UUID] | None = None,
) -> tuple[dict[uuid.UUID, str], bool]:
    """Per-document effective value of one header field across a
    stream: the latest correction wins, else the latest run's
    extraction. Returns (values, hit_scan_limit)."""
    extraction_stmt = (
        select(
            ExtractedField.document_id,
            ExtractedField.run_id,
            ExtractedField.raw_value,
        )
        .join(Document, Document.id == ExtractedField.document_id)
        .where(
            ExtractedField.organization_id == context.organization_id,
            Document.organization_id == context.organization_id,
            Document.stream_id == stream_id,
            Document.id != exclude_document_id,
            ExtractedField.field_key == field_key,
            ExtractedField.row_index.is_(None),
        )
        .limit(scan_limit + 1)
    )
    if document_ids is not None:
        extraction_stmt = extraction_stmt.where(Document.id.in_(document_ids))
    rows = list((await session.execute(extraction_stmt)).all())
    capped = len(rows) > scan_limit
    rows = rows[:scan_limit]

    #: Latest run wins when a document was reprocessed.
    run_ids = {row.run_id for row in rows}
    run_numbers: dict[uuid.UUID, int] = {}
    if run_ids:
        run_rows = await session.execute(
            select(ProcessingRun.id, ProcessingRun.run_number).where(
                ProcessingRun.organization_id == context.organization_id,
                ProcessingRun.id.in_(run_ids),
            )
        )
        run_numbers = {row.id: row.run_number for row in run_rows}
    best_run: dict[uuid.UUID, int] = {}
    values: dict[uuid.UUID, str] = {}
    for row in rows:
        if row.raw_value is None:
            continue
        run_number = run_numbers.get(row.run_id, 0)
        if row.document_id not in best_run or run_number >= best_run[row.document_id]:
            best_run[row.document_id] = run_number
            values[row.document_id] = row.raw_value

    correction_stmt = (
        select(FieldCorrection)
        .join(Document, Document.id == FieldCorrection.document_id)
        .where(
            FieldCorrection.organization_id == context.organization_id,
            Document.organization_id == context.organization_id,
            Document.stream_id == stream_id,
            Document.id != exclude_document_id,
            FieldCorrection.field_key == field_key,
            FieldCorrection.row_index.is_(None),
        )
        .order_by(FieldCorrection.created_at, FieldCorrection.id)
    )
    if document_ids is not None:
        correction_stmt = correction_stmt.where(Document.id.in_(document_ids))
    corrections = (await session.execute(correction_stmt)).scalars().all()
    for correction in corrections:  # chronological: the last one wins
        if correction.corrected_raw_value is None:
            values.pop(correction.document_id, None)
        else:
            values[correction.document_id] = correction.corrected_raw_value
    return values, capped


async def find_po_duplicates(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    exclude_document_id: uuid.UUID,
    po_number: str | None,
    customer: str | None = None,
    order_date: str | None = None,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> PoDuplicateSearch:
    """Business-duplicate candidates for one document. See module
    docstring for the comparison semantics."""
    notes: list[str] = []
    if po_number is None or not po_number.strip():
        return PoDuplicateSearch(
            candidates=(),
            notes=("the document has no PO number — the duplicate check was skipped",),
        )
    normalized_po = normalize_identifier(po_number)

    po_values, capped = await _effective_header_values(
        session,
        context,
        stream_id=stream_id,
        field_key="po_number",
        exclude_document_id=exclude_document_id,
        scan_limit=scan_limit,
    )
    if capped:
        notes.append(
            f"the duplicate scan is capped at {scan_limit} documents — older "
            "documents were not compared"
        )
    matching_ids = {
        document_id
        for document_id, value in po_values.items()
        if normalize_identifier(value) == normalized_po
    }
    if not matching_ids:
        return PoDuplicateSearch(candidates=(), notes=tuple(notes))

    customer_values, _ = await _effective_header_values(
        session,
        context,
        stream_id=stream_id,
        field_key="customer_name",
        exclude_document_id=exclude_document_id,
        scan_limit=scan_limit,
        document_ids=matching_ids,
    )
    date_values, _ = await _effective_header_values(
        session,
        context,
        stream_id=stream_id,
        field_key="order_date",
        exclude_document_id=exclude_document_id,
        scan_limit=scan_limit,
        document_ids=matching_ids,
    )
    documents = (
        (
            await session.execute(
                select(Document).where(
                    Document.organization_id == context.organization_id,
                    Document.id.in_(matching_ids),
                )
            )
        )
        .scalars()
        .all()
    )

    candidates: list[PoDuplicateCandidate] = []
    for document in sorted(documents, key=lambda d: (d.received_at, d.id)):
        candidate_customer = customer_values.get(document.id)
        if customer and candidate_customer:
            customer_matched: bool | None = normalize_text(customer) == normalize_text(
                candidate_customer
            )
            if not customer_matched:
                notes.append(
                    f"document {document.id} shares PO {po_number!r} but names a "
                    f"different customer ({candidate_customer!r}) — not a duplicate"
                )
                continue
        else:
            customer_matched = None
            notes.append(
                f"document {document.id} shares PO {po_number!r}; the customer could "
                "not be compared (missing on one side)"
            )
        candidate_date = date_values.get(document.id)
        order_date_matched = (
            order_date.strip() == candidate_date.strip() if order_date and candidate_date else None
        )
        candidates.append(
            PoDuplicateCandidate(
                document_id=document.id,
                document_state=document.state,
                received_at=document.received_at,
                po_number=po_number,
                customer_matched=customer_matched,
                order_date_matched=order_date_matched,
            )
        )
    return PoDuplicateSearch(candidates=tuple(candidates), notes=tuple(notes))


def _describe(candidate: PoDuplicateCandidate) -> str:
    date_part = {
        True: "same order date",
        False: "DIFFERENT order date — possibly a revision",
        None: "order date not comparable",
    }[candidate.order_date_matched]
    customer_part = "same customer" if candidate.customer_matched else "customer not compared"
    return (
        f"document {candidate.document_id} ({candidate.document_state}, received "
        f"{candidate.received_at.date().isoformat()}; {customer_part}; {date_part})"
    )


def assess_po_duplicates(
    candidates: tuple[PoDuplicateCandidate, ...] | list[PoDuplicateCandidate],
    *,
    policy: DuplicatePoPolicy = DEFAULT_PO_POLICY,
    override: DuplicateOverride | None = None,
) -> PoDuplicateAssessment:
    """Turn candidates into findings per the stream's policy."""
    findings: list[ValidationFinding] = []
    notes: list[str] = []

    rejected = [c for c in candidates if c.document_state == DocumentState.REJECTED.value]
    live = [c for c in candidates if c.document_state != DocumentState.REJECTED.value]
    for candidate in rejected:
        notes.append(
            f"{_describe(candidate)} was rejected — a resubmission is the normal fix, "
            "not a duplicate"
        )
    if not live:
        return PoDuplicateAssessment(findings=(), notes=tuple(notes))

    summary = "; ".join(_describe(candidate) for candidate in live)
    if policy is DuplicatePoPolicy.ALLOW:
        notes.append(f"duplicate PO recorded (policy allows): {summary}")
        return PoDuplicateAssessment(findings=(), notes=tuple(notes))

    if policy is DuplicatePoPolicy.BLOCK and override is None:
        findings.append(
            ValidationFinding(
                code="duplicate_po",
                severity="error",
                message=(
                    f"this PO number already exists in the stream: {summary}. The "
                    "stream blocks duplicates; a legitimate revision needs an "
                    "authorized override with a reason."
                ),
                field_key="po_number",
            )
        )
    elif policy is DuplicatePoPolicy.BLOCK:
        assert override is not None
        findings.append(
            ValidationFinding(
                code="duplicate_po_overridden",
                severity="warning",
                message=(
                    f"duplicate PO block overridden by {override.actor_id}: "
                    f"{override.reason!r}. Candidates: {summary}"
                ),
                field_key="po_number",
            )
        )
        notes.append("the override must be stored in the audit trail (to_record())")
    else:  # WARN
        findings.append(
            ValidationFinding(
                code="duplicate_po",
                severity="warning",
                message=f"this PO number already exists in the stream: {summary}",
                field_key="po_number",
            )
        )
    return PoDuplicateAssessment(findings=tuple(findings), notes=tuple(notes))


__all__ = [
    "DEFAULT_PO_POLICY",
    "DEFAULT_SCAN_LIMIT",
    "DuplicateOverride",
    "DuplicateOverrideError",
    "DuplicatePoPolicy",
    "PoDuplicateAssessment",
    "PoDuplicateCandidate",
    "PoDuplicateSearch",
    "assess_po_duplicates",
    "find_po_duplicates",
    "get_po_duplicate_policy",
]
