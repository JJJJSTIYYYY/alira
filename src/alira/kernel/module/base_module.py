"""Module declarations and application lifecycle hooks."""

from pathlib import Path


class BaseModule:
    """Declarative module metadata and asynchronous runtime hooks.

    Create exactly one initialized instance in a module's ``main.py``. Its name
    must match the containing directory. Constructors declare metadata only;
    defer business imports, event subscriptions and resource allocation to
    ``startup``. ModuleManager owns source and Python dependency installation.

    Attributes:
        name: Module identifier used in dependency declarations and CLI commands.
        version: Module version supplied by its author.
        description: Human-readable purpose of the module.
        required_modules: Prerequisite module names, ready before this module starts.
        requirements_txt: Explicit requirements path, or None for automatic lookup.
        author: Optional author attribution.
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
        """Store metadata and mark the declaration initialized.

        Args:
            name: Module name matching its directory and a valid Python identifier.
            version: Module version string.
            description: Optional human-readable description.
            required_modules: Prerequisite module names, not Python distribution names.
                None or an empty list means no prerequisites. A nonempty list is stored
                by reference; callers should not mutate it while modules are running.
            requirements_txt: Absolute path or path relative to ``alira/modules``.
                None selects ``requirements.txt`` beside ``main.py``; absence of that
                default file means no Python requirements. An explicit missing file
                fails when the manager resolves or loads the module.
            author: Optional author attribution.

        Notes:
            This constructor performs no imports, dependency resolution or file I/O.
            Metadata is validated by discovery and deployment boundaries. For static
            CLI inspection, declare names, prerequisites and requirements paths as
            literals or supported top-level constants; use a string for an explicit
            requirements path in ``main.py``.
        """
        self.name = name
        self.version = version
        self.description = description
        self.required_modules = required_modules or []
        self.requirements_txt = requirements_txt
        self.author = author
        self._initialized = True

    async def startup(self) -> None:
        """Acquire runtime resources and return once the module is ready.

        The manager calls this hook after prerequisite modules are ready and Python
        dependencies have been verified. Independent modules may start concurrently.
        The default implementation does nothing. Override it to import business code,
        register event handlers and start services.

        Returns:
            None. Successful completion marks the module loaded.

        Raises:
            Exception: A startup failure, propagated through the manager's task group.
                An ImportError is reported as ModuleLoadError with a repair hint.

        Notes:
            Record acquired resources incrementally: shutdown may be called after
            this hook fails or is cancelled before completion.
        """

    async def shutdown(self) -> None:
        """Release resources acquired during complete or partial startup.

        Unregister event subscriptions, stop tasks and close resources owned by this
        module. Dependents are stopped first. The default implementation does nothing.

        Returns:
            None when cleanup has completed.

        Raises:
            Exception: Cleanup failed. The manager retains pending runtime state so
                a later unload or stop can retry this hook.

        Notes:
            Support repeated cleanup attempts and partially initialized state.
            Shutdown does not uninstall Python dependencies or clear import caches.
        """
