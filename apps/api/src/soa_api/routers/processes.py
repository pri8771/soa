"""Process and stream configuration endpoints (CFG-008).

Reads need ``processes.read`` / ``streams.read``; every write needs the
matching ``.manage`` permission and goes through the audited domain
helpers. Draft edits use optimistic concurrency (If-Match on the record
version); state errors (editing published history, publishing twice)
surface as 409, and a failed checked publish returns the full validation
report so the operator knows exactly what to fix.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.processes import (
    Process,
    ProcessRepository,
    ProcessVersion,
    ProcessVersionRepository,
    create_draft,
    create_process,
)
from soa_api.domain.streams import (
    Stream,
    StreamRepository,
    StreamVersion,
    StreamVersionRepository,
    create_stream,
    create_stream_draft,
    publish_stream_draft,
)
from soa_api.domain.versioning import (
    ImmutableVersionError,
    InvalidVersionStateError,
    VersionState,
)
from soa_api.services.config_service import (
    DraftNotPublishableError,
    publish_process_draft_checked,
    rollback_active_version,
    validate_process_draft,
)
from soa_db import CursorRequest
from soa_db.mixins import VersionConflictError

router = APIRouter(tags=["processes"])

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class ProcessCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=100, pattern=SLUG_PATTERN)


class ProcessResponse(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    active_version_id: str | None
    version: int

    @classmethod
    def from_model(cls, process: Process) -> "ProcessResponse":
        return cls(
            id=str(process.id),
            name=process.name,
            slug=process.slug,
            status=process.status,
            active_version_id=str(process.active_version_id) if process.active_version_id else None,
            version=process.version,
        )


class ProcessListItem(ProcessResponse):
    """Browser row (CFG-009): counts and the active version number ride
    along so the list renders without N+1 detail fetches."""

    active_version_number: int | None
    streams_count: int
    draft_count: int


class VersionResponse(BaseModel):
    id: str
    version_number: int
    state: str
    change_summary: str | None
    published_at: str | None
    published_by: str | None
    version: int

    @classmethod
    def from_model(cls, record: ProcessVersion) -> "VersionResponse":
        return cls(
            id=str(record.id),
            version_number=record.version_number,
            state=record.state,
            change_summary=record.change_summary,
            published_at=record.published_at.isoformat() if record.published_at else None,
            published_by=record.published_by,
            version=record.version,
        )


class VersionDetailResponse(VersionResponse):
    definition: dict[str, Any]

    @classmethod
    def from_model(cls, record: ProcessVersion) -> "VersionDetailResponse":
        base = VersionResponse.from_model(record)
        return cls(**base.model_dump(), definition=dict(record.definition))


class DraftCreateRequest(BaseModel):
    definition: dict[str, Any] | None = None
    change_summary: str | None = Field(default=None, max_length=500)
    # Clone: seed the draft from an existing version's definition.
    from_version_id: uuid.UUID | None = None


class DraftUpdateRequest(BaseModel):
    definition: dict[str, Any]
    change_summary: str | None = Field(default=None, max_length=500)


class RollbackRequest(BaseModel):
    target_version_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=500)


class StreamCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=100, pattern=SLUG_PATTERN)


class StreamResponse(BaseModel):
    id: str
    process_id: str
    name: str
    slug: str
    status: str
    active_version_id: str | None

    @classmethod
    def from_model(cls, stream: Stream) -> "StreamResponse":
        return cls(
            id=str(stream.id),
            process_id=str(stream.process_id),
            name=stream.name,
            slug=stream.slug,
            status=stream.status,
            active_version_id=str(stream.active_version_id) if stream.active_version_id else None,
        )


class StreamDraftRequest(BaseModel):
    overrides: dict[str, Any] = Field(default_factory=dict)
    change_summary: str | None = Field(default=None, max_length=500)


class StreamVersionResponse(BaseModel):
    id: str
    version_number: int
    state: str
    overrides: dict[str, Any]
    resolved_snapshot: dict[str, Any] | None
    pinned_process_version_id: str | None

    @classmethod
    def from_model(cls, record: StreamVersion) -> "StreamVersionResponse":
        return cls(
            id=str(record.id),
            version_number=record.version_number,
            state=record.state,
            overrides=dict(record.overrides),
            resolved_snapshot=dict(record.resolved_snapshot) if record.resolved_snapshot else None,
            pinned_process_version_id=str(record.pinned_process_version_id)
            if record.pinned_process_version_id
            else None,
        )


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


async def _load_process(
    session: DbSession, authorized: AuthorizedContext, process_slug: str
) -> Process:
    process = await ProcessRepository(session, authorized.org_context).get_by_slug(process_slug)
    if process is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Process not found.")
    return process


async def _load_version(
    session: DbSession, authorized: AuthorizedContext, process: Process, version_id: uuid.UUID
) -> ProcessVersion:
    record = await ProcessVersionRepository(session, authorized.org_context).get(version_id)
    if record is None or record.process_id != process.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    return record


# --- processes -------------------------------------------------------------


@router.get("/orgs/{organization_slug}/processes")
async def list_processes(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.read"))],
    session: DbSession,
) -> list[ProcessListItem]:
    org_id = authorized.org_context.organization_id
    page = await ProcessRepository(session, authorized.org_context).list_page(
        CursorRequest(limit=200)
    )
    stream_rows = (
        await session.execute(
            select(Stream.process_id, func.count())
            .where(Stream.organization_id == org_id)
            .group_by(Stream.process_id)
        )
    ).all()
    stream_counts: dict[uuid.UUID, int] = {row[0]: int(row[1]) for row in stream_rows}
    draft_rows = (
        await session.execute(
            select(ProcessVersion.process_id, func.count())
            .where(
                ProcessVersion.organization_id == org_id,
                ProcessVersion.state == VersionState.DRAFT,
            )
            .group_by(ProcessVersion.process_id)
        )
    ).all()
    draft_counts: dict[uuid.UUID, int] = {row[0]: int(row[1]) for row in draft_rows}
    active_ids = [p.active_version_id for p in page.items if p.active_version_id is not None]
    active_numbers: dict[uuid.UUID, int] = {}
    if active_ids:
        number_rows = (
            await session.execute(
                select(ProcessVersion.id, ProcessVersion.version_number).where(
                    ProcessVersion.organization_id == org_id,
                    ProcessVersion.id.in_(active_ids),
                )
            )
        ).all()
        active_numbers = {row[0]: int(row[1]) for row in number_rows}
    return [
        ProcessListItem(
            **ProcessResponse.from_model(process).model_dump(),
            active_version_number=active_numbers.get(process.active_version_id)
            if process.active_version_id
            else None,
            streams_count=int(stream_counts.get(process.id, 0)),
            draft_count=int(draft_counts.get(process.id, 0)),
        )
        for process in page.items
    ]


@router.post("/orgs/{organization_slug}/processes", status_code=status.HTTP_201_CREATED)
async def create_process_endpoint(
    body: ProcessCreateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
) -> ProcessResponse:
    existing = await ProcessRepository(session, authorized.org_context).get_by_slug(body.slug)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Process slug {body.slug!r} is already taken.",
        )
    process = await create_process(
        session,
        authorized.org_context,
        name=body.name,
        slug=body.slug,
        actor_id=_actor(authorized),
    )
    return ProcessResponse.from_model(process)


@router.get("/orgs/{organization_slug}/processes/{process_slug}")
async def get_process(
    process_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.read"))],
    session: DbSession,
) -> dict[str, Any]:
    process = await _load_process(session, authorized, process_slug)
    versions = await ProcessVersionRepository(session, authorized.org_context).list_for_process(
        process.id
    )
    streams = await StreamRepository(session, authorized.org_context).list_page(
        CursorRequest(limit=200)
    )
    return {
        "process": ProcessResponse.from_model(process).model_dump(),
        "versions": [VersionResponse.from_model(v).model_dump() for v in versions],
        "streams": [
            StreamResponse.from_model(s).model_dump()
            for s in streams.items
            if s.process_id == process.id
        ],
    }


@router.get("/orgs/{organization_slug}/processes/{process_slug}/versions/{version_id}")
async def get_version(
    process_slug: str,
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.read"))],
    session: DbSession,
) -> VersionDetailResponse:
    process = await _load_process(session, authorized, process_slug)
    record = await _load_version(session, authorized, process, version_id)
    return VersionDetailResponse.from_model(record)


@router.post(
    "/orgs/{organization_slug}/processes/{process_slug}/versions",
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    process_slug: str,
    body: DraftCreateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
) -> VersionDetailResponse:
    process = await _load_process(session, authorized, process_slug)
    definition = body.definition
    if body.from_version_id is not None:
        source = await _load_version(session, authorized, process, body.from_version_id)
        definition = dict(source.definition)
    draft = await create_draft(
        session,
        authorized.org_context,
        process=process,
        definition=definition,
        change_summary=body.change_summary,
        actor_id=_actor(authorized),
    )
    return VersionDetailResponse.from_model(draft)


@router.patch("/orgs/{organization_slug}/processes/{process_slug}/versions/{version_id}")
async def update_draft(
    process_slug: str,
    version_id: uuid.UUID,
    body: DraftUpdateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> VersionDetailResponse:
    process = await _load_process(session, authorized, process_slug)
    record = await _load_version(session, authorized, process, version_id)
    if record.state != VersionState.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Published versions are immutable; create a new draft.",
        )
    try:
        if if_match is not None:
            record.expect_version(if_match)
        record.definition = dict(body.definition)
        if body.change_summary is not None:
            record.change_summary = body.change_summary
        await session.flush()
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return VersionDetailResponse.from_model(record)


@router.post("/orgs/{organization_slug}/processes/{process_slug}/versions/{version_id}/validate")
async def validate_version(
    process_slug: str,
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.read"))],
    session: DbSession,
) -> dict[str, Any]:
    process = await _load_process(session, authorized, process_slug)
    record = await _load_version(session, authorized, process, version_id)
    report = await validate_process_draft(
        session, authorized.org_context, process=process, draft=record
    )
    return report.as_dict()


@router.post("/orgs/{organization_slug}/processes/{process_slug}/versions/{version_id}/publish")
async def publish_version(
    process_slug: str,
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    process = await _load_process(session, authorized, process_slug)
    record = await _load_version(session, authorized, process, version_id)
    try:
        published, report = await publish_process_draft_checked(
            session,
            authorized.org_context,
            process=process,
            draft=record,
            actor_id=_actor(authorized),
        )
    except DraftNotPublishableError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.report.as_dict()
        ) from None
    except (InvalidVersionStateError, ImmutableVersionError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return {
        "version": VersionResponse.from_model(published).model_dump(),
        "report": report.as_dict(),
    }


@router.post("/orgs/{organization_slug}/processes/{process_slug}/rollback")
async def rollback(
    process_slug: str,
    body: RollbackRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
) -> ProcessResponse:
    process = await _load_process(session, authorized, process_slug)
    target = await _load_version(session, authorized, process, body.target_version_id)
    try:
        await rollback_active_version(
            session,
            authorized.org_context,
            process=process,
            target=target,
            reason=body.reason,
            actor_id=_actor(authorized),
        )
    except InvalidVersionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return ProcessResponse.from_model(process)


# --- streams ---------------------------------------------------------------


@router.post(
    "/orgs/{organization_slug}/processes/{process_slug}/streams",
    status_code=status.HTTP_201_CREATED,
)
async def create_stream_endpoint(
    process_slug: str,
    body: StreamCreateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> StreamResponse:
    process = await _load_process(session, authorized, process_slug)
    existing = await StreamRepository(session, authorized.org_context).get_by_slug(body.slug)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Stream slug {body.slug!r} is already taken.",
        )
    stream = await create_stream(
        session,
        authorized.org_context,
        process_id=process.id,
        name=body.name,
        slug=body.slug,
        actor_id=_actor(authorized),
    )
    return StreamResponse.from_model(stream)


async def _load_stream(
    session: DbSession, authorized: AuthorizedContext, stream_slug: str
) -> Stream:
    stream = await StreamRepository(session, authorized.org_context).get_by_slug(stream_slug)
    if stream is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stream not found.")
    return stream


@router.get("/orgs/{organization_slug}/streams/{stream_slug}")
async def get_stream(
    stream_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, Any]:
    stream = await _load_stream(session, authorized, stream_slug)
    versions = await StreamVersionRepository(session, authorized.org_context).list_for_stream(
        stream.id
    )
    return {
        "stream": StreamResponse.from_model(stream).model_dump(),
        "versions": [StreamVersionResponse.from_model(v).model_dump() for v in versions],
    }


@router.post(
    "/orgs/{organization_slug}/streams/{stream_slug}/versions",
    status_code=status.HTTP_201_CREATED,
)
async def create_stream_version(
    stream_slug: str,
    body: StreamDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> StreamVersionResponse:
    stream = await _load_stream(session, authorized, stream_slug)
    draft = await create_stream_draft(
        session,
        authorized.org_context,
        stream=stream,
        overrides=body.overrides,
        change_summary=body.change_summary,
        actor_id=_actor(authorized),
    )
    return StreamVersionResponse.from_model(draft)


@router.post("/orgs/{organization_slug}/streams/{stream_slug}/versions/{version_id}/publish")
async def publish_stream_version(
    stream_slug: str,
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> StreamVersionResponse:
    stream = await _load_stream(session, authorized, stream_slug)
    draft = await StreamVersionRepository(session, authorized.org_context).get(version_id)
    if draft is None or draft.stream_id != stream.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    # Streams pin the process's CURRENT published version at publish time.
    process = await ProcessRepository(session, authorized.org_context).get(stream.process_id)
    if process is None or process.active_version_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The parent process has no published version to pin.",
        )
    process_version = await ProcessVersionRepository(session, authorized.org_context).get(
        process.active_version_id
    )
    if process_version is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The parent process's active version could not be loaded.",
        )
    try:
        published = await publish_stream_draft(
            session,
            authorized.org_context,
            stream=stream,
            draft=draft,
            process_version=process_version,
            actor_id=_actor(authorized),
        )
    except InvalidVersionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return StreamVersionResponse.from_model(published)
