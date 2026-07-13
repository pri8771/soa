"""Duplicate detection (ING-006).

Exact duplicates are documents in the same stream with identical bytes
(same content SHA-256). Stream configuration chooses what happens via
``duplicate_policy``:

- ``reject``  — the duplicate is refused (state rejected, reason names
  the original document).
- ``flag``    — the duplicate proceeds, marked with ``duplicate_of``
  (default: a human decides in review).
- ``allow``   — the duplicate proceeds, still marked and audited.

Whatever the policy, a duplicate is NEVER silent: ``duplicate_of`` is
recorded and a ``document.duplicate_detected`` audit event is written,
so two deliveries of the same bytes cannot happen without a trace.

Business-level duplicates (same customer + PO number across different
files) can only be checked after extraction; ``BUSINESS_DUPLICATE_HOOK``
is the seam the PRC validation stage calls once extracted fields exist.

Concurrency: detection is check-then-act inside the completing
transaction. Two truly simultaneous completions of identical bytes on
separate connections can both miss each other under READ COMMITTED;
ING-007's atomic registration is the place for a database-level guard
(e.g. an advisory lock on the content hash) if pilot traffic shows the
race matters.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.audit import ActorType, record_audit_event
from soa_db.documents import Document, DocumentRepository
from soa_db.repository import OrganizationContext


class DuplicatePolicy(StrEnum):
    REJECT = "reject"
    FLAG = "flag"
    ALLOW = "allow"


DEFAULT_POLICY = DuplicatePolicy.FLAG


def get_duplicate_policy(stream_config: Mapping[str, object]) -> DuplicatePolicy:
    raw = stream_config.get("duplicate_policy")
    try:
        return DuplicatePolicy(str(raw))
    except ValueError:
        return DEFAULT_POLICY


async def find_exact_duplicate(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    content_sha256: str,
    exclude_document_id: uuid.UUID,
) -> Document | None:
    """Earliest document in the stream with the same bytes, if any."""
    candidates = await DocumentRepository(session, context).list_by_content_hash(content_sha256)
    for candidate in candidates:  # ordered by received_at
        if candidate.stream_id == stream_id and candidate.id != exclude_document_id:
            return candidate
    return None


async def mark_duplicate(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document: Document,
    original: Document,
    policy: DuplicatePolicy,
    actor_id: str,
) -> None:
    document.duplicate_of = original.id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="document.duplicate_detected",
        target_type="document",
        target_id=str(document.id),
        organization_id=context.organization_id,
        summary={
            "duplicate_of": str(original.id),
            "policy": policy.value,
            "content_sha256": document.content_sha256,
        },
    )


#: Seam for business-level duplicate checks (customer + PO number),
#: filled by CAT-013 below. Intake only handles exact content
#: duplicates; the validation stage calls the hook once extracted
#: fields exist.
BusinessDuplicateHook = Callable[
    [AsyncSession, OrganizationContext, uuid.UUID, Mapping[str, object]],
    Awaitable[list[Document]],
]


async def find_business_duplicate_documents(
    session: AsyncSession,
    context: OrganizationContext,
    document_id: uuid.UUID,
    extracted: Mapping[str, object],
) -> list[Document]:
    """The CAT-013 implementation of the business-duplicate seam: other
    documents in the same stream whose effective PO number (and, where
    comparable, customer) matches the given extracted header values."""
    from sqlalchemy import select

    from soa_db.duplicate_po import find_po_duplicates

    document = await DocumentRepository(session, context).get(document_id)
    if document is None:
        return []

    def text(key: str) -> str | None:
        value = extracted.get(key)
        return str(value) if value is not None and str(value).strip() else None

    search = await find_po_duplicates(
        session,
        context,
        stream_id=document.stream_id,
        exclude_document_id=document.id,
        po_number=text("po_number"),
        customer=text("customer_name"),
        order_date=text("order_date"),
    )
    if not search.candidates:
        return []
    order = {candidate.document_id: index for index, candidate in enumerate(search.candidates)}
    rows = (
        (
            await session.execute(
                select(Document).where(
                    Document.organization_id == context.organization_id,
                    Document.id.in_(order),
                )
            )
        )
        .scalars()
        .all()
    )
    return sorted(rows, key=lambda row: order[row.id])


BUSINESS_DUPLICATE_HOOK: BusinessDuplicateHook | None = find_business_duplicate_documents
