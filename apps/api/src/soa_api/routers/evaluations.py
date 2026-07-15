"""Durable gold-set evaluation and promotion evidence APIs."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.processes import ProcessRepository, ProcessVersionRepository
from soa_api.domain.streams import StreamRepository, StreamVersionRepository, resolve_snapshot
from soa_db.evaluation_runs import (
    EvaluationRun,
    EvaluationRunRepository,
    create_evaluation_run,
)
from soa_db.gold_datasets import GoldDatasetVersionRepository
from soa_db.jobs import enqueue_job
from soa_db.versioning import VersionState

router = APIRouter(tags=["evaluations"])


class EvaluationCreateRequest(BaseModel):
    stream_version_id: uuid.UUID
    dataset_version_id: uuid.UUID
    baseline_run_id: uuid.UUID | None = None
    predictions: dict[str, dict[str, Any]] = Field(default_factory=dict)


def _response(run: EvaluationRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "stream_id": str(run.stream_id),
        "candidate_fingerprint": run.candidate_fingerprint,
        "dataset_version_id": str(run.dataset_version_id),
        "baseline_run_id": str(run.baseline_run_id) if run.baseline_run_id else None,
        "state": run.state,
        "report": run.report,
        "gate_result": run.gate_result,
        "safe_error": run.safe_error,
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.post(
    "/orgs/{organization_slug}/streams/{stream_slug}/evaluations",
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_evaluation(
    stream_slug: str,
    body: EvaluationCreateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    context = authorized.org_context
    stream = await StreamRepository(session, context).get_by_slug(stream_slug)
    if stream is None:
        raise HTTPException(status_code=404, detail="Stream not found.")
    candidate = await StreamVersionRepository(session, context).get(body.stream_version_id)
    if candidate is None or candidate.stream_id != stream.id:
        raise HTTPException(status_code=404, detail="Candidate stream version not found.")
    process = await ProcessRepository(session, context).get(stream.process_id)
    if process is None or process.active_version_id is None:
        raise HTTPException(status_code=409, detail="The process has no active version.")
    process_version = await ProcessVersionRepository(session, context).get(
        process.active_version_id
    )
    if process_version is None:
        raise HTTPException(status_code=409, detail="The active process version is missing.")
    dataset = await GoldDatasetVersionRepository(session, context).get(body.dataset_version_id)
    if dataset is None or dataset.state not in (
        VersionState.PUBLISHED,
        VersionState.SUPERSEDED,
    ):
        raise HTTPException(status_code=409, detail="Evaluations require an immutable dataset.")
    snapshot = resolve_snapshot(process_version, candidate.overrides)
    run = await create_evaluation_run(
        session,
        context,
        stream_id=stream.id,
        candidate_fingerprint=str(snapshot["fingerprint"]),
        dataset_version_id=dataset.id,
        predictions=body.predictions,
        baseline_run_id=body.baseline_run_id,
        actor_id=authorized.principal.subject,
    )
    await enqueue_job(
        session,
        job_type="evaluation.run",
        organization_id=context.organization_id,
        payload={
            "organization_id": str(context.organization_id),
            "evaluation_run_id": str(run.id),
        },
        dedupe_key=f"evaluation:{run.id}",
        max_attempts=3,
    )
    return _response(run)


@router.get("/orgs/{organization_slug}/streams/{stream_slug}/evaluations")
async def list_evaluations(
    stream_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> list[dict[str, Any]]:
    stream = await StreamRepository(session, authorized.org_context).get_by_slug(stream_slug)
    if stream is None:
        raise HTTPException(status_code=404, detail="Stream not found.")
    rows = await EvaluationRunRepository(session, authorized.org_context).latest_for_stream(
        stream.id
    )
    return [_response(row) for row in rows]


@router.get("/orgs/{organization_slug}/evaluations/{run_id}")
async def get_evaluation(
    run_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("streams.read"))],
    session: DbSession,
) -> dict[str, Any]:
    run = await EvaluationRunRepository(session, authorized.org_context).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found.")
    return _response(run)
