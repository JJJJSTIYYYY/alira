import importlib
import subprocess
import sys

from alira.kernel.lifespan.auto_init import AutoInit, apixis_core
from alira.kernel.module import ModuleManager, module_manager
from alira.kernel.lifespan.resource_cleaner import resource_cleaner
from alira.shared.logger import logger

initialization = importlib.import_module("alira.kernel.initialize")


def test_initialize_registers_kernel_singletons_once_in_order(monkeypatch):
    class IsolatedAutoInit(AutoInit):
        _instance = None

    registry = IsolatedAutoInit()
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
            "from alira.kernel.lifespan.auto_init import auto_init, apixis_core\n"
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


async def test_repeated_manager_construction_preserves_running_state(tmp_path, module_manager_factory):
    from alira.kernel.module import BaseModule

    class Module(BaseModule):
        async def shutdown(self):
            pass

    manager = module_manager_factory(tmp_path)
    module = Module("example", "1")
    manager.register(module)
    await manager.load()
    repeated = type(manager)()
    assert repeated is manager
    assert repeated.modules_dir == tmp_path
    assert repeated.modules["example"] is module
    assert repeated.loaded == {"example"}
    await manager.stop()
