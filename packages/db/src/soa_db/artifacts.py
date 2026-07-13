"""Artifact metadata (STO-003).

An artifact is one immutable stored object tied to a document: the
original upload, a page image, extracted text, normalized output, an
export payload. Rows are APPEND-ONLY — once written, an artifact's
identity (key, hash, size, producer) can never change through the ORM;
only the retention class may be updated, because retention policy is an
operational decision about an object, not part of what the object is.
Replacing content means writing a NEW artifact under a NEW key.

Shared between the API (reads, signed downloads) and the worker
(producers), like jobs.
"""

import re
import uuid
from enum import StrEnum

from sqlalchemy import BigInteger, String, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ArtifactKind(StrEnum):
    ORIGINAL = "original"
    PAGE_IMAGE = "page_image"
    OCR_TEXT = "ocr_text"
    EXTRACTION = "extraction"
    NORMALIZED = "normalized"
    EXPORT_PAYLOAD = "export_payload"


class RetentionClass(StrEnum):
    STANDARD = "standard"  # deleted per the organization's retention policy
    EXTENDED = "extended"  # kept longer, e.g. dispute evidence
    LEGAL_HOLD = "legal_hold"  # never auto-deleted while the hold stands


class ArtifactImmutableError(Exception):
    """An attempt to modify an artifact's identity after creation."""

    def __init__(self, artifact_id: object, fields: list[str]) -> None:
        super().__init__(
            f"artifact {artifact_id} is immutable; refused update of {', '.join(fields)} — "
            "write a new artifact instead"
        )


#: The only column that may change after insert.
_MUTABLE_FIELDS = frozenset({"retention_class", "updated_at"})


class Artifact(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "artifacts"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    # The object key is globally unique: two artifacts can never claim the
    # same stored object, so an artifact row can't be "overwritten" by
    # re-pointing another row at its bytes.
    object_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # Producer provenance. Run/stage identifiers become foreign concepts
    # when PRC lands; until then they are recorded verbatim.
    produced_by_run_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    produced_by_stage: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retention_class: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RetentionClass.STANDARD.value
    )


@event.listens_for(Session, "before_flush")
def _refuse_artifact_mutation(session: Session, _ctx: object, _instances: object) -> None:
    for entity in session.dirty:
        if not isinstance(entity, Artifact) or not session.is_modified(entity):
            continue
        from sqlalchemy import inspect as sa_inspect

        state = sa_inspect(entity)
        changed = [
            attr.key
            for attr in state.attrs
            if attr.history.has_changes() and attr.key not in _MUTABLE_FIELDS
        ]
        if changed:
            raise ArtifactImmutableError(state.identity[0] if state.identity else "?", changed)


class ArtifactRepository(ScopedRepository[Artifact]):
    model = Artifact

    async def list_for_document(self, document_id: uuid.UUID) -> list[Artifact]:
        stmt = (
            self._scoped_select()
            .where(Artifact.document_id == document_id)
            .order_by(Artifact.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())


async def export_manifest(session: AsyncSession, context: OrganizationContext) -> dict[str, str]:
    """Object key -> SHA-256 for every artifact in the organization — the
    expected side of backup/migration reconciliation (STO-005)."""
    repo = ArtifactRepository(session, context)
    stmt = repo._scoped_select().order_by(Artifact.object_key)
    rows = (await session.execute(stmt)).scalars().all()
    return {row.object_key: row.sha256 for row in rows}


async def create_artifact(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    kind: ArtifactKind,
    object_key: str,
    sha256: str,
    size_bytes: int,
    content_type: str,
    produced_by_run_id: uuid.UUID | None = None,
    produced_by_stage: str | None = None,
    retention_class: RetentionClass = RetentionClass.STANDARD,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str = "worker",
) -> Artifact:
    if not _SHA256_HEX.fullmatch(sha256):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    if size_bytes < 0:
        raise ValueError("size_bytes must be non-negative")
    artifact = ArtifactRepository(session, context).add(
        Artifact(
            document_id=document_id,
            kind=kind.value,
            object_key=object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            produced_by_run_id=produced_by_run_id,
            produced_by_stage=produced_by_stage,
            retention_class=retention_class.value,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="artifact.created",
        target_type="artifact",
        target_id=str(artifact.id),
        organization_id=context.organization_id,
        summary={
            "document_id": str(document_id),
            "kind": kind.value,
            "sha256": sha256,
            "size_bytes": size_bytes,
        },
    )
    return artifact
