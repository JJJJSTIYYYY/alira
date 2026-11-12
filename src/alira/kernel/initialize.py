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
        ExceptionGroup: Start or stop hooks failed; see AutoInit for retry state.
        BaseException: The context body failed or was cancelled. The finally block
            still attempts stop; a stop failure can replace that body exception.

    Notes:
        Kernel services stop in reverse registration order, including after partial
        startup. Deployed sources and Python dependency records are preserved.
    """
    initialize()
    try:
        await auto_init.start()
        yield
    finally:
        await auto_init.stop()
