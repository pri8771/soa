import uuid
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.evaluation_runs import (
    CALLER_SUBMITTED_EVIDENCE_SOURCE,
    PROMOTABLE_EVIDENCE_SOURCE,
    EvaluationExecutionMode,
    EvaluationRunRepository,
    EvaluationRunState,
    attestation_digest,
    canonical_json_digest,
    create_evaluation_run,
    runtime_pin_digest,
    snapshot_digest,
)
from soa_db.repository import OrganizationContext

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("22222222-2222-4222-8222-222222222222")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/evaluation-runs.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_only_attested_server_execution_can_be_promotion_evidence(
    db: DatabaseSessions,
) -> None:
    fingerprint = "f" * 64
    dataset_id = uuid.uuid4()
    async with db.session_scope() as session:
        repository = EvaluationRunRepository(session, CONTEXT)
        legacy = await create_evaluation_run(
            session,
            CONTEXT,
            stream_id=STREAM,
            candidate_fingerprint=fingerprint,
            dataset_version_id=dataset_id,
            predictions={},
            baseline_run_id=None,
            actor_id="user:test",
        )
        legacy.state = EvaluationRunState.SUCCEEDED
        legacy.gate_result = {"passed": True}

        caller = await create_evaluation_run(
            session,
            CONTEXT,
            stream_id=STREAM,
            candidate_fingerprint=fingerprint,
            dataset_version_id=dataset_id,
            predictions={},
            baseline_run_id=None,
            actor_id="user:test",
        )
        caller.state = EvaluationRunState.SUCCEEDED
        caller.gate_result = {
            "passed": True,
            "promotion_eligible": True,
            "evidence_source": CALLER_SUBMITTED_EVIDENCE_SOURCE,
        }
        assert await repository.passed_for_candidate(STREAM, fingerprint) is None

        snapshot = {
            "process_version_id": str(uuid.uuid4()),
            "process_version_number": 1,
            "config": {"provider_policy_version_id": str(uuid.uuid4())},
        }
        fingerprint = snapshot_digest(snapshot)
        snapshot["fingerprint"] = fingerprint
        stream_version_id = uuid.uuid4()
        pins = {
            "stream_version_id": str(stream_version_id),
            "config_fingerprint": fingerprint,
            "instruction_version_id": None,
            "confidence_policy_version_id": None,
            "provider_policy_version_id": snapshot["config"]["provider_policy_version_id"],
            "provider_credential_ref": None,
        }
        execution = runtime_pin_digest(pins)
        pins["execution_fingerprint"] = execution
        attested = await create_evaluation_run(
            session,
            CONTEXT,
            stream_id=STREAM,
            candidate_fingerprint=fingerprint,
            dataset_version_id=dataset_id,
            predictions={},
            baseline_run_id=None,
            actor_id="system:evaluator",
            execution_mode=EvaluationExecutionMode.SERVER,
            stream_version_id=stream_version_id,
            candidate_snapshot=snapshot,
            runtime_pins=pins,
            execution_fingerprint=execution,
        )
        attested.state = EvaluationRunState.SUCCEEDED
        attested.report = {"documents_scored": 1, "documents_failed": 0}
        runtime_provenance = {"adapter": "test", "model": "deterministic-v1"}
        runtime_fingerprint = canonical_json_digest(runtime_provenance)
        attested.attestation = {
            "schema_version": 1,
            "execution_mode": "server",
            "candidate_fingerprint": fingerprint,
            "execution_fingerprint": execution,
            "dataset_version_id": str(dataset_id),
            "documents": {
                "a" * 64: {
                    "document_sha256": "a" * 64,
                    "source_document_id": str(uuid.uuid4()),
                    "source_artifact_id": str(uuid.uuid4()),
                    "runtime_fingerprint": runtime_fingerprint,
                    "runtime_provenance": runtime_provenance,
                }
            },
        }
        attested.attestation["manifest_fingerprint"] = attestation_digest(attested.attestation)
        attested.gate_result = {
            "passed": True,
            "promotion_eligible": True,
            "evidence_source": PROMOTABLE_EVIDENCE_SOURCE,
            "execution_fingerprint": execution,
            "attestation_fingerprint": attested.attestation["manifest_fingerprint"],
        }
        assert await repository.passed_for_candidate(STREAM, fingerprint) is attested

        attested.attestation["documents"]["a" * 64]["runtime_fingerprint"] = "c" * 64
        assert await repository.passed_for_candidate(STREAM, fingerprint) is None

    await db.dispose()
