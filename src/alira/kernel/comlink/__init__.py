"""Plugin communication API: Apixis public exports and the kernel service."""

from apixis import *
from apixis import __all__ as _apixis_exports

from alira.kernel.comlink.bus import ApixisCore, bus
from alira.kernel.comlink.decorator import ComlinkData, comlink

__all__ = ["ApixisCore", "ComlinkData", "bus", "comlink"]
__all__.extend(_apixis_exports)
