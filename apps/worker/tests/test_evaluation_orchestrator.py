import uuid
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.evaluation_runs import EvaluationRunRepository, create_evaluation_run
from soa_db.gold_datasets import (
    PrivacyClassification,
    add_gold_document,
    create_dataset_version,
    create_gold_dataset,
    publish_dataset_version,
)
from soa_db.repository import OrganizationContext
from soa_worker.evaluation_orchestrator import execute_evaluation

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("22222222-2222-4222-8222-222222222222")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/evaluation.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_evaluation_run_persists_report_checkpoint_and_gate(db: DatabaseSessions) -> None:
    sha = "a" * 64
    async with db.session_scope() as session:
        dataset = await create_gold_dataset(
            session,
            CONTEXT,
            name="Production benchmark",
            slug="production-benchmark",
            privacy_classification=PrivacyClassification.SYNTHETIC,
            actor_id="user:test",
        )
        version = await create_dataset_version(
            session, CONTEXT, dataset=dataset, actor_id="user:test"
        )
        await add_gold_document(
            session,
            CONTEXT,
            version=version,
            document_sha256=sha,
            split="test",
            expected_class="sales_order",
            ground_truth={"fields": {"po_number": "PO-1"}, "lines": []},
            actor_id="user:test",
        )
        await publish_dataset_version(
            session, CONTEXT, dataset=dataset, version=version, actor_id="user:test"
        )
        run = await create_evaluation_run(
            session,
            CONTEXT,
            stream_id=STREAM,
            candidate_fingerprint="f" * 64,
            dataset_version_id=version.id,
            predictions={
                sha: {
                    "fields": {"po_number": "PO-1"},
                    "lines": [],
                    "predicted_class": "sales_order",
                    "would_auto_approve": True,
                }
            },
            baseline_run_id=None,
            actor_id="user:test",
        )
        run_id = run.id

    async with db.session_scope() as session:
        await execute_evaluation(session, CONTEXT, run_id)

    async with db.session_scope() as session:
        stored = await EvaluationRunRepository(session, CONTEXT).get(run_id)
        assert stored is not None and stored.state == "succeeded"
        assert stored.report is not None and stored.report["field_exact_rate"] == 1.0
        assert stored.checkpoint["scores"][sha]["fields_exact"] == 1
        assert stored.checkpoint["by_field"]["po_number"]["exact"] == 1
        assert stored.gate_result is not None and stored.gate_result["passed"] is True

        # Redelivery is idempotent and leaves the completed result unchanged.
        await execute_evaluation(session, CONTEXT, run_id)
