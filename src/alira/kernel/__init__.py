"""Kernel lifecycle services and module management APIs."""

from alira.kernel.lifespan.auto_init import (
    ApixisCore,
    AutoInit,
    LifecycleService,
    apixis_core,
    auto_init,
)
from alira.kernel.lifespan.resource_cleaner import ResourceCleaner, resource_cleaner
from alira.kernel.module import BaseModule, ModuleManager, module_manager
from alira.kernel.module.dependencies import DependencyError, DependencyInfo
from alira.kernel.initialize import lifespan

__all__ = [
    "ApixisCore",
    "AutoInit",
    "BaseModule",
    "DependencyError",
    "DependencyInfo",
    "LifecycleService",
    "ModuleManager",
    "ResourceCleaner",
    "apixis_core",
    "auto_init",
    "lifespan",
    "module_manager",
    "resource_cleaner",
]
