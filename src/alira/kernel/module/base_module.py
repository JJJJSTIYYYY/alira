"""Module declarations and application lifecycle hooks."""

from pathlib import Path


class BaseModule:
    """Declare metadata without importing the module's business packages.

    ``requirements_txt`` is absolute or relative to ``alira/modules``. When
    omitted, the manager looks for requirements.txt beside the module's main.py.
    Only startup() and shutdown() are implemented by modules; package operations
    belong to ModuleManager.
    """

    _initialized: bool = False

    def __init__(
        self,
        name: str,
        version: str,
        *,
        description: str = "",
        required_modules: list[str] | None = None,
        requirements_txt: str | Path | None = None,
        author: str | None = None,
    ):
        self.name = name
        self.version = version
        self.description = description
        self.required_modules = required_modules or []
        self.requirements_txt = requirements_txt
        self.author = author
        self._initialized = True

    async def startup(self) -> None:
        """Import business packages and start services; return when ready."""

    async def shutdown(self) -> None:
        """Release acquired resources, including partial startup; no-op by default."""
