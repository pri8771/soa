"""Sensitive logging and telemetry canary suite (SEC-006).

REQUIRED in CI. Recognizable canary values — platform secrets, tenant
credentials, API keys, and customer document content — are pushed
through the critical paths (intake, credential management, rate-limit
denials, and the full worker pipeline), then EVERY observability
channel is swept for them:

- log records, formatted through the REAL ``JsonFormatter`` transport
  (message, extras after redaction, exception summaries);
- exported spans (names, attributes, events including recorded
  exceptions) via an in-memory exporter;
- exported metrics via an in-memory reader;
- API response bodies, including error responses — a 401 must not echo
  the bad key that caused it.

A canary surfacing anywhere here is a data leak, full stop.
"""

import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.domain.credentials import create_credential
from soa_api.settings import ApiSettings, Environment
from soa_config import MemorySecretStore
from soa_config.logging import JsonFormatter
from soa_config.telemetry import configure_telemetry
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.documents import DocumentState, SourceChannel, create_document, transition_document
from soa_db.extracted_fields import create_extracted_field
from soa_db.jobs import Job, JobStatus
from soa_db.repository import OrganizationContext
from soa_db.runs import start_run
from soa_storage import MemoryObjectStore, sha256_hex
from soa_storage.keys import artifact_key
from soa_worker.extraction.mock import (
    SYNTHETIC_SALES_ORDER,
    MockExtractionProvider,
)
from soa_worker.orchestrator import Orchestrator
from soa_worker.pipeline import build_executors

ADMIN = {"X-Dev-User": "user:admin"}

#: Platform-level canaries, injected through settings.
CANARY_SECRET_KEY = "CANARY-platform-secret-key-0123456789abcdef"
CANARY_DB_PASSWORD = "CANARY-database-password-77f"
#: Tenant-level canaries, injected through the API.
CANARY_CREDENTIAL = "whsec_CANARY_credential_value_31c"
CANARY_CREDENTIAL_ROTATED = "whsec_CANARY_rotated_value_9d4"
#: Customer document content, ingested as file bytes.
CANARY_DOCUMENT = b"%PDF-1.7 CANARY-DOCUMENT-BODY-5f2e purchase order"
#: Customer DATA on a document erased through the SEC-010 deletion path —
#: the extracted value the deletion workflow removes must not leak.
CANARY_DELETION_FIELD = "PO-CANARY-deletion-8a3f"
#: Extracted values of the synthetic sales order — customer DATA that
#: flows through the entire worker pipeline.
DOCUMENT_VALUE_CANARIES = ("PO-100042", "Acme Industrial Supply")


def formatted_log_output(records: list[logging.LogRecord]) -> str:
    """Captured records rendered through the REAL log transport.

    Swept records: EVERY level for the platform's own loggers (``soa*``
    code must never log a sensitive value, even at DEBUG), and INFO and
    above for third-party loggers — the configured production level
    (``configure_logging`` defaults to INFO). The exclusion this
    carves out is deliberate and narrow: the aiosqlite TEST driver
    echoes SQL parameters at DEBUG; production runs asyncpg at INFO,
    where no such echo exists.
    """
    formatter = JsonFormatter(service_name="soa-test", environment="test")
    swept = [r for r in records if r.name.startswith("soa") or r.levelno >= logging.INFO]
    return "\n".join(formatter.format(record) for record in swept)


def assert_no_canaries(haystack: str, canaries: dict[str, str], *, channel: str) -> None:
    for label, canary in canaries.items():
        assert canary not in haystack, f"{label} leaked into {channel}"


async def test_api_critical_paths_leak_no_canaries_anywhere(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()
    telemetry = configure_telemetry(
        service_name="soa-api",
        environment="test",
        span_exporter=span_exporter,
        metric_reader=metric_reader,
    )
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/canary.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    store = MemoryObjectStore()
    app = create_app(
        ApiSettings(
            environment=Environment.TEST,
            secret_key=SecretStr(CANARY_SECRET_KEY),
            database_url=SecretStr(
                f"postgresql+asyncpg://svc:{CANARY_DB_PASSWORD}@db.internal:5432/soa"
            ),
            api_ingest_rate_per_minute=2,
        ),
        telemetry=telemetry,
        db=db,
        object_store=store,
        secret_store=MemorySecretStore(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    responses: list[str] = []

    def track(response: Any) -> Any:
        responses.append(response.text)
        return response

    with caplog.at_level(logging.DEBUG):
        # Tenant, process, stream.
        for path, body in (
            ("/organizations", {"name": "Northstar", "slug": "northstar"}),
            ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
            (
                "/orgs/northstar/processes/purchase-orders/streams",
                {"name": "Email intake", "slug": "email"},
            ),
        ):
            assert track(client.post(path, json=body, headers=ADMIN)).status_code == 201

        # Integration credential set + rotation (SEC-005 path).
        assert (
            track(
                client.post(
                    "/orgs/northstar/integrations",
                    json={"name": "ERP", "slug": "erp", "integration_type": "webhook"},
                    headers=ADMIN,
                )
            ).status_code
            == 201
        )
        for value in (CANARY_CREDENTIAL, CANARY_CREDENTIAL_ROTATED):
            assert (
                track(
                    client.put(
                        "/orgs/northstar/integrations/erp/credential",
                        json={"kind": "webhook_hmac_secret", "secret": value},
                        headers=ADMIN,
                    )
                ).status_code
                == 200
            )

        # Upload session over the canary document's digest.
        assert (
            track(
                client.post(
                    "/orgs/northstar/streams/email/uploads",
                    json={
                        "filename": "po.pdf",
                        "size_bytes": len(CANARY_DOCUMENT),
                        "content_type": "application/pdf",
                        "sha256": sha256_hex(CANARY_DOCUMENT),
                    },
                    headers=ADMIN,
                )
            ).status_code
            == 201
        )

        # Public API ingestion: happy path, a WRONG key (401), and the
        # rate limit (429) — error paths must not echo keys either.
        org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
        async with db.session_scope() as session:
            _credential, raw_api_key = await create_credential(
                session,
                OrganizationContext(organization_id=org_id),
                name="erp-connector",
                scopes=["documents.upload"],
                actor_id="user:test",
            )

        def ingest(key: str, data: bytes) -> Any:
            return track(
                client.post(
                    "/v1/streams/email/documents",
                    files={"file": ("po.pdf", data, "application/pdf")},
                    headers={"X-Api-Key": key},
                )
            )

        assert ingest(raw_api_key, CANARY_DOCUMENT).status_code == 201
        assert ingest("soa_wrong_key_entirely", CANARY_DOCUMENT + b"2").status_code == 401
        assert ingest(raw_api_key, CANARY_DOCUMENT + b"3").status_code == 201
        assert ingest(raw_api_key, CANARY_DOCUMENT + b"4").status_code == 429

        # Operator-initiated deletion (SEC-010): a document tagged with a
        # customer-DATA canary (a stored object plus an extracted field) is
        # erased through the endpoint. The workflow removes the object and
        # the field and audits counts-only — the extracted value and the
        # object bytes must never surface in a log, span, metric, or the
        # response envelope.
        context = OrganizationContext(organization_id=org_id)
        stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
        deletable = CANARY_DOCUMENT + b" deletable"
        async with db.session_scope() as session:
            doomed = await create_document(
                session,
                context,
                stream_id=stream_id,
                source_channel=SourceChannel.UPLOAD,
                original_filename="doomed.pdf",
                content_sha256=sha256_hex(deletable),
                size_bytes=len(deletable),
                content_type="application/pdf",
                actor_id="user:test",
            )
            # received -> cancelled is a single valid transition into a
            # deletable state.
            await transition_document(
                session,
                context,
                document=doomed,
                to_state=DocumentState.CANCELLED,
                actor_id="worker",
            )
            run = await start_run(
                session,
                context,
                document_id=doomed.id,
                input_sha256=sha256_hex(deletable),
                stream_version_id=None,
                config_fingerprint="f" * 64,
                triggered_by="user:test",
            )
            await create_extracted_field(
                session,
                context,
                document_id=doomed.id,
                run_id=run.id,
                field_key="po_number",
                raw_value=CANARY_DELETION_FIELD,
                confidence=0.99,
                provider="mock",
            )
            key = artifact_key(org_id, doomed.id, kind="original", filename="doomed.pdf")
            await store.put(key, deletable, content_type="application/pdf")
            await create_artifact(
                session,
                context,
                document_id=doomed.id,
                kind=ArtifactKind.ORIGINAL,
                object_key=key,
                sha256=sha256_hex(deletable),
                size_bytes=len(deletable),
                content_type="application/pdf",
            )
            doomed_id = doomed.id

        assert (
            track(
                client.post(
                    f"/orgs/northstar/documents/{doomed_id}/deletion",
                    json={"reason": "canary erasure request"},
                    headers=ADMIN,
                )
            ).status_code
            == 201
        )

    canaries = {
        "platform secret_key": CANARY_SECRET_KEY,
        "database password": CANARY_DB_PASSWORD,
        "integration credential": CANARY_CREDENTIAL,
        "rotated integration credential": CANARY_CREDENTIAL_ROTATED,
        "raw API key": raw_api_key,
        # The marker substring, so even a PARTIAL body leak trips it.
        "document content": "CANARY-DOCUMENT-BODY-5f2e",
        # Customer DATA erased through the deletion endpoint.
        "deletion extracted field": CANARY_DELETION_FIELD,
    }
    assert_no_canaries(formatted_log_output(caplog.records), canaries, channel="logs")
    spans = span_exporter.get_finished_spans()
    span_dump = "\n".join(span.to_json() for span in spans)
    assert_no_canaries(span_dump, canaries, channel="traces")
    assert_no_canaries(str(metric_reader.get_metrics_data()), canaries, channel="metrics")
    # No response may carry a canary — the credential endpoints
    # acknowledge without echoing, and auth errors never quote the key.
    assert_no_canaries("\n".join(responses), canaries, channel="API responses")


ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("33333333-3333-4333-8333-333333333333")
CONTEXT = OrganizationContext(organization_id=ORG)


async def test_worker_pipeline_leaks_no_document_values_into_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The synthetic sales order carries known customer values; the full
    render -> extract -> normalize -> validate pipeline must keep them
    (and the raw bytes) out of every log record and job row."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/canary-worker.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    store = MemoryObjectStore()

    data = SYNTHETIC_SALES_ORDER
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=STREAM,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256=sha256_hex(data),
            size_bytes=len(data),
            content_type="application/pdf",
            actor_id="user:test",
        )
        key = artifact_key(ORG, document.id, kind="original", filename="po.pdf")
        await store.put(key, data, content_type="application/pdf")
        await create_artifact(
            session,
            CONTEXT,
            document_id=document.id,
            kind=ArtifactKind.ORIGINAL,
            object_key=key,
            sha256=sha256_hex(data),
            size_bytes=len(data),
            content_type="application/pdf",
        )
        for state in (DocumentState.VALIDATING_FILE, DocumentState.QUEUED):
            await transition_document(
                session, CONTEXT, document=document, to_state=state, actor_id="worker"
            )
        document_id = document.id

    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    with caplog.at_level(logging.DEBUG):
        await orchestrator.handle_preprocess(
            {
                "document_id": str(document_id),
                "organization_id": str(ORG),
                "stream_id": str(STREAM),
                "stream_version_id": None,
                "config_fingerprint": "f" * 64,
            }
        )
        processed: set[uuid.UUID] = set()
        for _ in range(50):
            async with db.session_scope() as session:
                jobs = (
                    (
                        await session.execute(
                            select(Job).where(
                                Job.job_type == "document.stage",
                                Job.status == JobStatus.PENDING.value,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                pending = [job for job in jobs if job.id not in processed]
                if not pending:
                    break
                job = pending[0]
                processed.add(job.id)
                payload = dict(job.payload)
            await orchestrator.handle_stage(payload)

    # The sweep is only meaningful if the pipeline actually ran to its
    # end — the synthetic order reaches APPROVED (PRC-012).
    from soa_db.documents import DocumentRepository

    async with db.session_scope() as session:
        processed = await DocumentRepository(session, CONTEXT).get(document_id)
        assert processed is not None and processed.state == "approved"

    canaries = {f"document value {value!r}": value for value in DOCUMENT_VALUE_CANARIES}
    canaries["document bytes"] = "SO-FIXTURE-001"
    assert_no_canaries(formatted_log_output(caplog.records), canaries, channel="worker logs")

    # Job rows travel through the queue and job-admin API — payloads and
    # error summaries must reference the document, never quote it.
    async with db.session_scope() as session:
        rows = (await session.execute(select(Job))).scalars().all()
        job_dump = str([(row.payload, row.last_error) for row in rows])
    assert_no_canaries(job_dump, canaries, channel="job rows")
