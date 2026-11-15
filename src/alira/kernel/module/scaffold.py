"""Create source skeletons for new Alira modules without importing them."""

from pathlib import Path
from tempfile import TemporaryDirectory

from alira.kernel.module.catalog import module_directory
from alira.kernel.module.dependencies.types import DependencyError


_MAIN_TEMPLATE = '''"""Module declaration and lifecycle hooks."""

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
    name={name!r},
    version=__version__,
    required_modules=[],
)
'''


def initialize_module(modules_dir: Path, name: str) -> Path:
    """Create a minimal module beneath modules_dir and return its directory.

    Args:
        modules_dir: Destination containing module source packages, created if absent.
        name: Python module identifier accepted by module_directory.

    Returns:
        The new module directory with main.py, an empty requirements.txt, package
        markers and src/shared/events.py. Lifecycle hooks are editable no-ops;
        the health hook delegates to the base implementation until customized.

    Raises:
        DependencyError: The name is invalid, the destination is a symlink or escapes
            modules_dir, or any file or directory already occupies the destination.
        OSError: Writing or publishing the scaffold failed.

    Notes:
        Files are prepared in temporary storage beneath modules_dir and published
        by directory rename. A write failure removes staging without publishing a
        partial module. No module imports, registrations or dependency changes occur.
    """
    destination = module_directory(modules_dir, name)
    if destination.exists():
        raise DependencyError(
            f"Module '{name}' already exists; existing sources are preserved"
        )
    modules_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".alira-init-", dir=modules_dir) as temporary:
        staged = Path(temporary) / name
        (staged / "src/shared").mkdir(parents=True)
        files = {
            "__init__.py": "",
            "main.py": _MAIN_TEMPLATE.format(name=name),
            "requirements.txt": "",
            "src/__init__.py": "",
            "src/shared/__init__.py": "",
            "src/shared/events.py": '"""Public event names and payload types for this module."""\n',
        }
        for relative, content in files.items():
            (staged / relative).write_text(content, encoding="utf-8")
        staged.rename(destination)
    return destination
