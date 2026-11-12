"""Public dependency contracts and actionable module errors."""

from dataclasses import dataclass
from pathlib import Path


class DependencyError(RuntimeError):
    """Base error for dependency deployment and module loading, with CLI-safe context.

    Attributes:
        module: Optional owning module name.
        missing: Optional missing/incompatible Python dependency identifier.
        hint: Optional repair command suitable for display to a user.

    str(error) includes the module prefix and repair hint when provided. The CLI
    reports this exception without an implementation traceback and exits nonzero.
    """

    def __init__(
        self,
        message: str,
        *,
        module: str | None = None,
        missing: str | None = None,
        hint: str | None = None,
    ):
        """Build an error message and retain structured ownership/repair fields.

        Args:
            message: Human-readable reason; avoid internal storage details in public errors.
            module: Owning module name or None when the error is not module-specific.
            missing: Dependency identifier or None; stored without changing the message.
            hint: Suggested repair command or None; appended as a Repair line.
        """
        self.module = module
        self.missing = missing
        self.hint = hint
        prefix = f"module '{module}': " if module else ""
        suffix = f"\nRepair: {hint}" if hint else ""
        super().__init__(prefix + message + suffix)


class ModuleLoadError(DependencyError):
    """A declaration, runtime dependency graph or deferred startup import is invalid.

    Inherits DependencyError's message/context constructor. Discovery and graph
    validation raise it directly; concurrent startup ImportError failures appear
    inside a task-group exception and carry module, missing and repair metadata.
    """


class DependencyResolutionError(DependencyError):
    """Requirements validation or dependency solving failed before record replacement.

    Inherits DependencyError's constructor. ModuleDependencies.rebuild uses this
    type before writing desired records or installing packages. Batch source
    installation recognizes it and attempts to withdraw newly published sources.
    """


@dataclass(frozen=True)
class DependencyInfo:
    """Immutable ownership summary for one locked Python distribution.

    Attributes:
        name: Canonicalized Python distribution name.
        version: Version recorded in the dependency lock, not an environment audit.
        modules: Immutable set of module names claiming this distribution.
        preinstalled: Whether the package belongs to the currently protected kernel
            closure, including uv when installed in this interpreter. This field
            does not mean every previously installed third-party package is protected.
    """

    name: str
    version: str
    modules: frozenset[str]
    preinstalled: bool

    @property
    def ref_count(self) -> int:
        """Return the number of distinct module owners, excluding kernel protection."""
        return len(self.modules)


@dataclass(frozen=True)
class ModuleRequirements:
    """One module's resolved Python package closure.

    Attributes:
        lockfile: Generated PEP 751 file, or None for a module without requirements.
            Resolver-generated paths belong to its caller-owned working directory.
        packages: Mapping of canonical distribution names to exact versions.
            The dataclass is frozen; this contained dictionary is not deep-frozen.
    """

    lockfile: Path | None
    packages: dict[str, str]


def sync_hint() -> str:
    """Return the standard user-facing command for source-driven dependency repair."""
    return "uv run alira sync"
