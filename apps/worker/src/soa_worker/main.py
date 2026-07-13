"""Worker entry point: ``python -m soa_worker.main`` or ``soa-worker``."""

import asyncio

from soa_config.logging import configure_logging
from soa_worker.registry import HandlerRegistry
from soa_worker.settings import load_settings
from soa_worker.worker import Worker


async def _run() -> None:
    settings = load_settings()
    configure_logging(
        service_name=settings.service_name,
        environment=settings.environment.value,
        level="DEBUG" if settings.debug else "INFO",
    )
    if settings.local_llm_endpoint:
        # Optional profile (AIO-007): only a deployment that configured
        # an endpoint gets the local LLM extraction capability.
        from soa_worker.llm_extraction import register_local_llm_extraction

        register_local_llm_extraction(settings.local_llm_endpoint, settings.local_llm_model)
    registry = HandlerRegistry()
    worker = Worker(settings, registry)
    worker.install_signal_handlers(asyncio.get_running_loop())
    await worker.run()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
