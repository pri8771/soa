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
from soa_api.domain.policies import PolicyVersionRepository
from soa_api.domain.processes import (
    Process,
    ProcessDefinitionError,
    ProcessRepository,
    ProcessVersion,
    ProcessVersionRepository,
    create_draft,
    create_process,
    validate_process_definition,
)
from soa_api.domain.resolver import ENVIRONMENT_DEFAULTS, resolve_configuration
from soa_api.domain.rules import (
    RuleExpressionError,
    RuleSetVersion,
    RuleSetVersionRepository,
    create_rule_set_draft,
    publish_rule_set_draft,
    validate_rule_set,
)
from soa_api.domain.schemas import (
    SchemaValidationError,
    SchemaVersion,
    SchemaVersionRepository,
    create_schema_draft,
    publish_schema_draft,
    to_json_schema,
    validate_schema,
)
from soa_api.domain.streams import (
    Stream,
    StreamOverrideError,
    StreamRepository,
    StreamStatus,
    StreamVersion,
    StreamVersionRepository,
    create_stream,
    create_stream_draft,
    publish_stream_draft,
    resolve_snapshot,
    validate_stream_overrides,
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
from soa_db.audit import ActorType, record_audit_event
from soa_db.catalogs import CatalogError, materialize_catalog_version_pins
from soa_db.evaluation_runs import EvaluationRunRepository
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


class StreamListItem(StreamResponse):
    process_name: str
    process_slug: str
    active_version_number: int | None


class ArchiveStreamRequest(BaseModel):
    """Archiving stops intake for the stream; the operator must explain the
    impact so the audit trail records why documents stopped flowing."""

    impact: str = Field(min_length=10, max_length=500)


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
    version: int

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
            version=record.version,
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
    try:
        draft = await create_draft(
            session,
            authorized.org_context,
            process=process,
            definition=definition,
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
        )
    except ProcessDefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
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
        validate_process_definition(body.definition)
        record.definition = dict(body.definition)
        if body.change_summary is not None:
            record.change_summary = body.change_summary
        await session.flush()
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except ProcessDefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
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


@router.get("/orgs/{organization_slug}/streams")
async def list_streams(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> list[StreamListItem]:
    org_id = authorized.org_context.organization_id
    page = await StreamRepository(session, authorized.org_context).list_page(
        CursorRequest(limit=200)
    )
    process_rows = (
        await session.execute(
            select(Process.id, Process.name, Process.slug).where(Process.organization_id == org_id)
        )
    ).all()
    process_info = {row[0]: (str(row[1]), str(row[2])) for row in process_rows}
    active_ids = [s.active_version_id for s in page.items if s.active_version_id is not None]
    active_numbers: dict[uuid.UUID, int] = {}
    if active_ids:
        number_rows = (
            await session.execute(
                select(StreamVersion.id, StreamVersion.version_number).where(
                    StreamVersion.organization_id == org_id,
                    StreamVersion.id.in_(active_ids),
                )
            )
        ).all()
        active_numbers = {row[0]: int(row[1]) for row in number_rows}
    items: list[StreamListItem] = []
    for stream in page.items:
        name, slug = process_info.get(stream.process_id, ("(unknown)", ""))
        items.append(
            StreamListItem(
                **StreamResponse.from_model(stream).model_dump(),
                process_name=name,
                process_slug=slug,
                active_version_number=active_numbers.get(stream.active_version_id)
                if stream.active_version_id
                else None,
            )
        )
    return items


@router.get("/orgs/{organization_slug}/streams/{stream_slug}/simulation")
async def get_stream_simulation(
    stream_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, Any]:
    """Return the latest real evaluation and its baseline comparison."""
    stream = await _load_stream(session, authorized, stream_slug)
    runs = await EvaluationRunRepository(session, authorized.org_context).latest_for_stream(
        stream.id
    )
    completed = [run for run in runs if run.report is not None]
    if not completed:
        return {
            "available": False,
            "reason": "No evaluation runs have completed for this stream yet.",
        }
    candidate = completed[0]
    baseline = next(
        (run for run in completed if run.id == candidate.baseline_run_id),
        None,
    )
    return {
        "available": True,
        "candidate": {
            "run_id": str(candidate.id),
            "fingerprint": candidate.candidate_fingerprint,
            "report": candidate.report,
        },
        "baseline": (
            {
                "run_id": str(baseline.id),
                "fingerprint": baseline.candidate_fingerprint,
                "report": baseline.report,
            }
            if baseline is not None
            else None
        ),
        "gate": candidate.gate_result,
    }


@router.post("/orgs/{organization_slug}/streams/{stream_slug}/archive")
async def archive_stream(
    stream_slug: str,
    body: ArchiveStreamRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> StreamResponse:
    stream = await _load_stream(session, authorized, stream_slug)
    if stream.status == StreamStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Stream is already archived."
        )
    stream.status = StreamStatus.ARCHIVED
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=_actor(authorized),
        action="stream.archived",
        target_type="stream",
        target_id=str(stream.id),
        organization_id=authorized.org_context.organization_id,
        summary={"slug": stream.slug, "impact": body.impact},
    )
    return StreamResponse.from_model(stream)


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
    try:
        draft = await create_stream_draft(
            session,
            authorized.org_context,
            stream=stream,
            overrides=body.overrides,
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
        )
    except StreamOverrideError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return StreamVersionResponse.from_model(draft)


@router.patch("/orgs/{organization_slug}/streams/{stream_slug}/versions/{version_id}")
async def update_stream_version(
    stream_slug: str,
    version_id: uuid.UUID,
    body: StreamDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> StreamVersionResponse:
    stream = await _load_stream(session, authorized, stream_slug)
    record = await StreamVersionRepository(session, authorized.org_context).get(version_id)
    if record is None or record.stream_id != stream.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    if record.state != VersionState.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Published stream versions are immutable; create a new draft.",
        )
    try:
        if if_match is not None:
            record.expect_version(if_match)
        validate_stream_overrides(body.overrides)
        record.overrides = dict(body.overrides)
        if body.change_summary is not None:
            record.change_summary = body.change_summary
        await session.flush()
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except StreamOverrideError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return StreamVersionResponse.from_model(record)


class ResolvePreviewRequest(BaseModel):
    overrides: dict[str, Any] = Field(default_factory=dict)


@router.post("/orgs/{organization_slug}/streams/{stream_slug}/resolve")
async def resolve_stream_preview(
    stream_slug: str,
    body: ResolvePreviewRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, Any]:
    """Dry-run resolution for the inheritance editor (CFG-013): merge the
    proposed overrides against the parent process's active version and
    return every layer plus the provenance-tagged result. Persists nothing."""
    stream = await _load_stream(session, authorized, stream_slug)
    try:
        validate_stream_overrides(body.overrides)
    except StreamOverrideError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    process = await ProcessRepository(session, authorized.org_context).get(stream.process_id)
    if process is None or process.active_version_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The parent process has no published version to resolve against.",
        )
    process_version = await ProcessVersionRepository(session, authorized.org_context).get(
        process.active_version_id
    )
    if process_version is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The parent process's active version could not be loaded.",
        )
    return {
        "layers": {
            "environment": dict(ENVIRONMENT_DEFAULTS),
            "process": dict(process_version.definition),
            "stream": dict(body.overrides),
        },
        "resolved": resolve_configuration(
            process_version=process_version, stream_overrides=body.overrides
        ),
    }


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
        try:
            catalog_version_pins = await materialize_catalog_version_pins(
                session,
                authorized.org_context,
                stream_id=stream.id,
            )
        except CatalogError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Promotion blocked: catalog bindings are invalid: {exc}",
            ) from None
        candidate_snapshot = resolve_snapshot(
            process_version,
            draft.overrides,
            catalog_version_pins=catalog_version_pins,
        )
    except StreamOverrideError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    candidate_config = candidate_snapshot.get("config")
    assert isinstance(candidate_config, dict)
    runtime_problem = await _candidate_runtime_problem(
        session,
        authorized,
        stream,
        candidate_config,
    )
    if runtime_problem:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Promotion blocked: candidate runtime configuration is invalid: {runtime_problem}"
            ),
        )
    if stream.active_version_id is not None and bool(
        process_version.definition.get("evaluation_gate_required", True)
    ):
        evidence = await EvaluationRunRepository(
            session, authorized.org_context
        ).passed_for_candidate(stream.id, str(candidate_snapshot["fingerprint"]))
        if evidence is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Promotion blocked: this candidate has no successful gold-set evaluation "
                    "with a passing regression gate."
                ),
            )
    try:
        published = await publish_stream_draft(
            session,
            authorized.org_context,
            stream=stream,
            draft=draft,
            process_version=process_version,
            actor_id=_actor(authorized),
            catalog_version_pins=catalog_version_pins,
        )
    except InvalidVersionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except StreamOverrideError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return StreamVersionResponse.from_model(published)


async def _candidate_runtime_problem(
    session: DbSession,
    authorized: AuthorizedContext,
    stream: Stream,
    config: dict[str, Any],
) -> str | None:
    """Validate every immutable runtime reference before a stream is sealed."""

    def identifier(key: str) -> uuid.UUID | None:
        try:
            return uuid.UUID(str(config[key]))
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    schema_id = identifier("schema_version_id")
    rules_id = identifier("rule_set_version_id")
    provider_id = identifier("provider_policy_version_id")
    if schema_id is None or rules_id is None or provider_id is None:
        return "schema, rule-set, and provider-policy pins are required"
    schema = await SchemaVersionRepository(session, authorized.org_context).get(schema_id)
    if (
        schema is None
        or schema.process_id != stream.process_id
        or schema.state not in (VersionState.PUBLISHED, VersionState.SUPERSEDED)
    ):
        return "schema pin is missing, mutable, foreign, or belongs to another process"
    rules = await RuleSetVersionRepository(session, authorized.org_context).get(rules_id)
    if (
        rules is None
        or rules.process_id != stream.process_id
        or rules.state not in (VersionState.PUBLISHED, VersionState.SUPERSEDED)
    ):
        return "rule-set pin is missing, mutable, foreign, or belongs to another process"
    provider = await PolicyVersionRepository(session, authorized.org_context).get(provider_id)
    if (
        provider is None
        or provider.policy_type != "provider"
        or provider.state
        not in (
            VersionState.PUBLISHED,
            VersionState.SUPERSEDED,
        )
    ):
        return "provider-policy pin is missing, mutable, foreign, or has the wrong type"
    confidence_id = identifier("confidence_policy_version_id")
    if config.get("confidence_policy_version_id") is not None:
        if confidence_id is None:
            return "confidence-policy pin is invalid"
        confidence = await PolicyVersionRepository(session, authorized.org_context).get(
            confidence_id
        )
        if (
            confidence is None
            or confidence.policy_type != "confidence"
            or confidence.state
            not in (
                VersionState.PUBLISHED,
                VersionState.SUPERSEDED,
            )
        ):
            return "confidence-policy pin is missing, mutable, foreign, or has the wrong type"
    if config.get("input_contract") != "single_sales_order":
        return "only the single_sales_order input contract is executable in P0"
    return None


# --- extraction schema (CFG-011 over the CFG-003 domain) -------------------


class SchemaVersionResponse(BaseModel):
    id: str
    version_number: int
    state: str
    definition: dict[str, Any]
    change_summary: str | None
    version: int

    @classmethod
    def from_model(cls, record: SchemaVersion) -> "SchemaVersionResponse":
        return cls(
            id=str(record.id),
            version_number=record.version_number,
            state=record.state,
            definition=dict(record.definition),
            change_summary=record.change_summary,
            version=record.version,
        )


class SchemaDraftRequest(BaseModel):
    definition: dict[str, Any]
    change_summary: str | None = Field(default=None, max_length=500)


@router.get("/orgs/{organization_slug}/processes/{process_slug}/schema")
async def get_schema_versions(
    process_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.read"))],
    session: DbSession,
) -> dict[str, Any]:
    process = await _load_process(session, authorized, process_slug)
    versions = await SchemaVersionRepository(session, authorized.org_context).list_for_process(
        process.id
    )
    published = next((v for v in versions if v.state == VersionState.PUBLISHED), None)
    return {
        "versions": [SchemaVersionResponse.from_model(v).model_dump() for v in versions],
        "published_json_schema": to_json_schema(validate_schema(published.definition))
        if published
        else None,
    }


@router.post(
    "/orgs/{organization_slug}/processes/{process_slug}/schema/versions",
    status_code=status.HTTP_201_CREATED,
)
async def create_schema_version(
    process_slug: str,
    body: SchemaDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
) -> SchemaVersionResponse:
    process = await _load_process(session, authorized, process_slug)
    try:
        draft = await create_schema_draft(
            session,
            authorized.org_context,
            process_id=process.id,
            definition=body.definition,
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
        )
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    return SchemaVersionResponse.from_model(draft)


@router.patch("/orgs/{organization_slug}/processes/{process_slug}/schema/versions/{version_id}")
async def update_schema_draft(
    process_slug: str,
    version_id: uuid.UUID,
    body: SchemaDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> SchemaVersionResponse:
    process = await _load_process(session, authorized, process_slug)
    record = await SchemaVersionRepository(session, authorized.org_context).get(version_id)
    if record is None or record.process_id != process.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    if record.state != VersionState.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Published schema versions are immutable; create a new draft.",
        )
    try:
        validate_schema(body.definition)  # no invalid schema can be stored
        if if_match is not None:
            record.expect_version(if_match)
        record.definition = dict(body.definition)
        if body.change_summary is not None:
            record.change_summary = body.change_summary
        await session.flush()
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return SchemaVersionResponse.from_model(record)


@router.post(
    "/orgs/{organization_slug}/processes/{process_slug}/schema/versions/{version_id}/publish"
)
async def publish_schema_version(
    process_slug: str,
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
) -> SchemaVersionResponse:
    process = await _load_process(session, authorized, process_slug)
    record = await SchemaVersionRepository(session, authorized.org_context).get(version_id)
    if record is None or record.process_id != process.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    try:
        published = await publish_schema_draft(
            session, authorized.org_context, draft=record, actor_id=_actor(authorized)
        )
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    except InvalidVersionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return SchemaVersionResponse.from_model(published)


# --- validation rules (CFG-012 over the CFG-004 domain) ---------------------


class RuleSetVersionResponse(BaseModel):
    id: str
    version_number: int
    state: str
    definition: dict[str, Any]
    change_summary: str | None
    version: int

    @classmethod
    def from_model(cls, record: RuleSetVersion) -> "RuleSetVersionResponse":
        return cls(
            id=str(record.id),
            version_number=record.version_number,
            state=record.state,
            definition=dict(record.definition),
            change_summary=record.change_summary,
            version=record.version,
        )


class RuleSetDraftRequest(BaseModel):
    definition: dict[str, Any]
    change_summary: str | None = Field(default=None, max_length=500)


class RuleSetValidateRequest(BaseModel):
    definition: dict[str, Any]


async def _schema_field_types(
    session: DbSession, authorized: AuthorizedContext, process_id: uuid.UUID
) -> dict[str, str]:
    """Field types from the process's published schema — the type universe
    rules must check against. Empty when no schema is published yet, so any
    field reference fails validation with an honest 'unknown field'."""
    versions = await SchemaVersionRepository(session, authorized.org_context).list_for_process(
        process_id
    )
    published = next((v for v in versions if v.state == VersionState.PUBLISHED), None)
    if published is None:
        return {}
    return validate_schema(published.definition).field_types()


@router.get("/orgs/{organization_slug}/processes/{process_slug}/rules")
async def get_rule_set_versions(
    process_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.read"))],
    session: DbSession,
) -> dict[str, Any]:
    process = await _load_process(session, authorized, process_slug)
    versions = await RuleSetVersionRepository(session, authorized.org_context).list_for_process(
        process.id
    )
    return {
        "versions": [RuleSetVersionResponse.from_model(v).model_dump() for v in versions],
        "field_types": await _schema_field_types(session, authorized, process.id),
    }


@router.post("/orgs/{organization_slug}/processes/{process_slug}/rules/validate")
async def validate_rule_set_endpoint(
    process_slug: str,
    body: RuleSetValidateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.read"))],
    session: DbSession,
) -> dict[str, Any]:
    """Dry-run validation: type-check every condition and execute authored
    test cases without persisting anything. The builder's 'run test cases'
    button calls this."""
    process = await _load_process(session, authorized, process_slug)
    field_types = await _schema_field_types(session, authorized, process.id)
    try:
        validate_rule_set(body.definition, field_types)
    except RuleExpressionError as exc:
        return {"valid": False, "message": str(exc)}
    return {"valid": True, "message": None}


@router.post(
    "/orgs/{organization_slug}/processes/{process_slug}/rules/versions",
    status_code=status.HTTP_201_CREATED,
)
async def create_rule_set_version(
    process_slug: str,
    body: RuleSetDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
) -> RuleSetVersionResponse:
    process = await _load_process(session, authorized, process_slug)
    field_types = await _schema_field_types(session, authorized, process.id)
    try:
        draft = await create_rule_set_draft(
            session,
            authorized.org_context,
            process_id=process.id,
            definition=body.definition,
            field_types=field_types,
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
        )
    except RuleExpressionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    return RuleSetVersionResponse.from_model(draft)


@router.patch("/orgs/{organization_slug}/processes/{process_slug}/rules/versions/{version_id}")
async def update_rule_set_draft(
    process_slug: str,
    version_id: uuid.UUID,
    body: RuleSetDraftRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> RuleSetVersionResponse:
    process = await _load_process(session, authorized, process_slug)
    record = await RuleSetVersionRepository(session, authorized.org_context).get(version_id)
    if record is None or record.process_id != process.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    if record.state != VersionState.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Published rule-set versions are immutable; create a new draft.",
        )
    field_types = await _schema_field_types(session, authorized, process.id)
    try:
        validate_rule_set(body.definition, field_types)  # no invalid rule set can be stored
        if if_match is not None:
            record.expect_version(if_match)
        record.definition = dict(body.definition)
        if body.change_summary is not None:
            record.change_summary = body.change_summary
        await session.flush()
    except RuleExpressionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return RuleSetVersionResponse.from_model(record)


@router.post(
    "/orgs/{organization_slug}/processes/{process_slug}/rules/versions/{version_id}/publish"
)
async def publish_rule_set_version(
    process_slug: str,
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("processes.manage"))],
    session: DbSession,
) -> RuleSetVersionResponse:
    process = await _load_process(session, authorized, process_slug)
    record = await RuleSetVersionRepository(session, authorized.org_context).get(version_id)
    if record is None or record.process_id != process.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    field_types = await _schema_field_types(session, authorized, process.id)
    try:
        published = await publish_rule_set_draft(
            session,
            authorized.org_context,
            draft=record,
            field_types=field_types,
            actor_id=_actor(authorized),
        )
    except RuleExpressionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    except InvalidVersionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return RuleSetVersionResponse.from_model(published)
