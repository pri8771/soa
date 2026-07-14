"""Worker entry point: ``python -m soa_worker.main`` or ``soa-worker``."""

import asyncio

from soa_config.logging import configure_logging
from soa_worker.registry import HandlerRegistry
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
    registry = HandlerRegistry()
    worker = Worker(settings, registry)
    worker.install_signal_handlers(asyncio.get_running_loop())
    await worker.run()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
