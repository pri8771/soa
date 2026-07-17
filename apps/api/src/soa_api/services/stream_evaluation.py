"""Shared gold-set evaluation creation.

The evaluations router and the training-set "measure the lift" action both
create the same kind of evaluation run: score a candidate stream configuration
against an immutable gold-dataset version and enqueue it. This holds that logic
once — resolve the candidate snapshot, enforce the server-mode attestation
preconditions, pin the runtime, and enqueue — so a training evaluation is
byte-for-byte the same contract as a manual one, gate and all.
"""

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.domain.processes import ProcessRepository, ProcessVersionRepository
from soa_api.domain.streams import Stream, StreamVersion, resolve_snapshot
from soa_api.services.runtime_pins import RuntimePinError, resolve_evaluation_runtime_pins
from soa_db.artifacts import ArtifactKind, ArtifactRepository
from soa_db.catalogs import CatalogError, materialize_catalog_version_pins
from soa_db.documents import DocumentRepository
from soa_db.evaluation_runs import (
    EvaluationExecutionMode,
    EvaluationRun,
    EvaluationRunRepository,
    EvaluationRunState,
    create_evaluation_run,
    is_promotable_server_evidence,
)
from soa_db.gold_datasets import GoldDatasetVersion, GoldDocumentRepository
from soa_db.jobs import enqueue_job
from soa_db.repository import OrganizationContext


async def create_stream_evaluation(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream: Stream,
    candidate: StreamVersion,
    dataset: GoldDatasetVersion,
    execution_mode: EvaluationExecutionMode,
    predictions: dict[str, dict[str, Any]],
    baseline_run_id: uuid.UUID | None,
    allow_external_provider: bool,
    actor_id: str,
) -> EvaluationRun:
    """Create and enqueue an evaluation of ``candidate`` against ``dataset``.

    ``stream``, ``candidate`` (belonging to it), and ``dataset`` (immutable) are
    pre-loaded by the caller; every other consistency check — process version,
    baseline compatibility, and the full server-mode attestation preconditions
    — happens here and raises ``HTTPException`` so both callers surface the same
    409s.
    """
    process = await ProcessRepository(session, context).get(stream.process_id)
    if process is None or process.active_version_id is None:
        raise HTTPException(status_code=409, detail="The process has no active version.")
    process_version = await ProcessVersionRepository(session, context).get(
        process.active_version_id
    )
    if process_version is None:
        raise HTTPException(status_code=409, detail="The active process version is missing.")

    if baseline_run_id is not None:
        baseline = await EvaluationRunRepository(session, context).get(baseline_run_id)
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
                status_code=409, detail="The baseline must be a completed evaluation."
            )
        if execution_mode is EvaluationExecutionMode.SERVER and not is_promotable_server_evidence(
            baseline
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "A server evaluation requires a baseline backed by complete, attested "
                    "server-executed evidence."
                ),
            )

    try:
        catalog_version_pins = await materialize_catalog_version_pins(
            session, context, stream_id=stream.id
        )
    except CatalogError as error:
        raise HTTPException(
            status_code=409, detail=f"Candidate catalog bindings are invalid: {error}"
        ) from None
    snapshot = resolve_snapshot(
        process_version, candidate.overrides, catalog_version_pins=catalog_version_pins
    )

    runtime_pins: dict[str, Any] | None = None
    execution_fingerprint: str | None = None
    if execution_mode is EvaluationExecutionMode.SERVER:
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
        predictions=predictions,
        baseline_run_id=baseline_run_id,
        actor_id=actor_id,
        execution_mode=execution_mode,
        stream_version_id=candidate.id
        if execution_mode is EvaluationExecutionMode.SERVER
        else None,
        candidate_snapshot=snapshot if execution_mode is EvaluationExecutionMode.SERVER else None,
        runtime_pins=runtime_pins,
        execution_fingerprint=execution_fingerprint,
        allow_external_provider=allow_external_provider,
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
    return run
