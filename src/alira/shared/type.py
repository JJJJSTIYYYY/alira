"""Shared markers and errors for unavailable modules."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModuleNotLoaded:
    """Mark a value as unavailable because its owning module is not loaded.

    Args:
        module_name: Name of the module required to provide the value.

    The marker is false in boolean contexts. Inspect ``module_name`` to identify
    the unavailable module, or raise ``ModuleNotLoadedError(module_name)`` when
    the caller requires it to be loaded.
    """

    module_name: str

    def __bool__(self) -> bool:
        return False


class ModuleNotLoadedError(RuntimeError):
    """Report an operation that requires a module which is not loaded.

    Args:
        module_name: Name of the required module, retained on the exception.
    """

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        super().__init__(f"Module '{module_name}' is not loaded.")
