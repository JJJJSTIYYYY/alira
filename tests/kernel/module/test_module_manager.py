import asyncio
import importlib
import sys
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from alira.kernel.module import BaseModule

deployment_module = importlib.import_module(
    "alira.kernel.module.dependencies.deployment"
)


class Service(BaseModule):
    def __init__(self, name, calls, **kwargs):
        super().__init__(name, "1.0", **kwargs)
        self.startup = AsyncMock(side_effect=lambda: calls.append(f"start:{name}"))
        self.shutdown = AsyncMock(side_effect=lambda: calls.append(f"stop:{name}"))


@pytest.fixture
def manager(tmp_path, module_manager_factory):
    return module_manager_factory(tmp_path)


def test_discovery_skips_legacy_and_defers_business_imports(
    tmp_path, monkeypatch, module_manager_factory
):
    package = f"modules_{uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").touch()
    (root / "legacy").mkdir()
    (root / "legacy" / "__init__.py").write_text(
        "raise AssertionError('legacy imported')"
    )
    folder = root / "demo"
    folder.mkdir()
    (folder / "__init__.py").touch()
    (folder / "business.py").write_text("raise AssertionError('business imported')")
    (folder / "main.py").write_text(
        "from alira.kernel.module import BaseModule\n"
        "class Demo(BaseModule):\n"
        "    async def startup(self):\n"
        "        from . import business\n"
        "    async def shutdown(self): pass\n"
        "module = Demo('demo', '1.0')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    manager = module_manager_factory(root, package=package)
    assert list(manager.discover()) == ["demo"]
    assert manager.discover()["demo"] is manager.modules["demo"]


def test_discovery_rejects_ambiguous_declarations(
    tmp_path, monkeypatch, module_manager_factory
):
    package = f"modules_{uuid4().hex}"
    folder = tmp_path / package / "demo"
    folder.mkdir(parents=True)
    (folder.parent / "__init__.py").touch()
    (folder / "__init__.py").touch()
    (folder / "main.py").write_text(
        "from alira.kernel.module import BaseModule\n"
        "a = BaseModule('a', '1')\nb = BaseModule('b', '1')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ValueError, match="exactly one"):
        module_manager_factory(folder.parent, package=package).discover()


@pytest.mark.parametrize(
    "declaration",
    [
        "",
        "\n  \n",
        "# Module entry\n",
        "from alira.kernel.module import BaseModule\nclass Demo(BaseModule): pass\n",
        (
            "from alira.kernel.module import BaseModule\n"
            "class Demo(BaseModule):\n"
            "    def __init__(self): self.name = 'partial'\n"
            "module = Demo()\n"
        ),
    ],
)
def test_discovery_marks_entries_without_initialized_modules_unavailable(
    tmp_path,
    monkeypatch,
    declaration,
    module_manager_factory,
):
    package = f"modules_{uuid4().hex}"
    folder = tmp_path / package / "demo"
    folder.mkdir(parents=True)
    (folder.parent / "__init__.py").touch()
    (folder / "__init__.py").touch()
    (folder / "main.py").write_text(declaration)
    monkeypatch.syspath_prepend(str(tmp_path))
    manager = module_manager_factory(folder.parent, package=package)
    assert not manager.discover()
    assert manager.unavailable == {"demo"}
    assert f"{package}.demo.main" in sys.modules


def test_register_requires_completed_base_module_initialization(manager):
    class Incomplete(BaseModule):
        def __init__(self):
            self.name = "incomplete"

    with pytest.raises(TypeError, match="initialized BaseModule"):
        manager.register(Incomplete())


async def test_load_order_selected_closure_and_unload_protection(manager):
    calls = []
    for name, dependencies in [
        ("c", ["b"]),
        ("unrelated", []),
        ("b", ["a"]),
        ("a", []),
    ]:
        manager.register(Service(name, calls, required_modules=dependencies))
    await manager.load("c")
    await manager.load("c")
    assert calls == ["start:a", "start:b", "start:c"]
    with pytest.raises(ValueError, match="required by"):
        await manager.unload("a")
    await manager.stop()
    assert calls == ["start:a", "start:b", "start:c", "stop:c", "stop:b", "stop:a"]
    assert manager.loaded == frozenset()


async def test_independent_startups_are_parallel(manager):
    starting = {name: asyncio.Event() for name in ("a", "b")}

    for name in ("a", "b"):
        module = Service(name, [])

        async def startup(name=name):
            starting[name].set()
            await starting["b" if name == "a" else "a"].wait()

        module.startup.side_effect = startup
        manager.register(module)
    await asyncio.wait_for(manager.load(), 2)
    assert manager.loaded == {"a", "b"}
    await manager.stop()


async def test_dependent_starts_as_soon_as_its_prerequisite_is_ready(manager):
    child_started = asyncio.Event()
    slow = Service("slow", [])
    fast = Service("fast", [])
    child = Service("child", [], required_modules=["fast"])
    slow.startup.side_effect = child_started.wait
    child.startup.side_effect = child_started.set
    for module in (slow, fast, child):
        manager.register(module)
    await asyncio.wait_for(manager.load(), 2)
    await manager.stop()


@pytest.mark.parametrize(
    "dependencies, message", [(["missing"], "unknown modules"), (["a"], "cycle")]
)
async def test_invalid_graph_fails_before_install(
    manager, dependencies, message, monkeypatch
):
    manager.register(Service("a", [], required_modules=dependencies))
    install = AsyncMock()
    monkeypatch.setattr(deployment_module, "run_uv", install)
    with pytest.raises(ValueError, match=message):
        await manager.load()
    install.assert_not_awaited()


async def test_startup_failure_stops_partial_resources_and_blocks_dependents(manager):
    calls = []
    broken = Service("broken", calls)
    broken.startup.side_effect = RuntimeError("startup failed")
    child = Service("child", calls, required_modules=["broken"])
    manager.register(broken)
    manager.register(child)
    with pytest.raises(ExceptionGroup, match="unhandled errors"):
        await manager.load()
    broken.shutdown.assert_awaited_once()
    child.startup.assert_not_awaited()
    assert not manager.loaded
    assert not manager.dependencies
    broken.startup.side_effect = None
    await manager.load()
    await manager.stop()


async def test_cancellation_cleans_partial_startup(manager):
    entered = asyncio.Event()
    module = Service("a", [])

    async def startup():
        entered.set()
        await asyncio.Event().wait()

    module.startup.side_effect = startup
    manager.register(module)
    task = asyncio.create_task(manager.load())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    module.shutdown.assert_awaited_once()
    assert not manager.dependencies
    assert not manager.loaded


async def test_module_cannot_override_package_operations(manager):
    module = Service("a", [])
    module.install = AsyncMock(side_effect=AssertionError("module install called"))
    module.uninstall = AsyncMock(side_effect=AssertionError("module uninstall called"))
    manager.register(module)
    await manager.load()
    await manager.stop()
    module.install.assert_not_awaited()
    module.uninstall.assert_not_awaited()


async def test_missing_explicit_requirements_is_an_error(manager):
    manager.register(Service("a", [], requirements_txt="missing.txt"))
    with pytest.raises(FileNotFoundError, match="requirements file"):
        await manager.load()


async def test_failed_child_shutdown_keeps_prerequisite_running_for_retry(manager):
    calls = []
    parent = Service("parent", calls)
    child = Service("child", calls, required_modules=["parent"])
    child.shutdown.side_effect = [RuntimeError("not stopped"), None]
    manager.register(parent)
    manager.register(child)
    await manager.load()
    with pytest.raises(ExceptionGroup, match="Failed to unload modules"):
        await manager.stop()
    assert manager.loaded == {"parent", "child"}
    parent.shutdown.assert_not_awaited()
    await manager.stop()
    assert not manager.loaded
    parent.shutdown.assert_awaited_once()


async def test_start_only_discovers_without_validating_or_installing_dependencies(
    tmp_path,
    monkeypatch,
    module_manager_factory,
):
    package = f"modules_{uuid4().hex}"
    folder = tmp_path / package / "inactive"
    folder.mkdir(parents=True)
    (folder.parent / "__init__.py").touch()
    (folder / "__init__.py").touch()
    (folder / "main.py").write_text(
        "from alira.kernel.module import BaseModule\n"
        "module = BaseModule('inactive', '1', required_modules=['missing'], "
        "requirements_txt='missing.txt')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    manager = module_manager_factory(folder.parent, package=package)
    resolver = AsyncMock(
        side_effect=AssertionError("Dependencies resolved during discovery")
    )
    monkeypatch.setattr(deployment_module, "resolve_requirements", resolver)
    await manager.start()
    assert list(manager.modules) == ["inactive"]
    assert not manager.loaded
    assert not manager.dependencies
    resolver.assert_not_awaited()
    await manager.stop()


async def test_unselected_module_dependencies_do_not_block_loading_or_shutdown(manager):
    calls = []
    manager.register(Service("selected", calls))
    manager.register(
        Service(
            "inactive",
            calls,
            required_modules=["missing"],
            requirements_txt="missing.txt",
        )
    )
    await manager.start()
    await manager.load("selected")
    assert manager.loaded == {"selected"}
    await manager.stop()
    assert calls == ["start:selected", "stop:selected"]


async def test_startup_import_error_names_module_dependency_and_repair(manager):
    from alira.kernel.module.dependencies import ModuleLoadError

    module = Service("plugin", [])
    module.startup.side_effect = ModuleNotFoundError(
        "No module named 'fastapi'", name="fastapi"
    )
    manager.register(module)
    with pytest.raises(ExceptionGroup) as caught:
        await manager.load("plugin")
    error = caught.value.exceptions[0]
    assert isinstance(error, ModuleLoadError)
    assert error.module == "plugin"
    assert error.missing == "fastapi"
    assert error.hint == "uv run alira sync"
    assert "fastapi" in str(error)
    module.shutdown.assert_awaited_once()
    assert not manager.loaded
    assert not manager._runtime.startup_attempted
