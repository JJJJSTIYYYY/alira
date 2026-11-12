from alira._version import __version__
from alira.kernel.module.base_module import BaseModule


class AgentModule(BaseModule):
    def __init__(self):
        super().__init__(name="agent", version=__version__, description="Alira agent service")
        self._runtime = None

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass