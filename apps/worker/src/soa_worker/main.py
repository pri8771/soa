"""Worker entry point: ``python -m soa_worker.main`` or ``soa-worker``."""

import asyncio
import logging
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps

import httpx

from soa_config.logging import configure_logging
from soa_config.telemetry import configure_telemetry
from soa_db import DatabaseSessions, create_database_engine
from soa_db.deletion_requests import DOCUMENT_DELETION_JOB_TYPE
from soa_db.uploads import UPLOAD_CLEANUP_JOB_TYPE
from soa_storage import ObjectStore
from soa_storage.secrets_gcp import build_secret_store
from soa_worker.database_queue import DatabaseJobQueue
from soa_worker.document_deletion import execute_document_deletion
from soa_worker.domain_job_failures import DomainJobFailureCoordinator
from soa_worker.export_orchestrator import execute_export
from soa_worker.external_cleanup import (
    ExternalCleanupCoordinator,
    register_external_cleanup_handler,
)
from soa_worker.job_metrics import JobMetrics
from soa_worker.model_usage import TokenPricing
from soa_worker.orchestrator import STAGE_JOB_TYPE, Orchestrator
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.run_config import ResolvedExecutorFactory
from soa_worker.secret_revoke import register_secret_revoke_handler
from soa_worker.settings import WorkerSettings, load_settings
from soa_worker.upload_cleanup import cleanup_expired_upload
from soa_worker.worker import Worker

PREPROCESS_JOB_TYPE = "document.preprocess"
EXPORT_JOB_TYPE = "export.deliver"
EVALUATION_JOB_TYPE = "evaluation.run"
OUTBOX_JOB_TYPE = "outbox.publish"
DATA_EXPORT_JOB_TYPE = "data_export.build"
DATA_EXPORT_CLEANUP_JOB_TYPE = "data_export.cleanup"

logger = logging.getLogger(__name__)


def require_tenant_job(job: JobEnvelope) -> uuid.UUID:
    """Return trusted queue tenant after fencing the payload copy.

    The queue row is trusted metadata; payload JSON is not. Every tenant job
    carries both so a corrupt/replayed payload cannot make a worker bind a
    different RLS tenant or mutate another tenant's storage.
    """

    if job.organization_id is None:
        raise ValueError(f"{job.job_type} job is missing its trusted tenant id")
    try:
        payload_organization_id = uuid.UUID(str(job.payload["organization_id"]))
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"{job.job_type} job has no valid tenant payload") from error
    if payload_organization_id != job.organization_id:
        raise ValueError(f"{job.job_type} tenant payload does not match its queue envelope")
    return job.organization_id


TenantJobHandler = Callable[[JobEnvelope, uuid.UUID], Awaitable[None]]


def register_tenant_handler(
    registry: HandlerRegistry, job_type: str
) -> Callable[[TenantJobHandler], TenantJobHandler]:
    """Register a handler behind the mandatory queue/payload tenant fence."""

    def decorator(handler: TenantJobHandler) -> TenantJobHandler:
        @wraps(handler)
        async def trusted(job: JobEnvelope) -> None:
            await handler(job, require_tenant_job(job))

        registry.register(job_type)(trusted)
        return handler

    return decorator


def _register_extraction_providers(settings: WorkerSettings) -> None:
    """Register optional local and credential-capable hosted providers.

    Hosted factories accept either a deployment key or the exact tenant
    secret pinned to a run; with neither, provider construction fails closed
    before any document leaves the deployment (AIO-006).

    Local first (AIO-007), then the BYO hosted keys (AIO-008). Local and
    hosted can coexist — the router (AIO-013) picks among them per the
    tenant's data policy."""
    if settings.local_llm_endpoint:
        from soa_worker.llm_extraction import register_local_llm_extraction

        register_local_llm_extraction(
            settings.local_llm_endpoint,
            settings.local_llm_model,
            timeout_seconds=settings.local_llm_timeout_seconds,
        )

    from soa_worker.anthropic_extraction import register_anthropic_extraction
    from soa_worker.gemini_extraction import register_gemini_extraction

    # Register hosted capabilities even when the deployment has no shared key:
    # the run's pinned tenant SecretReference supplies the credential at use time.
    register_anthropic_extraction(
        settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None,
        model=settings.anthropic_model,
        pricing=TokenPricing(
            reference=settings.anthropic_pricing_reference,
            input_cents_per_million=settings.anthropic_input_cents_per_million,
            output_cents_per_million=settings.anthropic_output_cents_per_million,
        ),
    )
    register_gemini_extraction(
        settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None,
        model=settings.gemini_model,
        pricing=TokenPricing(
            reference=settings.gemini_pricing_reference,
            input_cents_per_million=settings.gemini_input_cents_per_million,
            output_cents_per_million=settings.gemini_output_cents_per_million,
        ),
    )

    if settings.hosted_openai_endpoint:
        from soa_worker.llm_extraction import register_hosted_openai_extraction

        register_hosted_openai_extraction(
            name=settings.hosted_openai_provider_name,
            endpoint=settings.hosted_openai_endpoint,
            model=settings.hosted_openai_model,
            api_key=(
                settings.hosted_openai_api_key.get_secret_value()
                if settings.hosted_openai_api_key
                else None
            ),
            region=settings.hosted_openai_region,
            pricing=TokenPricing(
                reference=settings.hosted_openai_pricing_reference,
                input_cents_per_million=settings.hosted_openai_input_cents_per_million,
                output_cents_per_million=settings.hosted_openai_output_cents_per_million,
            ),
        )


def _build_object_store(settings: WorkerSettings) -> ObjectStore:
    """Build the same object store the API writes to, so the worker reads
    the originals it uploaded and writes derived artifacts alongside."""
    if settings.storage_backend == "filesystem":
        from soa_storage.filesystem import FilesystemObjectStore

        return FilesystemObjectStore(root=settings.storage_filesystem_root)
    if settings.storage_backend == "gcs":
        from soa_storage.gcs import GcsObjectStore, GcsSettings

        if not settings.storage_gcs_project:
            raise ValueError("worker GCS storage requires SOA_WORKER_STORAGE_GCS_PROJECT")
        return GcsObjectStore(
            GcsSettings(
                bucket=settings.storage_bucket,
                project=settings.storage_gcs_project,
                kms_key_name=settings.storage_gcs_kms_key_name,
            )
        )
    from soa_storage.s3 import S3ObjectStore, S3Settings

    if not (
        settings.storage_endpoint_url
        and settings.storage_access_key
        and settings.storage_secret_key
    ):
        raise ValueError(
            "worker S3 storage requires SOA_WORKER_STORAGE_ENDPOINT_URL, "
            "SOA_WORKER_STORAGE_ACCESS_KEY, and SOA_WORKER_STORAGE_SECRET_KEY"
        )
    return S3ObjectStore(
        S3Settings(
            endpoint_url=settings.storage_endpoint_url,
            access_key=settings.storage_access_key,
            secret_key=settings.storage_secret_key,
            bucket=settings.storage_bucket,
            region=settings.storage_region,
            force_path_style=settings.storage_force_path_style,
            sse=settings.storage_sse,
            sse_kms_key_id=settings.storage_sse_kms_key_id,
        )
    )


async def _run() -> None:
    settings = load_settings()
    configure_logging(
        service_name=settings.service_name,
        environment=settings.environment.value,
        level="DEBUG" if settings.debug else "INFO",
    )
    telemetry = configure_telemetry(
        service_name=settings.service_name,
        environment=settings.environment.value,
        profile=settings.telemetry_profile,
        otlp_endpoint=settings.otlp_endpoint,
    )
    _register_extraction_providers(settings)
    db = DatabaseSessions(create_database_engine(settings.database_url.get_secret_value()))
    store = _build_object_store(settings)
    if hasattr(store, "ensure_bucket"):
        await store.ensure_bucket()
    secret_store = build_secret_store(
        backend=settings.secrets_backend,
        directory=settings.secrets_directory,
        aws_region=settings.secrets_aws_region,
        gcp_project=settings.secrets_gcp_project,
    )
    orchestrator = Orchestrator(
        db,
        {},
        executor_resolver=ResolvedExecutorFactory(store, secret_store),
    )
    registry = HandlerRegistry()
    register_secret_revoke_handler(registry, db, secret_store)
    register_external_cleanup_handler(registry, db, store, secret_store)

    @register_tenant_handler(registry, PREPROCESS_JOB_TYPE)
    async def preprocess(job: JobEnvelope, _organization_id: uuid.UUID) -> None:
        await orchestrator.handle_preprocess(job.payload)

    @register_tenant_handler(registry, STAGE_JOB_TYPE)
    async def stage(job: JobEnvelope, _organization_id: uuid.UUID) -> None:
        await orchestrator.handle_stage(job.payload)

    @register_tenant_handler(registry, UPLOAD_CLEANUP_JOB_TYPE)
    async def cleanup_upload(job: JobEnvelope, organization_id: uuid.UUID) -> None:
        await cleanup_expired_upload(
            db,
            store,
            organization_id=organization_id,
            upload_session_id=uuid.UUID(str(job.payload["upload_session_id"])),
        )

    @register_tenant_handler(registry, DOCUMENT_DELETION_JOB_TYPE)
    async def delete_document(job: JobEnvelope, organization_id: uuid.UUID) -> None:
        await execute_document_deletion(
            db,
            store,
            organization_id=organization_id,
            deletion_request_id=uuid.UUID(str(job.payload["deletion_request_id"])),
        )

    @register_tenant_handler(registry, EXPORT_JOB_TYPE)
    async def export(job: JobEnvelope, organization_id: uuid.UUID) -> None:
        export_job_id = uuid.UUID(str(job.payload["export_job_id"]))
        from soa_db.repository import OrganizationContext

        async with httpx.AsyncClient(timeout=60.0) as client:
            result = await execute_export(
                db,
                store,
                OrganizationContext(organization_id=organization_id),
                export_job_id=export_job_id,
                client=client,
                allowlist=settings.export_destination_allowlist,
                timestamp=int(time.time()),
                secret_store=secret_store,
            )
        if result.outcome == "retryable_error":
            raise RuntimeError("export destination asked for a retry")

    @register_tenant_handler(registry, EVALUATION_JOB_TYPE)
    async def evaluation(job: JobEnvelope, organization_id: uuid.UUID) -> None:
        from soa_db.repository import OrganizationContext
        from soa_db.tenant_guard import bind_tenant
        from soa_worker.evaluation_orchestrator import execute_evaluation

        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            await execute_evaluation(
                session,
                OrganizationContext(organization_id=organization_id),
                uuid.UUID(str(job.payload["evaluation_run_id"])),
                store=store,
                secret_store=secret_store,
            )

    @registry.register(OUTBOX_JOB_TYPE)
    async def publish_outbox(job: JobEnvelope) -> None:
        from soa_worker.outbox_publisher import publish_outbox_event

        if not settings.outbox_publish_url:
            if not settings.is_development_like:
                raise RuntimeError("outbox publication destination is not configured")
            # Local development intentionally has no external event receiver.
            # Acknowledge the queue intent while retaining the OutboxEvent in
            # pending state; an operator can configure a receiver and use the
            # audited replay endpoint later. Production settings fail startup
            # before this path can be reached.
            logger.info(
                "outbox event retained without delivery in local environment",
                extra={"outbox_event_id": str(job.payload.get("outbox_event_id", "unknown"))},
            )
            return
        async with httpx.AsyncClient(timeout=30.0) as client:
            result = await publish_outbox_event(
                db,
                event_id=uuid.UUID(str(job.payload["outbox_event_id"])),
                expected_organization_id=job.organization_id,
                destination_url=settings.outbox_publish_url,
                client=client,
                signing_secret=(
                    settings.outbox_signing_secret.get_secret_value()
                    if settings.outbox_signing_secret
                    else None
                ),
            )
        if result.outcome == "retryable_error":
            raise RuntimeError("outbox destination asked for a retry")

    @register_tenant_handler(registry, DATA_EXPORT_JOB_TYPE)
    async def build_data_export(job: JobEnvelope, organization_id: uuid.UUID) -> None:
        from soa_db.repository import OrganizationContext
        from soa_db.tenant_guard import bind_tenant
        from soa_worker.data_export_orchestrator import execute_data_export_batch

        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            await execute_data_export_batch(
                session,
                store,
                OrganizationContext(organization_id=organization_id),
                export_id=uuid.UUID(str(job.payload["data_export_id"])),
            )

    @register_tenant_handler(registry, DATA_EXPORT_CLEANUP_JOB_TYPE)
    async def cleanup_data_export(job: JobEnvelope, organization_id: uuid.UUID) -> None:
        from soa_db.repository import OrganizationContext
        from soa_db.tenant_guard import bind_tenant
        from soa_worker.data_export_orchestrator import (
            cleanup_expired_data_export,
            delete_export_objects,
        )

        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            if job.payload.get("data_export_id"):
                await cleanup_expired_data_export(
                    session,
                    store,
                    OrganizationContext(organization_id=organization_id),
                    export_id=uuid.UUID(str(job.payload["data_export_id"])),
                )
            else:
                raw_keys = job.payload.get("object_keys", [])
                if not isinstance(raw_keys, list) or not all(
                    isinstance(key, str) for key in raw_keys
                ):
                    raise ValueError("data export cleanup requires a bounded object key list")
                # No database state participates in an orphan-key cleanup;
                # release its otherwise empty transaction before storage I/O.
                await session.commit()
                await delete_export_objects(store, raw_keys, organization_id=organization_id)

    worker_id = f"{socket.gethostname()}:{uuid.uuid4()}"
    domain_failures = DomainJobFailureCoordinator(db)
    external_cleanups = ExternalCleanupCoordinator(db)

    async def reconcile_terminal_failures() -> int:
        domain_count = await domain_failures.reconcile()
        cleanup_count = await external_cleanups.reconcile()
        return domain_count + cleanup_count

    queue = DatabaseJobQueue(
        db,
        worker_id=worker_id,
        reconcile_terminal_failures=reconcile_terminal_failures,
        metrics=JobMetrics(telemetry),
        observation_interval_seconds=settings.queue_observation_interval_seconds,
        lock_recovery_interval_seconds=settings.lock_recovery_interval_seconds,
    )
    worker = Worker(
        settings,
        registry,
        fetch_job=queue.claim,
        telemetry=telemetry,
        on_job_succeeded=queue.succeeded,
        on_job_failed=queue.failed,
        on_job_terminal_failure=domain_failures.handle,
        heartbeat_job=queue.heartbeat,
    )
    worker.install_signal_handlers(asyncio.get_running_loop())
    try:
        await worker.run()
    finally:
        telemetry.shutdown()
        await db.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
