"""Plugin communication API: Apixis public exports and the kernel service."""

from apixis import *
from apixis import __all__ as _apixis_exports

from alira.kernel.comlink.apixis_core import ApixisCore, apixis_core
from alira.kernel.comlink.decorator import ComlinkData, comlink

__all__ = ["ApixisCore", "ComlinkData", "apixis_core", "comlink"]
__all__.extend(_apixis_exports)
