import importlib
import subprocess
import sys

from alira.kernel.comlink import apixis_core
from alira.kernel.lifespan.resource_cleaner import resource_cleaner
from alira.kernel.module import ModuleManager, module_manager
from alira.shared.logger import logger

initialization = importlib.import_module("alira.kernel.initialize")


def test_initialize_registers_kernel_singletons_once_in_order(monkeypatch, auto_init_factory):
    registry = auto_init_factory()
    monkeypatch.setattr(initialization, "auto_init", registry)
    initialization.initialize()
    initialization.initialize()
    expected = [logger, apixis_core, resource_cleaner, module_manager]
    assert len(registry._services) == len(expected)
    assert all(actual is service for actual, service in zip(registry._services, expected))
    assert ModuleManager() is module_manager


def test_kernel_import_order_does_not_register_or_import_legacy_services():
    subprocess.run(
        [sys.executable, "-c", (
            "import sys\n"
            "from alira.kernel.module import ModuleManager, module_manager\n"
            "from alira.kernel.lifespan.resource_cleaner import resource_cleaner\n"
            "from alira.kernel.comlink import apixis_core\n"
            "from alira.kernel.lifespan import auto_init\n"
            "from alira.shared.logger import logger\n"
            "assert auto_init._services == []\n"
            "from alira.kernel.initialize import initialize\n"
            "initialize()\n"
            "assert auto_init._services == [logger, apixis_core, resource_cleaner, module_manager]\n"
            "assert 'alira.modules.database.src' not in sys.modules\n"
            "assert 'alira.modules.tool.src.mcp' not in sys.modules\n"
            "assert ModuleManager() is module_manager\n"
        )], check=True, capture_output=True,
    )


def test_repeated_manager_construction_preserves_running_state(tmp_path, monkeypatch):
    from alira.kernel.module import BaseModule

    module = BaseModule("example", "1")
    manager = module_manager
    monkeypatch.setattr(manager, "modules_dir", tmp_path)
    monkeypatch.setattr(manager._registry, "modules", {"example": module})
    monkeypatch.setattr(manager._runtime, "loaded", {"example"})
    assert ModuleManager() is manager
    assert manager.modules_dir == tmp_path
    assert manager.modules["example"] is module
    assert manager.loaded == {"example"}


def test_module_manager_factory_preserves_process_singleton(module_manager_factory):
    first = module_manager_factory()
    second = module_manager_factory()
    assert first is not second
    assert first is not module_manager
    assert second is not module_manager
    assert first._registry.modules is not second._registry.modules
    assert first._dependencies.claims is not second._dependencies.claims
    assert ModuleManager() is module_manager
