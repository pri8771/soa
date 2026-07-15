import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from soa_api.domain.policies import PolicyVersion
from soa_api.domain.rules import RuleSetVersion
from soa_api.domain.schemas import SchemaVersion
from soa_config import MemorySecretStore
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import Artifact, ArtifactKind, create_artifact
from soa_db.catalog_selections import CatalogFieldSelection
from soa_db.documents import DocumentRepository, SourceChannel, create_document
from soa_db.evaluation_runs import (
    CALLER_SUBMITTED_EVIDENCE_SOURCE,
    PROMOTABLE_EVIDENCE_SOURCE,
    EvaluationExecutionMode,
    EvaluationRun,
    EvaluationRunRepository,
    EvaluationRunState,
    create_evaluation_run,
    is_promotable_server_evidence,
    runtime_pin_digest,
)
from soa_db.extracted_fields import ExtractedField
from soa_db.gold_datasets import (
    PrivacyClassification,
    add_gold_document,
    create_dataset_version,
    create_gold_dataset,
    publish_dataset_version,
)
from soa_db.pages import DocumentPage
from soa_db.repository import OrganizationContext
from soa_db.review_tasks import ReviewTask
from soa_db.runs import ProcessingRun
from soa_storage import MemoryObjectStore, sha256_hex
from soa_worker.evaluation import (
    CohortMetrics,
    EvalDocument,
    EvalPrediction,
    EvaluationReport,
    FieldScore,
)
from soa_worker.evaluation_candidate import CandidateEvaluationError
from soa_worker.evaluation_orchestrator import (
    BaselineCompatibilityError,
    _compatible_baseline_report,
    execute_evaluation,
)
from soa_worker.extraction.mock import SYNTHETIC_SALES_ORDER
from soa_worker.providers import Capability, provider_info
from soa_worker.run_config import snapshot_fingerprint

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
            expected_class="purchase_order",
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
                    "predicted_class": "purchase_order",
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
        assert stored.gate_result is not None
        assert stored.gate_result["passed"] is False
        assert stored.gate_result["promotion_eligible"] is False
        assert stored.gate_result["evidence_source"] == CALLER_SUBMITTED_EVIDENCE_SOURCE
        assert any(
            finding["kind"] == "non_promotable_evidence_source"
            for finding in stored.gate_result["findings"]
        )
        assert (
            await EvaluationRunRepository(session, CONTEXT).passed_for_candidate(STREAM, "f" * 64)
            is None
        )

        # Redelivery is idempotent and leaves the completed result unchanged.
        await execute_evaluation(session, CONTEXT, run_id)


async def test_server_evaluation_executes_pinned_candidate_without_business_writes(
    db: DatabaseSessions,
) -> None:
    store = MemoryObjectStore()
    sha = sha256_hex(SYNTHETIC_SALES_ORDER)
    schema_id, rules_id, policy_id, stream_version_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    snapshot: dict[str, object] = {
        "process_version_id": str(uuid.uuid4()),
        "process_version_number": 7,
        "config": {
            "schema_version_id": str(schema_id),
            "rule_set_version_id": str(rules_id),
            "provider_policy_version_id": str(policy_id),
            "catalog_version_pins": [],
            "languages": ["en"],
            "input_contract": "single_sales_order",
        },
    }
    snapshot["fingerprint"] = snapshot_fingerprint(snapshot)
    pins: dict[str, object] = {
        "stream_version_id": str(stream_version_id),
        "config_fingerprint": snapshot["fingerprint"],
        "instruction_version_id": None,
        "confidence_policy_version_id": None,
        "provider_policy_version_id": str(policy_id),
        "provider_credential_ref": None,
    }
    pins["execution_fingerprint"] = runtime_pin_digest(pins)

    async with db.session_scope() as session:
        session.add_all(
            [
                SchemaVersion(
                    id=schema_id,
                    organization_id=ORG,
                    process_id=uuid.uuid4(),
                    version_number=1,
                    state="published",
                    definition={
                        "fields": [
                            {
                                "key": "po_number",
                                "label": "PO number",
                                "type": "text",
                                "criticality": "critical",
                                "normalization": "identifier",
                            },
                            {
                                "key": "customer_name",
                                "label": "Customer",
                                "type": "text",
                                "criticality": "critical",
                            },
                            {
                                "key": "lines",
                                "label": "Lines",
                                "type": "table",
                                "columns": [
                                    {
                                        "key": "sku",
                                        "label": "SKU",
                                        "type": "text",
                                        "criticality": "critical",
                                        "normalization": "identifier",
                                    },
                                    {
                                        "key": "quantity",
                                        "label": "Quantity",
                                        "type": "number",
                                        "criticality": "critical",
                                    },
                                ],
                            },
                        ]
                    },
                ),
                RuleSetVersion(
                    id=rules_id,
                    organization_id=ORG,
                    process_id=uuid.uuid4(),
                    version_number=1,
                    state="published",
                    definition={"version": "evaluation-test", "rules": []},
                ),
                PolicyVersion(
                    id=policy_id,
                    organization_id=ORG,
                    policy_type="provider",
                    version_number=1,
                    state="published",
                    definition={
                        "provider_name": "mock",
                        "capabilities": ["field_extraction"],
                    },
                ),
            ]
        )
        source = await create_document(
            session,
            CONTEXT,
            stream_id=STREAM,
            source_channel=SourceChannel.UPLOAD,
            original_filename="gold.pdf",
            content_sha256=sha,
            size_bytes=len(SYNTHETIC_SALES_ORDER),
            content_type="application/pdf",
            actor_id="user:test",
        )
        object_key = f"orgs/{ORG}/documents/{source.id}/original/gold.pdf"
        await store.put(object_key, SYNTHETIC_SALES_ORDER, content_type="application/pdf")
        await create_artifact(
            session,
            CONTEXT,
            document_id=source.id,
            kind=ArtifactKind.ORIGINAL,
            object_key=object_key,
            sha256=sha,
            size_bytes=len(SYNTHETIC_SALES_ORDER),
            content_type="application/pdf",
        )
        dataset = await create_gold_dataset(
            session,
            CONTEXT,
            name="Attested production benchmark",
            slug="attested-production-benchmark",
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
            source_document_id=source.id,
            split="test",
            expected_class="purchase_order",
            ground_truth={
                "fields": {
                    "po_number": "PO-100042",
                    "customer_name": "Acme Industrial Supply",
                },
                "lines": [
                    {"sku": "WID-100", "quantity": "10"},
                    {"sku": "GAD-205", "quantity": "3"},
                ],
            },
            actor_id="user:test",
        )
        await publish_dataset_version(
            session, CONTEXT, dataset=dataset, version=version, actor_id="user:test"
        )
        run = await create_evaluation_run(
            session,
            CONTEXT,
            stream_id=STREAM,
            candidate_fingerprint=str(snapshot["fingerprint"]),
            dataset_version_id=version.id,
            predictions={},
            baseline_run_id=None,
            actor_id="user:test",
            execution_mode=EvaluationExecutionMode.SERVER,
            stream_version_id=stream_version_id,
            candidate_snapshot=dict(snapshot),
            runtime_pins=dict(pins),
            execution_fingerprint=str(pins["execution_fingerprint"]),
        )
        run_id, source_id = run.id, source.id

    async with db.session_scope() as session:
        await execute_evaluation(
            session,
            CONTEXT,
            run_id,
            store=store,
            secret_store=MemorySecretStore(),
        )

    async with db.session_scope() as session:
        stored = await EvaluationRunRepository(session, CONTEXT).get(run_id)
        assert stored is not None and stored.state == EvaluationRunState.SUCCEEDED
        assert stored.gate_result is not None
        assert stored.gate_result["passed"] is True
        assert stored.gate_result["promotion_eligible"] is True
        assert stored.gate_result["evidence_source"] == PROMOTABLE_EVIDENCE_SOURCE
        assert is_promotable_server_evidence(stored)
        assert stored.attestation is not None
        evidence = stored.attestation["documents"][sha]
        assert evidence["source_document_id"] == str(source_id)
        assert evidence["runtime_provenance"]["rendering"]["engine"]["name"] == "pypdfium2"
        assert evidence["runtime_provenance"]["extraction"]["model"] == "mock-v1"
        serialized = json.dumps(stored.attestation)
        assert "PO-100042" not in serialized
        assert "Acme Industrial Supply" not in serialized
        source = await DocumentRepository(session, CONTEXT).get(source_id)
        assert source is not None and source.state == "received"
        assert await session.scalar(select(func.count()).select_from(ProcessingRun)) == 0
        assert await session.scalar(select(func.count()).select_from(Artifact)) == 1
        assert await session.scalar(select(func.count()).select_from(DocumentPage)) == 0
        assert await session.scalar(select(func.count()).select_from(ExtractedField)) == 0
        assert await session.scalar(select(func.count()).select_from(ReviewTask)) == 0
        assert await session.scalar(select(func.count()).select_from(CatalogFieldSelection)) == 0


async def test_retryable_server_failure_commits_checkpoint_and_resume_skips_scored_document(
    db: DatabaseSessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot: dict[str, object] = {
        "process_version_id": str(uuid.uuid4()),
        "process_version_number": 1,
        "config": {"provider_policy_version_id": str(uuid.uuid4())},
    }
    snapshot["fingerprint"] = snapshot_fingerprint(snapshot)
    stream_version_id = uuid.uuid4()
    pins: dict[str, object] = {
        "stream_version_id": str(stream_version_id),
        "config_fingerprint": snapshot["fingerprint"],
        "instruction_version_id": None,
        "confidence_policy_version_id": None,
        "provider_policy_version_id": snapshot["config"]["provider_policy_version_id"],
        "provider_credential_ref": None,
    }
    pins["execution_fingerprint"] = runtime_pin_digest(pins)
    async with db.session_scope() as session:
        dataset = await create_gold_dataset(
            session,
            CONTEXT,
            name="Resume benchmark",
            slug="resume-benchmark",
            privacy_classification=PrivacyClassification.SYNTHETIC,
            actor_id="user:test",
        )
        version = await create_dataset_version(
            session, CONTEXT, dataset=dataset, actor_id="user:test"
        )
        for sha, value in (("a" * 64, "PO-A"), ("b" * 64, "PO-B")):
            await add_gold_document(
                session,
                CONTEXT,
                version=version,
                document_sha256=sha,
                split="test",
                ground_truth={"fields": {"po_number": value}, "lines": []},
                actor_id="user:test",
            )
        await publish_dataset_version(
            session, CONTEXT, dataset=dataset, version=version, actor_id="user:test"
        )
        run = await create_evaluation_run(
            session,
            CONTEXT,
            stream_id=STREAM,
            candidate_fingerprint=str(snapshot["fingerprint"]),
            dataset_version_id=version.id,
            predictions={},
            baseline_run_id=None,
            actor_id="user:test",
            execution_mode=EvaluationExecutionMode.SERVER,
            stream_version_id=stream_version_id,
            candidate_snapshot=dict(snapshot),
            runtime_pins=dict(pins),
            execution_fingerprint=str(pins["execution_fingerprint"]),
        )
        run_id = run.id

    calls: list[str] = []
    fail = True

    class FakeEvaluator:
        provider_info = provider_info(Capability.FIELD_EXTRACTION, "mock")

        async def extract(self, document: EvalDocument) -> EvalPrediction:
            sha = document.document_sha256
            calls.append(sha)
            if fail and sha == "b" * 64:
                raise CandidateEvaluationError("provider temporarily unavailable", retryable=True)
            return EvalPrediction(fields={"po_number": "PO-A" if sha[0] == "a" else "PO-B"})

    async def fake_builder(*_args: object, **_kwargs: object) -> FakeEvaluator:
        return FakeEvaluator()

    monkeypatch.setattr(
        "soa_worker.evaluation_orchestrator.build_candidate_evaluator", fake_builder
    )
    with pytest.raises(CandidateEvaluationError, match="temporarily unavailable"):
        async with db.session_scope() as session:
            await execute_evaluation(
                session,
                CONTEXT,
                run_id,
                store=MemoryObjectStore(),
                secret_store=MemorySecretStore(),
            )
    async with db.session_scope() as session:
        interrupted = await EvaluationRunRepository(session, CONTEXT).get(run_id)
        assert interrupted is not None and interrupted.state == EvaluationRunState.RUNNING
        assert set(interrupted.checkpoint["scores"]) == {"a" * 64}
        assert interrupted.checkpoint["errors"] == {}

    fail = False
    async with db.session_scope() as session:
        await execute_evaluation(
            session,
            CONTEXT,
            run_id,
            store=MemoryObjectStore(),
            secret_store=MemorySecretStore(),
        )
    assert calls == ["a" * 64, "b" * 64, "b" * 64]


def _report() -> EvaluationReport:
    return EvaluationReport(
        documents_scored=1,
        documents_failed=0,
        field_exact_rate=1.0,
        field_normalized_rate=1.0,
        line_cell_exact_rate=1.0,
        class_accuracy=1.0,
        false_auto_approval_rate=0.0,
        mean_latency_ms=1.0,
        total_cost_cents=0,
        by_field={"po_number": FieldScore(total=1, exact=1, normalized=1)},
        by_split={"test": 1},
        by_cohort={
            "test": CohortMetrics(
                documents=1,
                field_exact_rate=1.0,
                field_normalized_rate=1.0,
                line_cell_exact_rate=1.0,
            )
        },
        errors={},
    )


def _runs() -> tuple[EvaluationRun, EvaluationRun, EvaluationReport]:
    dataset_id = uuid.uuid4()
    report = _report()
    baseline = EvaluationRun(
        id=uuid.uuid4(),
        organization_id=ORG,
        stream_id=STREAM,
        candidate_fingerprint="b" * 64,
        dataset_version_id=dataset_id,
        state=EvaluationRunState.SUCCEEDED,
        predictions={},
        checkpoint={},
        report=json.loads(report.to_json()),
        gate_result={},
        created_by="user:test",
    )
    run = EvaluationRun(
        id=uuid.uuid4(),
        organization_id=ORG,
        stream_id=STREAM,
        candidate_fingerprint="c" * 64,
        dataset_version_id=dataset_id,
        baseline_run_id=baseline.id,
        state=EvaluationRunState.RUNNING,
        predictions={},
        checkpoint={},
        created_by="user:test",
    )
    return run, baseline, report


def test_compatible_baseline_requires_matching_tenant_stream_dataset_and_schema() -> None:
    run, baseline, report = _runs()
    assert _compatible_baseline_report(run, baseline, report) == report


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("organization_id", uuid.UUID("99999999-9999-4999-8999-999999999999"), "organization"),
        ("stream_id", uuid.UUID("88888888-8888-4888-8888-888888888888"), "stream"),
        ("dataset_version_id", uuid.uuid4(), "dataset version"),
        ("state", EvaluationRunState.PENDING, "completed"),
    ],
)
def test_incompatible_baseline_identity_is_rejected(
    attribute: str, value: object, message: str
) -> None:
    run, baseline, report = _runs()
    setattr(baseline, attribute, value)
    with pytest.raises(BaselineCompatibilityError, match=message):
        _compatible_baseline_report(run, baseline, report)


def test_incompatible_metric_schema_is_rejected() -> None:
    run, baseline, report = _runs()
    assert baseline.report is not None
    baseline.report["metric_schema_version"] = "legacy-report-v0"
    with pytest.raises(BaselineCompatibilityError, match="metric schema"):
        _compatible_baseline_report(run, baseline, report)


def test_incompatible_cohort_and_field_schema_are_rejected() -> None:
    run, baseline, report = _runs()
    assert baseline.report is not None
    baseline.report["by_split"] = {"validation": 1}
    with pytest.raises(BaselineCompatibilityError, match="cohort"):
        _compatible_baseline_report(run, baseline, report)

    run, baseline, report = _runs()
    assert baseline.report is not None
    baseline.report["by_field"] = {"different_field": {"total": 1, "exact": 1, "normalized": 1}}
    with pytest.raises(BaselineCompatibilityError, match="field metric schema"):
        _compatible_baseline_report(run, baseline, report)
