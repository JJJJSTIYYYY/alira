import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from alira.kernel.module import BaseModule
from alira.kernel.module.dependencies import ModuleLoadError

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


@pytest.fixture
def startup_errors(monkeypatch):
    errors = []

    def log(message):
        errors.append((message, sys.exception()))

    monkeypatch.setattr("alira.kernel.module.runtime.lifecycle.logger.exception", log)
    return errors


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


@pytest.mark.parametrize("declaration", [None, "", "module = BaseModule('demo', '1')"])
async def test_preflight_and_kernel_start_share_one_discovery(
    tmp_path, monkeypatch, module_manager_factory, declaration
):
    package = f"modules_{uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").touch()
    if declaration is not None:
        folder = root / "demo"
        folder.mkdir()
        (folder / "__init__.py").touch()
        (folder / "main.py").write_text(
            "from alira.kernel.module import BaseModule\n" + declaration
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    manager = module_manager_factory(root, package=package)
    expected = dict(manager.discover())
    unavailable = manager.unavailable
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "glob", Mock(side_effect=AssertionError("Repeated scan")))
        await manager.start()
        assert dict(manager.discover()) == expected
    assert manager.unavailable == unavailable


async def test_modules_without_requirements_skip_environment_verification(
    manager, monkeypatch
):
    module = Service("minimal", [])
    manager.register(module)
    for method in ("restore", "installed", "verify"):
        monkeypatch.setattr(
            manager._dependencies,
            method,
            Mock(side_effect=AssertionError("Unnecessary dependency verification")),
        )
    await manager.load("minimal")
    assert manager.loaded == {"minimal"}
    module.startup.assert_awaited_once()
    await manager.stop()


@pytest.mark.parametrize(
    "declaration, message",
    [
        ("a = BaseModule('a', '1')\nb = BaseModule('b', '1')\n", "exactly one"),
        (
            "module = BaseModule('other', '1')\n",
            "Module name 'other' does not match directory 'demo'",
        ),
    ],
)
def test_discovery_rejects_invalid_declarations(
    tmp_path, monkeypatch, module_manager_factory, declaration, message
):
    package = f"modules_{uuid4().hex}"
    folder = tmp_path / package / "demo"
    folder.mkdir(parents=True)
    (folder.parent / "__init__.py").touch()
    (folder / "__init__.py").touch()
    (folder / "main.py").write_text(
        "from alira.kernel.module import BaseModule\n" + declaration
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    manager = module_manager_factory(folder.parent, package=package)
    with pytest.raises(ModuleLoadError, match=message):
        manager.discover()
    assert not manager.modules
    assert not manager.loaded


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

    with pytest.raises(ModuleLoadError, match="initialized BaseModule"):
        manager.register(Incomplete())
    assert not manager.modules


@pytest.mark.parametrize("declaration", [None, object()])
def test_register_rejects_non_module_declarations(manager, declaration):
    with pytest.raises(ModuleLoadError, match="initialized BaseModule"):
        manager.register(declaration)
    assert not manager.modules


def test_register_rejects_duplicate_names_but_allows_the_same_instance(manager):
    original = Service("demo", [])
    manager.register(original)
    manager.register(original)
    with pytest.raises(ModuleLoadError, match="Duplicate module name: demo"):
        manager.register(Service("demo", []))
    assert dict(manager.modules) == {"demo": original}


async def test_load_selected_closure_and_close_in_reverse_order(manager):
    calls = []
    for name, dependencies in [
        ("c", ["b"]),
        ("unrelated", []),
        ("b", ["a"]),
        ("a", []),
    ]:
        manager.register(Service(name, calls, required_modules=dependencies))
    await manager.load("c")
    assert calls == ["start:a", "start:b", "start:c"]
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
    "dependencies, message", [(["missing"], "Unknown module"), (["a"], "cycle")]
)
async def test_invalid_graph_fails_before_install(
    manager, dependencies, message, monkeypatch
):
    manager.register(Service("a", [], required_modules=dependencies))
    install = AsyncMock()
    monkeypatch.setattr(deployment_module, "run_uv", install)
    with pytest.raises(ModuleLoadError, match=message):
        await manager.load()
    install.assert_not_awaited()
    manager.modules["a"].startup.assert_not_awaited()
    assert not manager.loaded


async def test_load_rejects_unknown_module_before_startup(manager):
    module = Service("known", [])
    manager.register(module)
    with pytest.raises(ModuleLoadError, match="Unknown module: missing"):
        await manager.load("missing")
    module.startup.assert_not_awaited()
    assert not manager.loaded


async def test_startup_failure_stops_partial_resources_and_blocks_dependents(manager):
    calls = []
    broken = Service("broken", calls)
    broken.startup.side_effect = RuntimeError("startup failed")
    child = Service("child", calls, required_modules=["broken"])
    manager.register(broken)
    manager.register(child)
    await manager.load()
    broken.shutdown.assert_awaited_once()
    child.startup.assert_not_awaited()
    assert not manager.loaded
    assert not manager.dependencies
    await manager.stop()
    broken.shutdown.assert_awaited_once()
    child.shutdown.assert_not_awaited()


async def test_failed_startup_cleans_before_independent_startup_finishes(manager):
    cleaned = asyncio.Event()
    broken = Service("broken", [])
    broken.startup.side_effect = RuntimeError("startup failed")
    broken.shutdown.side_effect = cleaned.set
    independent = Service("independent", [])
    independent.startup.side_effect = cleaned.wait
    manager.register(broken)
    manager.register(independent)
    await asyncio.wait_for(manager.load(), 2)
    assert manager.loaded == {"independent"}
    broken.shutdown.assert_awaited_once()
    independent.shutdown.assert_not_awaited()
    await manager.stop()
    broken.shutdown.assert_awaited_once()
    independent.shutdown.assert_awaited_once()


@pytest.mark.parametrize("failure", [None, "agent", "database", "plugin"])
async def test_load_reports_shared_dependency_paths_after_all_branches_finish(
    manager, capsys, startup_errors, failure
):
    database_finished = asyncio.Event()
    for name, dependencies in [
        ("agent", []),
        ("database", ["agent"]),
        ("web", ["database"]),
        ("plugin", ["agent"]),
        ("outer", ["plugin"]),
    ]:
        module = Service(name, [], required_modules=dependencies)

        async def startup(name=name):
            if name == "database":
                database_finished.set()
            if name == "plugin":
                await database_finished.wait()
                # A failing parallel startup must not cancel this slower branch.
                await asyncio.sleep(0)
            assert "modules load " not in capsys.readouterr().out
            if name == failure:
                raise RuntimeError(f"{name} failed")

        module.startup.side_effect = startup
        manager.register(module)

    await asyncio.wait_for(manager.load("web", "agent", "outer", "web"), 2)
    assert [str(error) for _, error in startup_errors] == (
        [f"{failure} failed"] if failure else []
    )

    chains = [("agent", "database", "web"), ("agent", "plugin", "outer")]
    expected = []
    loaded = set()
    for chain in chains:
        skipped = False
        parts = []
        for name in chain:
            if skipped:
                parts.append(f"\033[90m{name}\033[0m")
            elif name == failure:
                parts.append(f"\033[31m{name}[x]\033[0m")
                skipped = True
            else:
                parts.append(name)
                loaded.add(name)
        result = "failed" if skipped else "success"
        expected.append(f"modules load {result}: {' -> '.join(parts)}")
    assert capsys.readouterr().out.splitlines() == expected
    assert manager.loaded == loaded
    for name, module in manager.modules.items():
        assert module.startup.await_count == int(name in loaded or name == failure)
        assert module.shutdown.await_count == int(name == failure)
    await manager.stop()
    for name, module in manager.modules.items():
        assert module.shutdown.await_count == int(name in loaded or name == failure)


@pytest.mark.parametrize("failed", [(), ("left",), ("left", "right")])
async def test_join_waits_for_all_prerequisites_and_skips_after_failure(
    manager, capsys, startup_errors, failed
):
    entered = {name: asyncio.Event() for name in ("left", "right")}
    for name, ready in entered.items():
        module = Service(name, [])

        async def startup(name=name, ready=ready):
            ready.set()
            await entered["right" if name == "left" else "left"].wait()
            if name in failed:
                raise RuntimeError(name)

        module.startup.side_effect = startup
        manager.register(module)
    child = Service("child", [], required_modules=["left", "right"])
    manager.register(child)
    await asyncio.wait_for(manager.load("child"), 2)
    assert {str(error) for _, error in startup_errors} == set(failed)
    if failed:
        child.startup.assert_not_awaited()
    else:
        child.startup.assert_awaited_once()
    expected = []
    for name in entered:
        parent = f"\033[31m{name}[x]\033[0m" if name in failed else name
        result = "failed" if failed else "success"
        tail = "\033[90mchild\033[0m" if failed else "child"
        expected.append(f"modules load {result}: {parent} -> {tail}")
    assert capsys.readouterr().out.splitlines() == expected
    assert manager.loaded == set(entered) - set(failed) | (
        set() if failed else {"child"}
    )
    await manager.stop()


@pytest.mark.parametrize("selection", ["parent", "child", "unrelated"])
@pytest.mark.parametrize("after_stop", [False, True])
async def test_load_is_once_per_application(manager, selection, after_stop):
    parent = Service("parent", [])
    child = Service("child", [], required_modules=["parent"])
    unrelated = Service("unrelated", [])
    manager.register(parent)
    manager.register(child)
    manager.register(unrelated)
    await manager.load("parent")
    if after_stop:
        await manager.stop()
    with pytest.raises(RuntimeError, match="only be loaded once"):
        await manager.load(selection)
    parent.startup.assert_awaited_once()
    child.startup.assert_not_awaited()
    unrelated.startup.assert_not_awaited()
    await manager.stop()


async def test_load_reports_results_even_when_failed_startup_cleanup_fails(
    manager, capsys, startup_errors
):
    broken = Service("broken", [])
    broken.startup.side_effect = RuntimeError("startup")
    broken.shutdown.side_effect = [RuntimeError("cleanup"), None]
    healthy = Service("healthy", [])
    manager.register(broken)
    manager.register(healthy)
    await manager.load()
    assert [(message, str(error)) for message, error in startup_errors] == [
        ("[module:broken] startup failed", "startup"),
        ("[module:broken] shutdown failed", "cleanup"),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "modules load failed: \033[31mbroken[x]\033[0m",
        "modules load success: healthy",
    ]
    assert manager.loaded == {"healthy"}
    await manager.stop()
    broken.shutdown.assert_awaited_once()
    healthy.shutdown.assert_awaited_once()
    assert not manager._runtime.active


async def test_startup_errors_are_logged_before_other_prerequisites_finish(
    manager, capsys
):
    first_failed, second_failed = asyncio.Event(), asyncio.Event()
    for name, dependencies in [
        ("parent", []),
        ("first", ["parent"]),
        ("second", []),
        ("late", []),
        ("child", ["first", "late"]),
        ("other", ["late"]),
    ]:
        module = Service(name, [], required_modules=dependencies)

        async def startup(name=name):
            if name == "first":
                first_failed.set()
                raise RuntimeError("first failure")
            if name == "second":
                await first_failed.wait()
                captured = capsys.readouterr()
                assert "[module:first] startup failed" in captured.err
                assert "RuntimeError: first failure" in captured.err
                assert "modules load " not in captured.out
                second_failed.set()
                raise ValueError("second failure")
            if name == "late":
                await second_failed.wait()
                captured = capsys.readouterr()
                assert "[module:second] startup failed" in captured.err
                assert "ValueError: second failure" in captured.err
                assert "first failure" not in captured.err
                assert "modules load " not in captured.out

        module.startup.side_effect = startup
        manager.register(module)

    await asyncio.wait_for(manager.load("child", "second", "other"), 2)
    assert manager.loaded == {"parent", "late", "other"}
    manager.modules["child"].startup.assert_not_awaited()
    for name in manager.loaded:
        manager.modules[name].shutdown.assert_not_awaited()
    assert capsys.readouterr().out.splitlines() == [
        "modules load failed: parent -> \033[31mfirst[x]\033[0m -> \033[90mchild\033[0m",
        "modules load failed: late -> \033[90mchild\033[0m",
        "modules load failed: \033[31msecond[x]\033[0m",
        "modules load success: late -> other",
    ]
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


async def test_shutdown_failure_is_logged_and_remaining_modules_close_once(
    manager, startup_errors
):
    calls = []
    parent = Service("parent", calls)
    child = Service("child", calls, required_modules=["parent"])
    child.shutdown.side_effect = RuntimeError("not stopped")
    manager.register(parent)
    manager.register(child)
    await manager.load()
    await manager.stop()
    assert [(message, str(error)) for message, error in startup_errors] == [
        ("[module:child] shutdown failed", "not stopped")
    ]
    assert calls == ["start:parent", "start:child", "stop:parent"]
    await manager.stop()
    assert not manager.loaded
    child.shutdown.assert_awaited_once()
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


async def test_startup_import_error_names_module_dependency_and_repair(
    manager, startup_errors
):
    module = Service("plugin", [])
    module.startup.side_effect = ModuleNotFoundError(
        "No module named 'fastapi'", name="fastapi"
    )
    manager.register(module)
    await manager.load("plugin")
    assert len(startup_errors) == 1
    message, error = startup_errors[0]
    assert message == "[module:plugin] startup failed"
    assert isinstance(error, ModuleLoadError)
    assert error.module == "plugin"
    assert error.missing == "fastapi"
    assert error.hint == "uv run alira sync"
    assert "fastapi" in str(error)
    module.shutdown.assert_awaited_once()
    assert not manager.loaded
    assert not manager._runtime.active


async def test_health_checks_all_registered_modules_without_loading_them(manager):
    assert await manager.check_health() == {}
    calls = []
    inactive = Service(
        "inactive", calls, required_modules=["missing"], requirements_txt="missing.txt"
    )
    active = Service("active", calls)
    inactive.health = AsyncMock(
        return_value={"health": False, "message": "Not started."}
    )
    manager.register(inactive)
    manager.register(active)
    await manager.load("active")
    try:
        assert await manager.check_health() == {
            "inactive": {"health": False, "message": "Not started."},
            "active": {"health": False, "message": "Health check is not implemented."},
        }
        inactive.health.assert_awaited_once_with()
        assert manager.loaded == {"active"}
        assert calls == ["start:active"]
        assert not manager.dependencies
    finally:
        await manager.stop()


async def test_health_selection_deduplicates_without_adding_prerequisites(manager):
    parent = Service("parent", [])
    child = Service("child", [], required_modules=["parent"])
    parent.health = AsyncMock()
    report = {"health": False, "message": "Queue is full."}
    child.health = AsyncMock(return_value=report)
    manager.register(parent)
    manager.register(child)

    assert await manager.check_health("child", "child") == {"child": report}
    child.health.assert_awaited_once_with()
    parent.health.assert_not_awaited()
    assert not manager.loaded


async def test_unknown_health_selection_fails_before_any_hook_runs(manager):
    module = Service("known", [])
    module.health = AsyncMock()
    manager.register(module)
    with pytest.raises(ModuleLoadError, match="Unknown modules: missing"):
        await manager.check_health("known", "missing")
    module.health.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Health probe failed"),
        TimeoutError("Probe timed out"),
        ValueError(),
    ],
)
async def test_health_failure_returns_false_and_continues_other_checks(manager, error):
    broken, healthy = Service("broken", []), Service("healthy", [])
    broken.health = AsyncMock(side_effect=error)
    report = {"health": True, "message": ""}
    healthy.health = AsyncMock(return_value=report)
    manager.register(broken)
    manager.register(healthy)

    assert await manager.check_health() == {
        "broken": {"health": False, "message": f"{type(error).__name__}: {error}"},
        "healthy": report,
    }
    broken.health.assert_awaited_once_with()
    healthy.health.assert_awaited_once_with()
    assert await asyncio.wait_for(manager.check_health("healthy"), 1) == {
        "healthy": report
    }


async def test_health_cancellation_releases_waiting_application_shutdown(manager):
    entered = asyncio.Event()
    module = Service("service", [])

    async def probe():
        entered.set()
        await asyncio.Event().wait()

    module.health = probe
    manager.register(module)
    await manager.load("service")
    checking = asyncio.create_task(manager.check_health("service"))
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        module.shutdown.assert_not_awaited()
        assert not stopping.done()
        checking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await checking
        await asyncio.wait_for(stopping, 1)
        module.shutdown.assert_awaited_once_with()
        assert not manager.loaded
    finally:
        checking.cancel()
        await asyncio.gather(checking, return_exceptions=True)
        if stopping is not None:
            await asyncio.wait_for(stopping, 1)
        await manager.stop()


async def test_cancellation_waits_for_failed_startup_cleanup_without_repeating_shutdown(
    manager,
):
    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
    resources = set()
    broken = Service("broken", [])

    async def startup():
        resources.add("resource")
        raise RuntimeError("startup failed after acquiring a resource")

    async def shutdown():
        cleanup_started.set()
        await release_cleanup.wait()
        resources.remove("resource")

    broken.startup.side_effect = startup
    broken.shutdown.side_effect = shutdown
    manager.register(broken)
    loading = asyncio.create_task(manager.load())
    try:
        await asyncio.wait_for(cleanup_started.wait(), 2)
        loading.cancel()
        await asyncio.sleep(0)
        assert not loading.done()
        assert resources == {"resource"}
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(loading, 2)
        assert not resources
        assert not manager.loaded
        assert not manager._runtime.active
        await manager.stop()
        broken.shutdown.assert_awaited_once()
    finally:
        release_cleanup.set()
        await asyncio.gather(loading, return_exceptions=True)
