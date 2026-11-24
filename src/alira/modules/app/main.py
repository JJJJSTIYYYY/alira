"""Module declaration and lifecycle hooks."""

from alira.kernel.module import BaseModule, ModuleStatus

__version__ = "0.1.0"


class Module(BaseModule):
    async def startup(self) -> None:
        """Import business code and acquire module resources here."""

    async def shutdown(self) -> None:
        """Release resources, including those acquired during partial startup."""

    async def health(self) -> ModuleStatus:
        """Inspect owned resources and report the module's current health here."""
        return await super().health()


module = Module(
    name='app',
    version=__version__,
    required_modules=[],
    required_kernel=">=3.0.0",
)
