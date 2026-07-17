"""Classifier API — routing tables on intake streams + manual routing.

An intake stream (bucket) with a published classifier routes arriving
documents to their skill on the document's own text. This router manages the
versioned routing table (draft → publish, immutable once published) and
closes the loop for the documents the classifier refused to guess on: the
unrouted queue and the manual route action, whose every decision is audited
with the acting user — the raw material for training the next classifier
version.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.streams import Stream, StreamRepository
from soa_api.services.runtime_pins import RuntimePinError, resolve_runtime_pins
from soa_db.audit import ActorType, record_audit_event
from soa_db.classifiers import (
    ClassifierValidationError,
    ClassifierVersion,
    ClassifierVersionRepository,
    create_classifier_draft,
    publish_classifier_draft,
    update_classifier_draft,
)
from soa_db.documents import (
    Document,
    DocumentRepository,
    DocumentState,
    InvalidDocumentTransitionError,
    transition_document,
)
from soa_db.jobs import enqueue_job
from soa_db.runs import ProcessingRunRepository
from soa_db.versioning import InvalidVersionStateError

router = APIRouter(tags=["classifiers"])

#: The classify stage marks documents it refused to guess on with this
#: substring in state_reason (state is failed_terminal until a human routes).
UNROUTED_MARKER = "unrouted:"


class ClassifierContent(BaseModel):
    routes: list[dict[str, Any]]


class CreateClassifierRequest(BaseModel):
    content: ClassifierContent
    change_summary: str | None = Field(default=None, max_length=500)


class ClassifierVersionResponse(BaseModel):
    id: uuid.UUID
    stream_id: uuid.UUID
    version_number: int
    state: str
    reference: str
    content: dict[str, Any]
    change_summary: str | None
    published_at: str | None
    published_by: str | None

    @classmethod
    def from_model(cls, record: ClassifierVersion) -> "ClassifierVersionResponse":
        return cls(
            id=record.id,
            stream_id=record.stream_id,
            version_number=record.version_number,
            state=record.state,
            reference=record.reference,
            content=record.content,
            change_summary=record.change_summary,
            published_at=record.published_at.isoformat() if record.published_at else None,
            published_by=record.published_by,
        )


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


async def _load_stream(
    session: DbSession, authorized: AuthorizedContext, stream_slug: str
) -> Stream:
    stream = await StreamRepository(session, authorized.org_context).get_by_slug(stream_slug)
    if stream is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stream not found.")
    return stream


async def _validate_targets(
    session: DbSession, authorized: AuthorizedContext, content: dict[str, Any]
) -> None:
    """Every route target must be a real, non-archived stream in this org."""
    repo = StreamRepository(session, authorized.org_context)
    for route in content.get("routes", []) or []:
        raw = route.get("target_stream_id")
        try:
            target_id = uuid.UUID(str(raw))
        except (TypeError, ValueError):
            continue  # shape errors surface via validate_classifier_content
        target = await repo.get(target_id)
        if target is None or target.status == "archived":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Route {route.get('label')!r} targets a stream that does not exist "
                "or is archived.",
            )


@router.get("/orgs/{organization_slug}/streams/{stream_slug}/classifier")
async def list_classifier_versions(
    stream_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, Any]:
    stream = await _load_stream(session, authorized, stream_slug)
    versions = await ClassifierVersionRepository(session, authorized.org_context).list_for_stream(
        stream.id
    )
    return {"items": [ClassifierVersionResponse.from_model(v) for v in versions]}


@router.post(
    "/orgs/{organization_slug}/streams/{stream_slug}/classifier",
    status_code=status.HTTP_201_CREATED,
)
async def create_classifier_endpoint(
    stream_slug: str,
    body: CreateClassifierRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> ClassifierVersionResponse:
    stream = await _load_stream(session, authorized, stream_slug)
    await _validate_targets(session, authorized, body.content.model_dump())
    try:
        draft = await create_classifier_draft(
            session,
            authorized.org_context,
            stream_id=stream.id,
            content=body.content.model_dump(),
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
        )
    except ClassifierValidationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    return ClassifierVersionResponse.from_model(draft)


@router.patch("/orgs/{organization_slug}/classifier-versions/{version_id}")
async def update_classifier_endpoint(
    version_id: uuid.UUID,
    body: CreateClassifierRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> ClassifierVersionResponse:
    record = await ClassifierVersionRepository(session, authorized.org_context).get(version_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Classifier version not found.")
    await _validate_targets(session, authorized, body.content.model_dump())
    try:
        updated = await update_classifier_draft(
            session,
            authorized.org_context,
            draft=record,
            content=body.content.model_dump(),
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
        )
    except ClassifierValidationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except InvalidVersionStateError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    return ClassifierVersionResponse.from_model(updated)


@router.post("/orgs/{organization_slug}/classifier-versions/{version_id}/publish")
async def publish_classifier_endpoint(
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> ClassifierVersionResponse:
    record = await ClassifierVersionRepository(session, authorized.org_context).get(version_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Classifier version not found.")
    try:
        published = await publish_classifier_draft(
            session, authorized.org_context, draft=record, actor_id=_actor(authorized)
        )
    except (ClassifierValidationError, InvalidVersionStateError) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    return ClassifierVersionResponse.from_model(published)


# --------------------------------------------------------------------------- #
# Unrouted queue + manual routing
# --------------------------------------------------------------------------- #


@router.get("/orgs/{organization_slug}/routing/unrouted")
async def list_unrouted_documents(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    session: DbSession,
) -> dict[str, Any]:
    """Documents the classifier refused to guess on, oldest first."""
    rows = (
        (
            await session.execute(
                select(Document)
                .where(
                    Document.organization_id == authorized.org_context.organization_id,
                    Document.state == DocumentState.FAILED_TERMINAL.value,
                    Document.state_reason.contains(UNROUTED_MARKER),
                )
                .order_by(Document.received_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(document.id),
                "stream_id": str(document.stream_id),
                "original_filename": document.original_filename,
                "received_at": document.received_at.isoformat(),
                "state_reason": document.state_reason,
            }
            for document in rows
        ]
    }


class RouteRequest(BaseModel):
    stream_slug: str = Field(min_length=2, max_length=100)


@router.post("/orgs/{organization_slug}/documents/{document_id}/route")
async def route_document(
    document_id: uuid.UUID,
    body: RouteRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.reprocess"))],
    session: DbSession,
) -> dict[str, Any]:
    """A human routes an unrouted document to its skill.

    The decision re-queues the document under the target stream's currently
    published configuration and is audited with the acting user — the labelled
    example a future classifier version trains on."""
    context = authorized.org_context
    document = await DocumentRepository(session, context).get(document_id, for_update=True)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    if document.state != DocumentState.FAILED_TERMINAL.value or not (
        document.state_reason and UNROUTED_MARKER in document.state_reason
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only unrouted documents can be routed manually."
        )
    target = await StreamRepository(session, context).get_by_slug(body.stream_slug)
    if target is None or target.status == "archived":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target stream not found.")
    try:
        pins = await resolve_runtime_pins(
            session, context, stream_version_id=target.active_version_id
        )
    except RuntimePinError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None

    source_stream_id = document.stream_id
    document.stream_id = target.id
    try:
        await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.QUEUED,
            reason=f"routed manually to {target.slug}",
            actor_id=_actor(authorized),
            actor_type=ActorType.USER,
        )
    except InvalidDocumentTransitionError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None

    runs = await ProcessingRunRepository(session, context).list_for_document(document.id)
    await enqueue_job(
        session,
        job_type="document.preprocess",
        payload={
            "document_id": str(document.id),
            "stream_id": str(target.id),
            "organization_id": str(context.organization_id),
            # The human decision is authoritative: the classify stage must
            # honor it, never re-classify (which could bounce the document
            # straight back to unrouted).
            "triggered_by": "manual-route",
            **pins.job_payload(),
        },
        organization_id=context.organization_id,
        dedupe_key=f"document.preprocess:{document.id}:manual-route:{len(runs) + 1}",
        priority=document.priority,
    )
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=_actor(authorized),
        action="document.routed",
        target_type="document",
        target_id=str(document.id),
        organization_id=context.organization_id,
        summary={
            "from_stream_id": str(source_stream_id),
            "to_stream_id": str(target.id),
            "method": "manual",
        },
    )
    return {"document_id": str(document.id), "stream_slug": target.slug, "state": "queued"}
