"""Public dependency contracts and actionable module errors."""

from dataclasses import dataclass
from pathlib import Path


class DependencyError(RuntimeError):
    """Dependency failure with optional module ownership and a repair command."""

    def __init__(
        self,
        message: str,
        *,
        module: str | None = None,
        missing: str | None = None,
        hint: str | None = None,
    ):
        self.module = module
        self.missing = missing
        self.hint = hint
        prefix = f"module '{module}': " if module else ""
        suffix = f"\nRepair: {hint}" if hint else ""
        super().__init__(prefix + message + suffix)


class ModuleLoadError(DependencyError):
    """A module's deferred business imports failed during startup."""


@dataclass(frozen=True)
class DependencyInfo:
    name: str
    version: str
    modules: frozenset[str]
    preinstalled: bool

    @property
    def ref_count(self) -> int:
        return len(self.modules)


@dataclass(frozen=True)
class ModuleRequirements:
    lockfile: Path | None
    packages: dict[str, str]


def sync_hint() -> str:
    return "uv run alira sync"
