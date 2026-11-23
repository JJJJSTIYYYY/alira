"""Kernel service ownership and registration order."""

from contextlib import asynccontextmanager

from alira.kernel.comlink import apixis_core
from alira.kernel.lifespan import auto_init, resource_cleaner
from alira.kernel.module import module_manager
from alira.shared.logger import logger


def initialize() -> None:
    """Register kernel-owned singletons in their required startup order.

    Registers Logger, ApixisCore, ResourceCleaner and ModuleManager. Registration
    is idempotent by service identity and does not invoke their start hooks.
    Applications normally enter lifespan instead of calling hooks individually.
    """
    for service in (
        logger,
        apixis_core,
        resource_cleaner,
        module_manager,
    ):
        auto_init.register(service)


@asynccontextmanager
async def lifespan():
    """Enter the kernel service lifecycle and guarantee a stop attempt on exit.

    Yields:
        None after registered service starts finish. Modules are discovered by
        ModuleManager.start but are not loaded until ModuleManager.load is called.

    Raises:
        Exception: A kernel service failed to start, logged before propagation.
        BaseException: Startup or the context body failed or was cancelled. The
            finally block still closes registered services in reverse order.

    Notes:
        Kernel services stop in reverse registration order, including after partial
        startup. Ordinary shutdown failures are logged individually while remaining
        services continue closing. Deployed sources and dependency records remain.
    """
    initialize()
    try:
        await auto_init.start()
        yield
    finally:
        await auto_init.stop()
