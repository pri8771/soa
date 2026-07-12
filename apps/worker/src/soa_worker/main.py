"""Worker entry point: ``python -m soa_worker.main`` or ``soa-worker``."""

import asyncio
import logging

from soa_worker.registry import HandlerRegistry
from soa_worker.settings import load_settings
from soa_worker.worker import Worker


async def _run() -> None:
    settings = load_settings()
    registry = HandlerRegistry()
    worker = Worker(settings, registry)
    worker.install_signal_handlers(asyncio.get_running_loop())
    await worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
