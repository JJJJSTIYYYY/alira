"""Kernel lifecycle services and module management APIs."""

from alira.kernel.comlink import ApixisCore, apixis_core
from alira.kernel.initialize import lifespan
from alira.kernel.lifespan import (
    AutoInit,
    LifecycleService,
    ResourceCleaner,
    auto_init,
    resource_cleaner,
)
from alira.kernel.module import (
    BaseModule,
    DependencyError,
    DependencyInfo,
    ModuleLoadError,
    ModuleManager,
    module_manager,
)

__all__ = [
    "ApixisCore",
    "AutoInit",
    "BaseModule",
    "DependencyError",
    "DependencyInfo",
    "LifecycleService",
    "ModuleLoadError",
    "ModuleManager",
    "ResourceCleaner",
    "apixis_core",
    "auto_init",
    "lifespan",
    "module_manager",
    "resource_cleaner",
]
