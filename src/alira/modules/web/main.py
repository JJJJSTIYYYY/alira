"""Declaration-only entry point for the web module."""

from alira._version import __version__
from alira.kernel.module.base_module import BaseModule


class WebModule(BaseModule):
    def __init__(self):
        super().__init__(name="web", version=__version__, description="Alira HTTP service")
        self._runtime = None

    async def startup(self) -> None:
        from alira.modules.web.src.runtime import WebRuntime

        self._runtime = WebRuntime()
        await self._runtime.start()

    async def shutdown(self) -> None:
        if self._runtime is not None:
            await self._runtime.stop()
            self._runtime = None


module = WebModule()
