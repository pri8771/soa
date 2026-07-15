"""Durable gold-set evaluation and promotion evidence APIs."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.processes import ProcessRepository, ProcessVersionRepository
from soa_api.domain.streams import StreamRepository, StreamVersionRepository, resolve_snapshot
from soa_api.services.runtime_pins import RuntimePinError, resolve_evaluation_runtime_pins
from soa_db.artifacts import ArtifactKind, ArtifactRepository
from soa_db.documents import DocumentRepository
from soa_db.evaluation_runs import (
    CALLER_SUBMITTED_EVIDENCE_SOURCE,
    EvaluationExecutionMode,
    EvaluationRun,
    EvaluationRunRepository,
    EvaluationRunState,
    create_evaluation_run,
    is_promotable_server_evidence,
)
from soa_db.gold_datasets import GoldDatasetVersionRepository, GoldDocumentRepository
from soa_db.jobs import enqueue_job
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
    if body.baseline_run_id is not None:
        baseline = await EvaluationRunRepository(session, context).get(body.baseline_run_id)
        if (
            baseline is None
            or baseline.stream_id != stream.id
            or baseline.dataset_version_id != dataset.id
        ):
            raise HTTPException(
                status_code=409,
                detail="The baseline must belong to this stream and immutable dataset version.",
            )
        if baseline.state != EvaluationRunState.SUCCEEDED or baseline.report is None:
            raise HTTPException(
                status_code=409,
                detail="The baseline must be a completed evaluation.",
            )
        if (
            body.execution_mode is EvaluationExecutionMode.SERVER
            and not is_promotable_server_evidence(baseline)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "A server evaluation requires a baseline backed by complete, attested "
                    "server-executed evidence."
                ),
            )
    snapshot = resolve_snapshot(process_version, candidate.overrides)
    runtime_pins: dict[str, Any] | None = None
    execution_fingerprint: str | None = None
    if body.execution_mode is EvaluationExecutionMode.SERVER:
        documents = await GoldDocumentRepository(session, context).list_for_version(dataset.id)
        if not documents:
            raise HTTPException(status_code=409, detail="The immutable dataset is empty.")
        for gold in documents:
            if gold.source_document_id is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Server evaluations require every gold document to reference its "
                        "tenant source document."
                    ),
                )
        source_ids = [gold.source_document_id for gold in documents if gold.source_document_id]
        sources = await DocumentRepository(session, context).get_many(source_ids)
        artifacts = await ArtifactRepository(session, context).list_for_documents(source_ids)
        for gold in documents:
            assert gold.source_document_id is not None
            source = sources.get(gold.source_document_id)
            if source is None or source.content_sha256 != gold.document_sha256:
                raise HTTPException(
                    status_code=409,
                    detail="A gold source document is missing or no longer matches its hash.",
                )
            originals = [
                artifact
                for artifact in artifacts.get(source.id, [])
                if artifact.kind == ArtifactKind.ORIGINAL.value
            ]
            if len(originals) != 1 or originals[0].sha256 != gold.document_sha256:
                raise HTTPException(
                    status_code=409,
                    detail="A gold source document has no unique matching original artifact.",
                )
        try:
            pins = await resolve_evaluation_runtime_pins(
                session,
                context,
                stream_version_id=candidate.id,
                candidate_snapshot=snapshot,
            )
        except RuntimePinError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        runtime_pins = pins.job_payload()
        execution_fingerprint = pins.execution_fingerprint
    run = await create_evaluation_run(
        session,
        context,
        stream_id=stream.id,
        candidate_fingerprint=str(snapshot["fingerprint"]),
        dataset_version_id=dataset.id,
        predictions=body.predictions,
        baseline_run_id=body.baseline_run_id,
        actor_id=authorized.principal.subject,
        execution_mode=body.execution_mode,
        stream_version_id=candidate.id
        if body.execution_mode is EvaluationExecutionMode.SERVER
        else None,
        candidate_snapshot=snapshot
        if body.execution_mode is EvaluationExecutionMode.SERVER
        else None,
        runtime_pins=runtime_pins,
        execution_fingerprint=execution_fingerprint,
        allow_external_provider=body.allow_external_provider,
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
