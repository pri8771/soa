"""Gold (evaluation) dataset storage (AIO-015).

A gold dataset is a tenant's curated ground truth: documents with the
values a correct extraction MUST produce. The evaluation runner
(AIO-016) scores candidate configurations against a PUBLISHED dataset
version, so versions follow the CFG discipline — drafts assemble
freely, published versions are immutable forever (a score is only
meaningful against a frozen dataset).

Tenancy and privacy:

- datasets are organization-scoped rows under FORCED RLS like every
  tenant table — one tenant's documents can never appear in another
  tenant's evaluation;
- every dataset carries a privacy classification. ``synthetic`` data
  has no customer origin; ``customer_confidential`` is the default for
  anything derived from real documents and stays strictly inside the
  tenant; ``customer_shared_by_agreement`` is the ONLY classification a
  future cross-tenant benchmark may draw from, and creating it REQUIRES
  the recorded agreement reference — no agreement, no sharing, fail
  closed;
- audit events record counts and identifiers, never ground-truth
  values (they are customer data).

Ground truth is a CLOSED shape per document: ``fields`` (header key →
expected value or null), optional ``lines`` (row dicts), optional
``validations`` (rule outcomes the document must trigger), plus the
evaluation ``split`` and optional expected document class.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow
from soa_db.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)

SPLITS = frozenset({"train", "validation", "test"})

#: Positional-hint bounds — regions travel inside the ground truth and, once
#: compiled to few-shot exemplars, inside every model request, so they stay
#: bounded like every other prompt-bound content.
MAX_GROUND_TRUTH_REGIONS = 500
MAX_REGION_POLYGON_VERTICES = 64


class PrivacyClassification(StrEnum):
    SYNTHETIC = "synthetic"
    CUSTOMER_CONFIDENTIAL = "customer_confidential"
    #: The ONLY classification cross-tenant use may ever draw from —
    #: and it requires the recorded agreement.
    CUSTOMER_SHARED_BY_AGREEMENT = "customer_shared_by_agreement"


class GoldDatasetError(ValueError):
    pass


def _validate_regions(regions: Any) -> None:
    """Optional positional hints: field key → the box on the page a labeller
    drew for it. Header fields key by their plain key; a line-item cell keys
    as ``lines.<row_index>.<column>``. Kept loosely coupled to ``fields``/
    ``lines`` (a region may point at a field a labeller left blank), but every
    box is shape- and bound-checked because it becomes prompt-bound content."""
    if not isinstance(regions, dict):
        raise GoldDatasetError("'regions' must map field keys to region objects")
    if len(regions) > MAX_GROUND_TRUTH_REGIONS:
        raise GoldDatasetError(f"'regions' exceeds {MAX_GROUND_TRUTH_REGIONS} entries")
    for key, region in regions.items():
        if not isinstance(key, str) or not key.strip():
            raise GoldDatasetError("region keys must be non-empty field identifiers")
        if not isinstance(region, dict):
            raise GoldDatasetError(f"regions[{key!r}] must be an object")
        page = region.get("page_number")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise GoldDatasetError(f"regions[{key!r}].page_number must be a 1-based integer")
        polygon = region.get("polygon")
        if not isinstance(polygon, list) or not (3 <= len(polygon) <= MAX_REGION_POLYGON_VERTICES):
            raise GoldDatasetError(
                f"regions[{key!r}].polygon must have 3..{MAX_REGION_POLYGON_VERTICES} points"
            )
        for point in polygon:
            if (
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in point)
            ):
                raise GoldDatasetError(f"regions[{key!r}].polygon points must be [x, y] numbers")
        unknown = set(region) - {"page_number", "polygon"}
        if unknown:
            raise GoldDatasetError(f"regions[{key!r}] has unknown keys: {sorted(unknown)}")


def validate_ground_truth(ground_truth: dict[str, Any]) -> None:
    fields = ground_truth.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise GoldDatasetError("ground truth needs a non-empty 'fields' object")
    for key, value in fields.items():
        if value is not None and not isinstance(value, str):
            raise GoldDatasetError(
                f"fields[{key!r}] must be the expected string value or null (absent)"
            )
    lines = ground_truth.get("lines", [])
    if not isinstance(lines, list):
        raise GoldDatasetError("'lines' must be a list of row objects")
    for index, row in enumerate(lines):
        if not isinstance(row, dict):
            raise GoldDatasetError(f"lines[{index}] must be an object")
        for key, value in row.items():
            if value is not None and not isinstance(value, str):
                raise GoldDatasetError(f"lines[{index}][{key!r}] must be a string or null")
    validations = ground_truth.get("validations", [])
    if not isinstance(validations, list) or not all(
        isinstance(item, str) and item.strip() for item in validations
    ):
        raise GoldDatasetError("'validations' must be a list of non-empty rule identifiers")
    regions = ground_truth.get("regions")
    if regions is not None:
        _validate_regions(regions)
    unknown = set(ground_truth) - {"fields", "lines", "validations", "regions"}
    if unknown:
        raise GoldDatasetError(
            f"unknown ground-truth keys: {sorted(unknown)} — the shape is closed"
        )


class GoldDataset(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "gold_datasets"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    privacy_classification: Mapped[str] = mapped_column(String(50), nullable=False)
    #: Reference to the explicit customer agreement permitting reuse;
    #: REQUIRED when the classification allows sharing.
    sharing_agreement_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The stream this dataset trains/evaluates. NULL = an org-level
    #: evaluation dataset unattached to any stream (the pre-training default);
    #: set = a per-stream "training set" (soft ref to streams.id, no FK).
    stream_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "slug"),
        Index("ix_gold_datasets_org_stream", "organization_id", "stream_id"),
    )


class GoldDatasetVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "gold_dataset_versions"

    dataset_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("dataset_id", "version_number"),
        Index(
            "uq_gold_dataset_versions_single_published",
            "dataset_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )


class GoldDocument(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "gold_documents"

    dataset_version_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    #: Identity of the source bytes — evaluation runs the SAME file.
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The tenant document it came from, when applicable.
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    split: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_class: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ground_truth: Mapped[dict[str, Any]] = mapped_column(
        PORTABLE_JSON, nullable=False, default=dict
    )

    __table_args__ = (UniqueConstraint("dataset_version_id", "document_sha256"),)


class GoldDatasetRepository(ScopedRepository[GoldDataset]):
    model = GoldDataset

    async def get_by_slug(self, slug: str) -> GoldDataset | None:
        stmt = self._scoped_select().where(GoldDataset.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_stream(self, stream_id: uuid.UUID) -> list[GoldDataset]:
        stmt = (
            self._scoped_select()
            .where(GoldDataset.stream_id == stream_id)
            .order_by(GoldDataset.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())


class GoldDatasetVersionRepository(ScopedRepository[GoldDatasetVersion]):
    model = GoldDatasetVersion

    async def list_for_dataset(self, dataset_id: uuid.UUID) -> list[GoldDatasetVersion]:
        stmt = (
            self._scoped_select()
            .where(GoldDatasetVersion.dataset_id == dataset_id)
            .order_by(GoldDatasetVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_published(self, dataset_id: uuid.UUID) -> GoldDatasetVersion | None:
        stmt = self._scoped_select().where(
            GoldDatasetVersion.dataset_id == dataset_id,
            GoldDatasetVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class GoldDocumentRepository(ScopedRepository[GoldDocument]):
    model = GoldDocument

    async def list_for_version(self, dataset_version_id: uuid.UUID) -> list[GoldDocument]:
        stmt = (
            self._scoped_select()
            .where(GoldDocument.dataset_version_id == dataset_version_id)
            .order_by(GoldDocument.document_sha256)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_for_sha256(
        self, dataset_version_id: uuid.UUID, document_sha256: str
    ) -> GoldDocument | None:
        stmt = self._scoped_select().where(
            GoldDocument.dataset_version_id == dataset_version_id,
            GoldDocument.document_sha256 == document_sha256,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


async def create_gold_dataset(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    name: str,
    slug: str,
    privacy_classification: PrivacyClassification,
    sharing_agreement_ref: str | None = None,
    description: str | None = None,
    stream_id: uuid.UUID | None = None,
    actor_id: str,
) -> GoldDataset:
    if (
        privacy_classification == PrivacyClassification.CUSTOMER_SHARED_BY_AGREEMENT
        and not (sharing_agreement_ref or "").strip()
    ):
        raise GoldDatasetError(
            "customer data can only be marked shareable with the explicit agreement "
            "recorded — pass sharing_agreement_ref, or keep it customer_confidential"
        )
    if (
        privacy_classification != PrivacyClassification.CUSTOMER_SHARED_BY_AGREEMENT
        and sharing_agreement_ref
    ):
        raise GoldDatasetError(
            "a sharing agreement only makes sense with the customer_shared_by_agreement "
            "classification"
        )
    dataset = GoldDatasetRepository(session, context).add(
        GoldDataset(
            name=name,
            slug=slug,
            description=description,
            privacy_classification=privacy_classification,
            sharing_agreement_ref=sharing_agreement_ref,
            stream_id=stream_id,
            created_by=actor_id,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="gold_dataset.created",
        target_type="gold_dataset",
        target_id=str(dataset.id),
        organization_id=context.organization_id,
        summary={
            "slug": slug,
            "privacy_classification": privacy_classification.value,
            "stream_id": str(stream_id) if stream_id else None,
        },
    )
    return dataset


async def create_dataset_version(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    dataset: GoldDataset,
    change_summary: str | None = None,
    actor_id: str,
) -> GoldDatasetVersion:
    versions = await GoldDatasetVersionRepository(session, context).list_for_dataset(dataset.id)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = GoldDatasetVersionRepository(session, context).add(
        GoldDatasetVersion(
            dataset_id=dataset.id, version_number=next_number, change_summary=change_summary
        )
    )
    await session.flush()
    return draft


async def add_gold_document(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    version: GoldDatasetVersion,
    document_sha256: str,
    split: str,
    ground_truth: dict[str, Any],
    expected_class: str | None = None,
    source_document_id: uuid.UUID | None = None,
    actor_id: str,
) -> GoldDocument:
    if version.state != VersionState.DRAFT:
        raise InvalidVersionStateError(
            f"documents can only be added to drafts; this version is {version.state!r}"
        )
    if split not in SPLITS:
        raise GoldDatasetError(f"split must be one of {sorted(SPLITS)}, not {split!r}")
    if len(document_sha256) != 64:
        raise GoldDatasetError("document_sha256 must be the 64-char hex digest")
    validate_ground_truth(ground_truth)
    existing = await GoldDocumentRepository(session, context).list_for_version(version.id)
    if any(doc.document_sha256 == document_sha256 for doc in existing):
        raise GoldDatasetError(
            f"document {document_sha256[:12]}… is already in this dataset version"
        )
    document = GoldDocumentRepository(session, context).add(
        GoldDocument(
            dataset_version_id=version.id,
            document_sha256=document_sha256,
            source_document_id=source_document_id,
            split=split,
            expected_class=expected_class,
            ground_truth=dict(ground_truth),
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="gold_dataset.document_added",
        target_type="gold_document",
        target_id=str(document.id),
        organization_id=context.organization_id,
        # Counts and identity — never the ground-truth values.
        summary={
            "dataset_version_id": str(version.id),
            "split": split,
            "fields": len(ground_truth.get("fields", {})),
            "lines": len(ground_truth.get("lines", []) or []),
        },
    )
    return document


async def upsert_gold_document(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    version: GoldDatasetVersion,
    document_sha256: str,
    split: str,
    ground_truth: dict[str, Any],
    expected_class: str | None = None,
    source_document_id: uuid.UUID | None = None,
    actor_id: str,
) -> GoldDocument:
    """Create or REPLACE the labelled document for a sample in a draft version.

    Annotation is incremental — a labeller saves the same sample repeatedly as
    they draw and assign more fields — so this replaces the row's ground truth
    (and split/class/source) rather than failing on the ``(version, sha256)``
    uniqueness the way :func:`add_gold_document` does. Draft-only; published
    versions are frozen forever."""
    if version.state != VersionState.DRAFT:
        raise InvalidVersionStateError(
            f"documents can only be edited on drafts; this version is {version.state!r}"
        )
    if split not in SPLITS:
        raise GoldDatasetError(f"split must be one of {sorted(SPLITS)}, not {split!r}")
    if len(document_sha256) != 64:
        raise GoldDatasetError("document_sha256 must be the 64-char hex digest")
    validate_ground_truth(ground_truth)
    repo = GoldDocumentRepository(session, context)
    existing = await repo.get_for_sha256(version.id, document_sha256)
    if existing is None:
        document = repo.add(
            GoldDocument(
                dataset_version_id=version.id,
                document_sha256=document_sha256,
                source_document_id=source_document_id,
                split=split,
                expected_class=expected_class,
                ground_truth=dict(ground_truth),
            )
        )
        action = "gold_dataset.document_added"
    else:
        existing.split = split
        existing.expected_class = expected_class
        if source_document_id is not None:
            existing.source_document_id = source_document_id
        existing.ground_truth = dict(ground_truth)
        document = existing
        action = "gold_dataset.document_updated"
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action=action,
        target_type="gold_document",
        target_id=str(document.id),
        organization_id=context.organization_id,
        # Counts and identity — never the ground-truth values.
        summary={
            "dataset_version_id": str(version.id),
            "split": split,
            "fields": len(ground_truth.get("fields", {})),
            "lines": len(ground_truth.get("lines", []) or []),
            "regions": len(ground_truth.get("regions", {}) or {}),
        },
    )
    return document


async def publish_dataset_version(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    dataset: GoldDataset,
    version: GoldDatasetVersion,
    actor_id: str,
    now: datetime | None = None,
) -> GoldDatasetVersion:
    if version.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts publish; this version is {version.state!r}")
    if version.dataset_id != dataset.id:
        raise InvalidVersionStateError("version belongs to a different dataset")
    documents = await GoldDocumentRepository(session, context).list_for_version(version.id)
    if not documents:
        raise GoldDatasetError("an empty dataset version cannot publish — nothing to score")
    previous = await GoldDatasetVersionRepository(session, context).get_published(dataset.id)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
        await session.flush()
    version.state = VersionState.PUBLISHED
    version.published_at = now or utcnow()
    version.published_by = actor_id
    await session.flush()
    splits = {split: sum(1 for doc in documents if doc.split == split) for split in sorted(SPLITS)}
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="gold_dataset.version_published",
        target_type="gold_dataset_version",
        target_id=str(version.id),
        organization_id=context.organization_id,
        summary={
            "dataset_id": str(dataset.id),
            "version_number": version.version_number,
            "documents": len(documents),
            "splits": splits,
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return version


async def update_gold_dataset(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    dataset: GoldDataset,
    name: str | None = None,
    description: str | None = None,
    actor_id: str,
) -> GoldDataset:
    """Rename/redescribe a training set. Identity (slug, stream, privacy) is
    immutable — those shape what has already been scored against it."""
    if name is not None:
        dataset.name = name
    if description is not None:
        dataset.description = description
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="gold_dataset.updated",
        target_type="gold_dataset",
        target_id=str(dataset.id),
        organization_id=context.organization_id,
        summary={"slug": dataset.slug},
    )
    return dataset


async def delete_gold_document(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    version: GoldDatasetVersion,
    document: GoldDocument,
    actor_id: str,
) -> None:
    """Remove a labelled sample from a draft version."""
    if version.state != VersionState.DRAFT:
        raise InvalidVersionStateError(
            f"documents can only be removed from drafts; this version is {version.state!r}"
        )
    await session.delete(document)
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="gold_dataset.document_removed",
        target_type="gold_document",
        target_id=str(document.id),
        organization_id=context.organization_id,
        summary={"dataset_version_id": str(version.id)},
    )


async def delete_gold_dataset(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    dataset: GoldDataset,
    actor_id: str,
) -> None:
    """Delete a training set and all of its draft content.

    Refused once anything has published: a published version is immutable
    evaluation evidence that promotion decisions may already reference. Callers
    should stop using such a set rather than delete its provenance."""
    version_repo = GoldDatasetVersionRepository(session, context)
    versions = await version_repo.list_for_dataset(dataset.id)
    if any(v.state != VersionState.DRAFT for v in versions):
        raise GoldDatasetError(
            "a training set with a published version cannot be deleted — its scored "
            "history is immutable evidence"
        )
    document_repo = GoldDocumentRepository(session, context)
    for version in versions:
        for document in await document_repo.list_for_version(version.id):
            await session.delete(document)
        await session.delete(version)
    await session.delete(dataset)
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="gold_dataset.deleted",
        target_type="gold_dataset",
        target_id=str(dataset.id),
        organization_id=context.organization_id,
        summary={"slug": dataset.slug, "versions": len(versions)},
    )


async def published_dataset_version(
    session: AsyncSession, context: OrganizationContext, *, dataset_id: uuid.UUID
) -> GoldDatasetVersion | None:
    return await GoldDatasetVersionRepository(session, context).get_published(dataset_id)


__all__ = [
    "MAX_GROUND_TRUTH_REGIONS",
    "MAX_REGION_POLYGON_VERTICES",
    "SPLITS",
    "GoldDataset",
    "GoldDatasetError",
    "GoldDatasetRepository",
    "GoldDatasetVersion",
    "GoldDatasetVersionRepository",
    "GoldDocument",
    "GoldDocumentRepository",
    "PrivacyClassification",
    "add_gold_document",
    "create_dataset_version",
    "create_gold_dataset",
    "delete_gold_dataset",
    "delete_gold_document",
    "publish_dataset_version",
    "published_dataset_version",
    "update_gold_dataset",
    "upsert_gold_document",
    "validate_ground_truth",
]
