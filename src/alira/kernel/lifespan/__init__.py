"""Kernel service lifecycle contracts and process-wide service instances."""

from alira.kernel.lifespan.apixis_core import ApixisCore, apixis_core
from alira.kernel.lifespan.auto_init import AutoInit, LifecycleService, auto_init
from alira.kernel.lifespan.resource_cleaner import ResourceCleaner, resource_cleaner

__all__ = [
    "ApixisCore",
    "AutoInit",
    "LifecycleService",
    "ResourceCleaner",
    "apixis_core",
    "auto_init",
    "resource_cleaner",
]
