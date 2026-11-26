"""Create source skeletons for new Alira modules without importing them."""

from pathlib import Path
from tempfile import TemporaryDirectory

from alira.kernel.module.dependencies.types import DependencyError
from alira.kernel.module.discovery.catalog import module_directory

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
    required_kernel=">=3.0.0",
)
'''


def initialize_module(modules_dir: Path, name: str) -> Path:
    """Create a minimal module beneath modules_dir and return its directory.

    Args:
        modules_dir: Destination containing module source packages, created if absent.
        name: Python module identifier accepted by module_directory.

    Returns:
        The new module directory with main.py, README.md, an empty requirements.txt,
        src/shared/events.py and an empty src/shared/model package, with package
        markers throughout. The shared package re-exports event definitions.
        Lifecycle hooks are editable no-ops; the health hook delegates to the
        base implementation until customized.

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
        (staged / "src/shared/model").mkdir(parents=True)
        files = {
            "__init__.py": "",
            "main.py": _MAIN_TEMPLATE.format(name=name),
            "requirements.txt": "",
            "README.md": f"Hello from {name}!",
            "src/__init__.py": "",
            "src/shared/__init__.py": "from .events import *\n",
            "src/shared/events.py": '"""Public event names and payload types for this module."""\n',
            "src/shared/model/__init__.py": "",
        }
        for relative, content in files.items():
            (staged / relative).write_text(content, encoding="utf-8")
        staged.rename(destination)
    return destination
