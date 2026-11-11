"""Kernel service ownership and registration order."""

from contextlib import asynccontextmanager

from alira.kernel.lifespan import apixis_core, auto_init, resource_cleaner
from alira.kernel.module import module_manager
from alira.shared.logger import logger


def initialize() -> None:
    """Register kernel singletons in startup order, once by identity."""
    for service in (
        logger,
        apixis_core,
        resource_cleaner,
        module_manager,
    ):
        auto_init.register(service)


@asynccontextmanager
async def lifespan():
    """Start application services and stop them in reverse registration order."""
    initialize()
    try:
        await auto_init.start()
        yield
    finally:
        await auto_init.stop()
