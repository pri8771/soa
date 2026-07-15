"""Worker entry point: ``python -m soa_worker.main`` or ``soa-worker``."""

import asyncio

from soa_config.logging import configure_logging
from soa_db import DatabaseSessions, create_database_engine
from soa_storage.store import ObjectStore
from soa_worker.settings import WorkerSettings, load_settings
from soa_worker.worker import Worker


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


def _build_object_store(settings: WorkerSettings) -> ObjectStore:
    """Build the same object store the API writes to, so the worker reads
    the originals it uploaded and writes derived artifacts alongside."""
    if settings.storage_backend == "filesystem":
        from soa_storage.filesystem import FilesystemObjectStore

        return FilesystemObjectStore(root=settings.storage_filesystem_root)
    if settings.storage_backend == "gcs" and settings.storage_gcs_project:
        from soa_storage.gcs import GcsObjectStore, GcsSettings

        return GcsObjectStore(
            GcsSettings(bucket=settings.storage_bucket, project=settings.storage_gcs_project)
        )
    if settings.storage_backend == "s3" and settings.storage_endpoint_url:
        from soa_storage.s3 import S3ObjectStore, S3Settings

        return S3ObjectStore(
            S3Settings(
                endpoint_url=settings.storage_endpoint_url,
                access_key=settings.storage_access_key or "",
                secret_key=settings.storage_secret_key or "",
                bucket=settings.storage_bucket,
                region=settings.storage_region,
                force_path_style=settings.storage_force_path_style,
            )
        )
    from soa_storage.memory import MemoryObjectStore

    return MemoryObjectStore()


async def _run() -> None:
    settings = load_settings()
    configure_logging(
        service_name=settings.service_name,
        environment=settings.environment.value,
        level="DEBUG" if settings.debug else "INFO",
    )
    _register_extraction_providers(settings)

    # Assemble the processing pipeline (PRC-003) and turn on the DB-backed
    # claim loop (JOB-003/004/005). Extraction uses the deterministic mock
    # provider (PRC-006) — swapping in the AIO provider router is a
    # follow-on that resolves the per-stream provider policy.
    from soa_worker.extraction.mock import MockExtractionProvider
    from soa_worker.job_runner import DbJobProcessor
    from soa_worker.orchestrator import STAGE_JOB_TYPE, Orchestrator
    from soa_worker.pipeline import build_executors

    engine = create_database_engine(settings.database_url.get_secret_value())
    db = DatabaseSessions(engine)
    store = _build_object_store(settings)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    processor = DbJobProcessor(
        db,
        {
            "document.preprocess": orchestrator.handle_preprocess,
            STAGE_JOB_TYPE: orchestrator.handle_stage,
        },
    )
    registry = processor.build_registry()
    worker = Worker(settings, registry, fetch_job=processor.fetch_job)
    worker.install_signal_handlers(asyncio.get_running_loop())
    await worker.run()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
