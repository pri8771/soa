"""Worker entry point: ``python -m soa_worker.main`` or ``soa-worker``."""

import asyncio
import socket
import time
import uuid

import httpx

from soa_config.logging import configure_logging
from soa_db import DatabaseSessions, create_database_engine
from soa_storage import ObjectStore
from soa_storage.secrets_gcp import build_secret_store
from soa_worker.database_queue import DatabaseJobQueue
from soa_worker.export_orchestrator import execute_export
from soa_worker.orchestrator import STAGE_JOB_TYPE, Orchestrator
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.run_config import ResolvedExecutorFactory
from soa_worker.settings import WorkerSettings, load_settings
from soa_worker.worker import Worker

PREPROCESS_JOB_TYPE = "document.preprocess"
EXPORT_JOB_TYPE = "export.deliver"


def _register_extraction_providers(settings: WorkerSettings) -> None:
    """Register the optional model-based extraction providers a
    deployment configured. Each is fail-closed: unset config means the
    provider simply does not exist (AIO-006), so an operator adds a
    capability by adding a key/endpoint, never by touching code.

    Local first (AIO-007), then the BYO hosted keys (AIO-008). Local and
    hosted can coexist — the router (AIO-013) picks among them per the
    tenant's data policy."""
    if settings.local_llm_endpoint:
        from soa_worker.llm_extraction import register_local_llm_extraction

        register_local_llm_extraction(settings.local_llm_endpoint, settings.local_llm_model)

    if settings.anthropic_api_key is not None:
        from soa_worker.anthropic_extraction import register_anthropic_extraction

        register_anthropic_extraction(
            settings.anthropic_api_key.get_secret_value(),
            model=settings.anthropic_model,
        )

    if settings.gemini_api_key is not None:
        from soa_worker.gemini_extraction import register_gemini_extraction

        register_gemini_extraction(
            settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
        )

    if settings.hosted_openai_api_key is not None and settings.hosted_openai_endpoint:
        from soa_worker.llm_extraction import register_hosted_openai_extraction

        register_hosted_openai_extraction(
            name=settings.hosted_openai_provider_name,
            endpoint=settings.hosted_openai_endpoint,
            model=settings.hosted_openai_model,
            api_key=settings.hosted_openai_api_key.get_secret_value(),
            region=settings.hosted_openai_region,
        )


async def _run() -> None:
    settings = load_settings()
    configure_logging(
        service_name=settings.service_name,
        environment=settings.environment.value,
        level="DEBUG" if settings.debug else "INFO",
    )
    _register_extraction_providers(settings)
    db = DatabaseSessions(create_database_engine(settings.database_url))
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
        executor_resolver=ResolvedExecutorFactory(store),
    )
    registry = HandlerRegistry()

    @registry.register(PREPROCESS_JOB_TYPE)
    async def preprocess(job: JobEnvelope) -> None:
        await orchestrator.handle_preprocess(job.payload)

    @registry.register(STAGE_JOB_TYPE)
    async def stage(job: JobEnvelope) -> None:
        await orchestrator.handle_stage(job.payload)

    @registry.register(EXPORT_JOB_TYPE)
    async def export(job: JobEnvelope) -> None:
        organization_id = uuid.UUID(str(job.payload["organization_id"]))
        export_job_id = uuid.UUID(str(job.payload["export_job_id"]))
        from soa_db.repository import OrganizationContext
        from soa_db.tenant_guard import bind_tenant

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with db.session_scope() as session:
                await bind_tenant(session, organization_id)
                result = await execute_export(
                    session,
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

    worker_id = f"{socket.gethostname()}:{uuid.uuid4()}"
    queue = DatabaseJobQueue(db, worker_id=worker_id)
    worker = Worker(
        settings,
        registry,
        fetch_job=queue.claim,
        on_job_succeeded=queue.succeeded,
        on_job_failed=queue.failed,
        heartbeat_job=queue.heartbeat,
    )
    worker.install_signal_handlers(asyncio.get_running_loop())
    try:
        await worker.run()
    finally:
        await db.dispose()


def _build_object_store(settings: WorkerSettings) -> ObjectStore:
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


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
