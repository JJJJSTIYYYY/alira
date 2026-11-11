"""Launch the application kernel and own its process signal handlers."""

import asyncio
import signal
from collections.abc import Sequence

from alira.kernel.initialize import lifespan
from alira.kernel.module import ModuleManager
from alira.shared.application.config import ALIRA_BASE_DIR
from alira.shared.application.version import print_logo
from alira.shared.logger import logger


async def run_application(
    manager: ModuleManager,
    modules: Sequence[str],
    *,
    install_missing: bool = False,
) -> None:
    print_logo()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    try:
        async with lifespan():
            logger.info(f"Application data directory: {ALIRA_BASE_DIR}")
            if modules:
                await manager.load(*modules, install_missing=install_missing)
            await stopped.wait()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
