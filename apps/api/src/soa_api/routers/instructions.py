"""Extraction-instruction version API (AIO-010).

Instruction content is sensitive configuration, so every route sits
behind the dedicated ``instructions.read`` / ``instructions.manage``
permissions — holding ``streams.read`` does NOT expose prompt text.
Published versions are immutable; publishing supersedes the previous
version for the same stream version; every response carries the exact
``reference`` string extraction calls record.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.schemas import SchemaVersionRepository
from soa_api.domain.streams import StreamVersionRepository
from soa_db.instructions import (
    InstructionValidationError,
    InstructionVersion,
    InstructionVersionRepository,
    create_instruction_draft,
    publish_instruction_draft,
    update_instruction_draft,
)
from soa_db.versioning import InvalidVersionStateError

router = APIRouter(tags=["instructions"])


class InstructionContent(BaseModel):
    instructions: str
    field_guidance: dict[str, str] = Field(default_factory=dict)


class CreateDraftRequest(BaseModel):
    schema_version_id: uuid.UUID
    content: InstructionContent
    change_summary: str | None = Field(default=None, max_length=500)


class UpdateDraftRequest(BaseModel):
    content: InstructionContent
    change_summary: str | None = Field(default=None, max_length=500)


class InstructionVersionResponse(BaseModel):
    id: uuid.UUID
    stream_version_id: uuid.UUID
    schema_version_id: uuid.UUID
    version_number: int
    state: str
    reference: str
    content: dict[str, Any]
    change_summary: str | None
    published_at: str | None
    published_by: str | None

    @classmethod
    def from_model(cls, record: InstructionVersion) -> "InstructionVersionResponse":
        return cls(
            id=record.id,
            stream_version_id=record.stream_version_id,
            schema_version_id=record.schema_version_id,
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


async def _load_stream_version(
    session: DbSession, authorized: AuthorizedContext, stream_version_id: uuid.UUID
) -> None:
    record = await StreamVersionRepository(session, authorized.org_context).get(stream_version_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "stream version not found")


async def _load_instruction(
    session: DbSession, authorized: AuthorizedContext, version_id: uuid.UUID
) -> InstructionVersion:
    record = await InstructionVersionRepository(session, authorized.org_context).get(version_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "instruction version not found")
    return record


@router.get("/orgs/{organization_slug}/stream-versions/{stream_version_id}/instructions")
async def list_instruction_versions(
    stream_version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("instructions.read"))],
    session: DbSession,
) -> dict[str, Any]:
    await _load_stream_version(session, authorized, stream_version_id)
    versions = await InstructionVersionRepository(
        session, authorized.org_context
    ).list_for_stream_version(stream_version_id)
    return {"items": [InstructionVersionResponse.from_model(v) for v in versions]}


@router.post(
    "/orgs/{organization_slug}/stream-versions/{stream_version_id}/instructions",
    status_code=status.HTTP_201_CREATED,
)
async def create_draft_endpoint(
    stream_version_id: uuid.UUID,
    body: CreateDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("instructions.manage"))],
    session: DbSession,
) -> InstructionVersionResponse:
    await _load_stream_version(session, authorized, stream_version_id)
    schema_version = await SchemaVersionRepository(session, authorized.org_context).get(
        body.schema_version_id
    )
    if schema_version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "schema version not found")
    try:
        draft = await create_instruction_draft(
            session,
            authorized.org_context,
            stream_version_id=stream_version_id,
            schema_version_id=body.schema_version_id,
            content=body.content.model_dump(),
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
        )
    except InstructionValidationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    return InstructionVersionResponse.from_model(draft)


@router.get("/orgs/{organization_slug}/instructions/{version_id}")
async def get_instruction_version(
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("instructions.read"))],
    session: DbSession,
) -> InstructionVersionResponse:
    record = await _load_instruction(session, authorized, version_id)
    return InstructionVersionResponse.from_model(record)


@router.patch("/orgs/{organization_slug}/instructions/{version_id}")
async def update_draft_endpoint(
    version_id: uuid.UUID,
    body: UpdateDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("instructions.manage"))],
    session: DbSession,
) -> InstructionVersionResponse:
    record = await _load_instruction(session, authorized, version_id)
    try:
        updated = await update_instruction_draft(
            session,
            authorized.org_context,
            draft=record,
            content=body.content.model_dump(),
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
        )
    except InstructionValidationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except InvalidVersionStateError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    return InstructionVersionResponse.from_model(updated)


@router.post("/orgs/{organization_slug}/instructions/{version_id}/publish")
async def publish_endpoint(
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("instructions.manage"))],
    session: DbSession,
) -> InstructionVersionResponse:
    record = await _load_instruction(session, authorized, version_id)
    try:
        published = await publish_instruction_draft(
            session, authorized.org_context, draft=record, actor_id=_actor(authorized)
        )
    except InstructionValidationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except InvalidVersionStateError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    return InstructionVersionResponse.from_model(published)
