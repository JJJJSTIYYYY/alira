"""Plugin communication API: Apixis public exports and the kernel service."""

from apixis import *
from apixis import __all__ as _apixis_exports

from alira.kernel.comlink.apixis_core import ApixisCore, apixis_core

__all__ = ["ApixisCore", "apixis_core"]
__all__.extend(_apixis_exports)
