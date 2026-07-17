"""Durable gold-set evaluation and promotion evidence APIs."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.streams import StreamRepository, StreamVersionRepository
from soa_api.services.stream_evaluation import create_stream_evaluation
from soa_db.evaluation_runs import (
    CALLER_SUBMITTED_EVIDENCE_SOURCE,
    EvaluationExecutionMode,
    EvaluationRun,
    EvaluationRunRepository,
)
from soa_db.gold_datasets import GoldDatasetVersionRepository
from soa_db.versioning import VersionState

router = APIRouter(tags=["evaluations"])


class EvaluationCreateRequest(BaseModel):
    execution_mode: EvaluationExecutionMode
    stream_version_id: uuid.UUID
    dataset_version_id: uuid.UUID
    baseline_run_id: uuid.UUID | None = None
    allow_external_provider: bool = Field(
        default=False,
        description=(
            "Explicit consent for a server evaluation to use a candidate provider whose "
            "declared data policy sends document content outside the deployment."
        ),
    )
    predictions: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Caller-supplied simulation predictions; never valid promotion evidence.",
    )

    @model_validator(mode="after")
    def validate_mode_inputs(self) -> "EvaluationCreateRequest":
        if self.execution_mode is EvaluationExecutionMode.SERVER:
            if self.predictions:
                raise ValueError("server evaluations cannot accept caller predictions")
        else:
            if not self.predictions:
                raise ValueError("simulation evaluations require caller predictions")
            if self.allow_external_provider:
                raise ValueError("simulation evaluations do not execute an external provider")
        return self


def _response(run: EvaluationRun) -> dict[str, Any]:
    gate = run.gate_result or {}
    return {
        "id": str(run.id),
        "stream_id": str(run.stream_id),
        "candidate_fingerprint": run.candidate_fingerprint,
        "dataset_version_id": str(run.dataset_version_id),
        "baseline_run_id": str(run.baseline_run_id) if run.baseline_run_id else None,
        "execution_mode": run.execution_mode,
        "stream_version_id": str(run.stream_version_id) if run.stream_version_id else None,
        "execution_fingerprint": run.execution_fingerprint,
        "state": run.state,
        "report": run.report,
        "gate_result": run.gate_result,
        "evidence_source": gate.get("evidence_source", CALLER_SUBMITTED_EVIDENCE_SOURCE)
        if run.execution_mode == EvaluationExecutionMode.SIMULATION.value
        else gate.get("evidence_source"),
        "promotion_eligible": gate.get("promotion_eligible") is True,
        "attestation": run.attestation,
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
    dataset = await GoldDatasetVersionRepository(session, context).get(body.dataset_version_id)
    if dataset is None or dataset.state not in (
        VersionState.PUBLISHED,
        VersionState.SUPERSEDED,
    ):
        raise HTTPException(status_code=409, detail="Evaluations require an immutable dataset.")
    run = await create_stream_evaluation(
        session,
        context,
        stream=stream,
        candidate=candidate,
        dataset=dataset,
        execution_mode=body.execution_mode,
        predictions=body.predictions,
        baseline_run_id=body.baseline_run_id,
        allow_external_provider=body.allow_external_provider,
        actor_id=authorized.principal.subject,
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
