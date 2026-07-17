"""Extraction-training API — the labelling front door into gold datasets.

A *training set* is a gold dataset scoped to one stream (``gold_datasets``
gained a nullable ``stream_id``). Its labelled sample documents serve two
purposes at once: they become few-shot exemplars for that stream's extraction
(Phase 2) and the held-out slice the evaluation runner scores (Phase 3). This
router is the previously-missing production write path — the gold-store service
functions existed but nothing outside tests created rows.

Scope discipline mirrors the existing versioned-config subsystems: a training
set has a single working DRAFT version that accepts labelled documents; once
PUBLISHED it is immutable forever (a score is only meaningful against a frozen
set). Continue labelling by starting a new draft, which clones the last
version's documents so the labeller iterates from where they left off.

Authorization reuses ``streams.manage`` (write) / ``streams.read`` (read) — the
same permissions the sibling evaluations router uses — so no new RBAC registry
entry or per-org backfill is required.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.streams import Stream, StreamRepository
from soa_db.documents import Document, DocumentRepository
from soa_db.gold_datasets import (
    SPLITS,
    GoldDataset,
    GoldDatasetError,
    GoldDatasetRepository,
    GoldDatasetVersion,
    GoldDatasetVersionRepository,
    GoldDocument,
    GoldDocumentRepository,
    PrivacyClassification,
    create_dataset_version,
    create_gold_dataset,
    delete_gold_dataset,
    delete_gold_document,
    publish_dataset_version,
    update_gold_dataset,
    upsert_gold_document,
)
from soa_db.versioning import InvalidVersionStateError, VersionState

router = APIRouter(tags=["training"])

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


# --------------------------------------------------------------------------- #
# Request / response shapes
# --------------------------------------------------------------------------- #


class CreateTrainingSetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=100, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=1000)


class UpdateTrainingSetRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class UpsertDocumentRequest(BaseModel):
    source_document_id: uuid.UUID
    split: str = Field(default="train")
    expected_class: str | None = Field(default=None, max_length=100)
    #: The closed ground-truth shape: {fields, lines?, validations?, regions?}.
    ground_truth: dict[str, Any]


class GoldDocumentResponse(BaseModel):
    id: uuid.UUID
    dataset_version_id: uuid.UUID
    source_document_id: uuid.UUID | None
    document_sha256: str
    split: str
    expected_class: str | None
    ground_truth: dict[str, Any]

    @classmethod
    def from_model(cls, record: GoldDocument) -> "GoldDocumentResponse":
        return cls(
            id=record.id,
            dataset_version_id=record.dataset_version_id,
            source_document_id=record.source_document_id,
            document_sha256=record.document_sha256,
            split=record.split,
            expected_class=record.expected_class,
            ground_truth=record.ground_truth,
        )


class VersionSummary(BaseModel):
    id: uuid.UUID
    version_number: int
    state: str
    published_at: str | None
    counts: dict[str, int]


class TrainingSetSummary(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    stream_id: uuid.UUID | None
    privacy_classification: str
    working_draft_version_id: uuid.UUID | None
    published_version_id: uuid.UUID | None
    document_count: int


class TrainingSetDetail(TrainingSetSummary):
    versions: list[VersionSummary]
    #: Documents of the working draft (else the latest version) — what the
    #: annotation UI renders and edits.
    documents: list[GoldDocumentResponse]


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


def _split_counts(documents: list[GoldDocument]) -> dict[str, int]:
    counts = {split: 0 for split in sorted(SPLITS)}
    for document in documents:
        counts[document.split] = counts.get(document.split, 0) + 1
    counts["total"] = len(documents)
    return counts


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #


async def _load_stream(
    session: DbSession, authorized: AuthorizedContext, stream_slug: str
) -> Stream:
    stream = await StreamRepository(session, authorized.org_context).get_by_slug(stream_slug)
    if stream is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stream not found.")
    return stream


async def _load_training_set(
    session: DbSession, authorized: AuthorizedContext, stream: Stream, ts_slug: str
) -> GoldDataset:
    dataset = await GoldDatasetRepository(session, authorized.org_context).get_by_slug(ts_slug)
    if dataset is None or dataset.stream_id != stream.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Training set not found.")
    return dataset


async def _versions(
    session: DbSession, authorized: AuthorizedContext, dataset: GoldDataset
) -> list[GoldDatasetVersion]:
    return await GoldDatasetVersionRepository(session, authorized.org_context).list_for_dataset(
        dataset.id
    )


def _working_draft(versions: list[GoldDatasetVersion]) -> GoldDatasetVersion | None:
    """The single editable draft is always the highest-numbered version when
    it is a draft (publish freezes it, so a later draft is created above)."""
    if versions and versions[-1].state == VersionState.DRAFT:
        return versions[-1]
    return None


def _published(versions: list[GoldDatasetVersion]) -> GoldDatasetVersion | None:
    for version in versions:
        if version.state == VersionState.PUBLISHED:
            return version
    return None


async def _summary(
    session: DbSession, authorized: AuthorizedContext, dataset: GoldDataset
) -> tuple[TrainingSetSummary, list[GoldDatasetVersion], GoldDatasetVersion | None]:
    versions = await _versions(session, authorized, dataset)
    draft = _working_draft(versions)
    published = _published(versions)
    reference = draft or versions[-1] if versions else None
    document_repo = GoldDocumentRepository(session, authorized.org_context)
    document_count = 0
    if reference is not None:
        document_count = len(await document_repo.list_for_version(reference.id))
    summary = TrainingSetSummary(
        id=dataset.id,
        slug=dataset.slug,
        name=dataset.name,
        description=dataset.description,
        stream_id=dataset.stream_id,
        privacy_classification=dataset.privacy_classification,
        working_draft_version_id=draft.id if draft else None,
        published_version_id=published.id if published else None,
        document_count=document_count,
    )
    return summary, versions, reference


# --------------------------------------------------------------------------- #
# Training-set CRUD
# --------------------------------------------------------------------------- #


@router.post(
    "/orgs/{organization_slug}/streams/{stream_slug}/training-sets",
    status_code=status.HTTP_201_CREATED,
)
async def create_training_set(
    stream_slug: str,
    body: CreateTrainingSetRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> TrainingSetSummary:
    stream = await _load_stream(session, authorized, stream_slug)
    existing = await GoldDatasetRepository(session, authorized.org_context).get_by_slug(body.slug)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A dataset with this slug already exists in the org."
        )
    try:
        dataset = await create_gold_dataset(
            session,
            authorized.org_context,
            name=body.name,
            slug=body.slug,
            # Real customer documents by default; stays strictly inside the tenant.
            privacy_classification=PrivacyClassification.CUSTOMER_CONFIDENTIAL,
            description=body.description,
            stream_id=stream.id,
            actor_id=_actor(authorized),
        )
        await create_dataset_version(
            session,
            authorized.org_context,
            dataset=dataset,
            change_summary="Initial draft",
            actor_id=_actor(authorized),
        )
    except GoldDatasetError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    summary, _, _ = await _summary(session, authorized, dataset)
    return summary


@router.get("/orgs/{organization_slug}/streams/{stream_slug}/training-sets")
async def list_training_sets(
    stream_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, list[TrainingSetSummary]]:
    stream = await _load_stream(session, authorized, stream_slug)
    datasets = await GoldDatasetRepository(session, authorized.org_context).list_for_stream(
        stream.id
    )
    items = [(await _summary(session, authorized, dataset))[0] for dataset in datasets]
    return {"items": items}


@router.get("/orgs/{organization_slug}/streams/{stream_slug}/training-sets/{ts_slug}")
async def get_training_set(
    stream_slug: str,
    ts_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> TrainingSetDetail:
    stream = await _load_stream(session, authorized, stream_slug)
    dataset = await _load_training_set(session, authorized, stream, ts_slug)
    summary, versions, reference = await _summary(session, authorized, dataset)
    document_repo = GoldDocumentRepository(session, authorized.org_context)
    version_summaries: list[VersionSummary] = []
    documents: list[GoldDocumentResponse] = []
    for version in versions:
        docs = await document_repo.list_for_version(version.id)
        version_summaries.append(
            VersionSummary(
                id=version.id,
                version_number=version.version_number,
                state=version.state,
                published_at=version.published_at.isoformat() if version.published_at else None,
                counts=_split_counts(docs),
            )
        )
        if reference is not None and version.id == reference.id:
            documents = [GoldDocumentResponse.from_model(doc) for doc in docs]
    return TrainingSetDetail(
        **summary.model_dump(), versions=version_summaries, documents=documents
    )


@router.patch("/orgs/{organization_slug}/streams/{stream_slug}/training-sets/{ts_slug}")
async def update_training_set(
    stream_slug: str,
    ts_slug: str,
    body: UpdateTrainingSetRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> TrainingSetSummary:
    stream = await _load_stream(session, authorized, stream_slug)
    dataset = await _load_training_set(session, authorized, stream, ts_slug)
    await update_gold_dataset(
        session,
        authorized.org_context,
        dataset=dataset,
        name=body.name,
        description=body.description,
        actor_id=_actor(authorized),
    )
    summary, _, _ = await _summary(session, authorized, dataset)
    return summary


@router.delete(
    "/orgs/{organization_slug}/streams/{stream_slug}/training-sets/{ts_slug}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_training_set(
    stream_slug: str,
    ts_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> None:
    stream = await _load_stream(session, authorized, stream_slug)
    dataset = await _load_training_set(session, authorized, stream, ts_slug)
    try:
        await delete_gold_dataset(
            session, authorized.org_context, dataset=dataset, actor_id=_actor(authorized)
        )
    except GoldDatasetError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None


# --------------------------------------------------------------------------- #
# Version lifecycle
# --------------------------------------------------------------------------- #


@router.post(
    "/orgs/{organization_slug}/streams/{stream_slug}/training-sets/{ts_slug}/versions",
    status_code=status.HTTP_201_CREATED,
)
async def start_new_draft(
    stream_slug: str,
    ts_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> VersionSummary:
    """Open a fresh draft to keep labelling after a publish. Clones the latest
    version's labelled documents so iteration continues from the last state."""
    stream = await _load_stream(session, authorized, stream_slug)
    dataset = await _load_training_set(session, authorized, stream, ts_slug)
    versions = await _versions(session, authorized, dataset)
    if _working_draft(versions) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A draft version is already open; publish or edit it."
        )
    document_repo = GoldDocumentRepository(session, authorized.org_context)
    source = versions[-1] if versions else None
    draft = await create_dataset_version(
        session,
        authorized.org_context,
        dataset=dataset,
        change_summary="New draft",
        actor_id=_actor(authorized),
    )
    if source is not None:
        for doc in await document_repo.list_for_version(source.id):
            await upsert_gold_document(
                session,
                authorized.org_context,
                version=draft,
                document_sha256=doc.document_sha256,
                split=doc.split,
                ground_truth=doc.ground_truth,
                expected_class=doc.expected_class,
                source_document_id=doc.source_document_id,
                actor_id=_actor(authorized),
            )
    docs = await document_repo.list_for_version(draft.id)
    return VersionSummary(
        id=draft.id,
        version_number=draft.version_number,
        state=draft.state,
        published_at=None,
        counts=_split_counts(docs),
    )


@router.post("/orgs/{organization_slug}/streams/{stream_slug}/training-sets/{ts_slug}/publish")
async def publish_training_set(
    stream_slug: str,
    ts_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> VersionSummary:
    stream = await _load_stream(session, authorized, stream_slug)
    dataset = await _load_training_set(session, authorized, stream, ts_slug)
    versions = await _versions(session, authorized, dataset)
    draft = _working_draft(versions)
    if draft is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no draft version to publish.")
    try:
        published = await publish_dataset_version(
            session,
            authorized.org_context,
            dataset=dataset,
            version=draft,
            actor_id=_actor(authorized),
        )
    except GoldDatasetError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    except InvalidVersionStateError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    docs = await GoldDocumentRepository(session, authorized.org_context).list_for_version(
        published.id
    )
    return VersionSummary(
        id=published.id,
        version_number=published.version_number,
        state=published.state,
        published_at=published.published_at.isoformat() if published.published_at else None,
        counts=_split_counts(docs),
    )


# --------------------------------------------------------------------------- #
# Labelled documents (annotation persistence)
# --------------------------------------------------------------------------- #


async def _require_draft(
    session: DbSession, authorized: AuthorizedContext, dataset: GoldDataset
) -> GoldDatasetVersion:
    versions = await _versions(session, authorized, dataset)
    draft = _working_draft(versions)
    if draft is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This training set has no open draft; start a new draft to label documents.",
        )
    return draft


async def _load_sample_document(
    session: DbSession, authorized: AuthorizedContext, stream: Stream, document_id: uuid.UUID
) -> Document:
    document = await DocumentRepository(session, authorized.org_context).get(document_id)
    if document is None or document.stream_id != stream.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sample document not found in this stream.")
    return document


@router.get("/orgs/{organization_slug}/streams/{stream_slug}/training-sets/{ts_slug}/documents")
async def list_training_documents(
    stream_slug: str,
    ts_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, list[GoldDocumentResponse]]:
    stream = await _load_stream(session, authorized, stream_slug)
    dataset = await _load_training_set(session, authorized, stream, ts_slug)
    _, _, reference = await _summary(session, authorized, dataset)
    if reference is None:
        return {"items": []}
    docs = await GoldDocumentRepository(session, authorized.org_context).list_for_version(
        reference.id
    )
    return {"items": [GoldDocumentResponse.from_model(doc) for doc in docs]}


@router.put("/orgs/{organization_slug}/streams/{stream_slug}/training-sets/{ts_slug}/documents")
async def upsert_training_document(
    stream_slug: str,
    ts_slug: str,
    body: UpsertDocumentRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> GoldDocumentResponse:
    """Save (create or replace) the labels a user drew for one sample. The
    client sends the full ground truth each time — annotation is incremental,
    so this is an idempotent upsert keyed on the sample's content hash."""
    stream = await _load_stream(session, authorized, stream_slug)
    dataset = await _load_training_set(session, authorized, stream, ts_slug)
    draft = await _require_draft(session, authorized, dataset)
    document = await _load_sample_document(session, authorized, stream, body.source_document_id)
    try:
        gold = await upsert_gold_document(
            session,
            authorized.org_context,
            version=draft,
            document_sha256=document.content_sha256,
            split=body.split,
            ground_truth=body.ground_truth,
            expected_class=body.expected_class,
            source_document_id=document.id,
            actor_id=_actor(authorized),
        )
    except GoldDatasetError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except InvalidVersionStateError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    return GoldDocumentResponse.from_model(gold)


@router.delete(
    "/orgs/{organization_slug}/streams/{stream_slug}/training-sets/{ts_slug}/documents/{gold_document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_training_document(
    stream_slug: str,
    ts_slug: str,
    gold_document_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> None:
    stream = await _load_stream(session, authorized, stream_slug)
    dataset = await _load_training_set(session, authorized, stream, ts_slug)
    draft = await _require_draft(session, authorized, dataset)
    document = await GoldDocumentRepository(session, authorized.org_context).get(gold_document_id)
    if document is None or document.dataset_version_id != draft.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Labelled document not found in the draft.")
    try:
        await delete_gold_document(
            session,
            authorized.org_context,
            version=draft,
            document=document,
            actor_id=_actor(authorized),
        )
    except InvalidVersionStateError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
