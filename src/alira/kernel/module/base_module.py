"""Module declarations and application lifecycle hooks."""

from pathlib import Path

ALIRA_DIR = Path(__file__).resolve().parents[2]


class BaseModule:
    """Declare metadata without importing the module's business packages.

    ``requirements_txt`` is absolute or relative to ``alira/modules``. When
    omitted, the manager looks for requirements.txt beside the module's main.py.
    Only startup() and shutdown() are implemented by modules; package operations
    belong to ModuleManager.
    """

    def __init__(
        self,
        name: str,
        version: str,
        *,
        description: str = "",
        required_modules: list[str] | None = None,
        requirements_txt: str | Path | None = None,
        author: str | None = None,
        logo_banner: str | None = None,
    ):
        self.name = name
        self.version = version
        self.description = description
        self.required_modules = required_modules or []
        self.requirements_txt = requirements_txt
        self.author = author
        self.logo_banner = logo_banner

    async def startup(self) -> None:
        """Import business packages and start services; return when ready."""
        if self.logo_banner:
            print(self.logo_banner)

    async def shutdown(self) -> None:
        """Release services, including resources acquired by a failed startup."""
        raise NotImplementedError("Subclasses should implement shutdown().")
