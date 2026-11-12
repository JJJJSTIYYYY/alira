"""Kernel service lifecycle contracts and process-wide service instances."""

from alira.kernel.lifespan.auto_init import AutoInit, LifecycleService, auto_init
from alira.kernel.lifespan.resource_cleaner import ResourceCleaner, resource_cleaner

__all__ = [
    "AutoInit",
    "LifecycleService",
    "ResourceCleaner",
    "auto_init",
    "resource_cleaner",
]
