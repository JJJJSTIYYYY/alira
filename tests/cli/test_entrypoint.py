import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from alira.cli import entrypoint as cli
from alira.kernel.module import BaseModule


@pytest.fixture
def application(tmp_path, monkeypatch, module_manager_factory, auto_init_factory):
    manager = module_manager_factory(tmp_path)
    calls = []
    core_ready = asyncio.Event()

    class Module(BaseModule):
        def __init__(self, name, **kwargs):
            super().__init__(name, "1", **kwargs)
            self.ready = asyncio.Event()

        async def startup(self):
            calls.append(f"start:{self.name}")
            self.ready.set()

        async def shutdown(self):
            calls.append(f"stop:{self.name}")

    manager.register(Module("base"))
    manager.register(Module("child", required_modules=["base"]))
    manager.register(
        Module("inactive", required_modules=["missing"], requirements_txt="missing.txt")
    )

    async def start_core():
        calls.append("core:start")
        core_ready.set()

    registry = auto_init_factory()
    registry.register(
        SimpleNamespace(
            start=start_core,
            stop=AsyncMock(side_effect=lambda: calls.append("core:stop")),
        )
    )
    initialization = importlib.import_module("alira.kernel.initialize")
    monkeypatch.setattr(initialization, "auto_init", registry)
    monkeypatch.setattr(
        initialization, "initialize", lambda: registry.register(manager)
    )
    monkeypatch.setattr(cli, "module_manager", manager)
    return manager, calls, core_ready


async def test_cli_loads_only_requested_modules_and_prerequisites_after_kernel_start(
    application,
    capsys,
):
    manager, calls, _ = application
    task = asyncio.create_task(cli.run(["run", "child", "base", "child", "--no-sync"]))
    try:
        await asyncio.wait_for(manager.modules["child"].ready.wait(), 2)
        assert manager.loaded == {"base", "child"}
        assert calls == ["core:start", "start:base", "start:child"]
        assert not manager.dependencies
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == [
        "core:start",
        "start:base",
        "start:child",
        "stop:child",
        "stop:base",
        "core:stop",
    ]
    assert [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("modules load ")
    ] == ["modules load success: base -> child"]


async def test_cli_keeps_prerequisites_and_independent_modules_running_after_failure(
    application, capsys, monkeypatch
):
    manager, calls, _ = application
    failed = asyncio.Event()
    loaded = asyncio.Event()
    load = manager.load

    async def load_modules(*names, **options):
        await load(*names, **options)
        loaded.set()

    monkeypatch.setattr(manager, "load", load_modules)

    async def broken_startup():
        calls.append("start:child")
        failed.set()
        raise RuntimeError("child failed")

    async def independent_startup():
        await failed.wait()
        await asyncio.sleep(0)
        captured = capsys.readouterr()
        assert "modules load " not in captured.out
        assert "[module:child] startup failed" in captured.err
        assert "RuntimeError: child failed" in captured.err
        calls.append("start:independent")

    async def broken_shutdown():
        calls.append("stop:child")

    manager.modules["child"].startup = broken_startup
    manager.modules["child"].shutdown = broken_shutdown
    independent = BaseModule("independent", "1")
    independent.startup = independent_startup
    independent.shutdown = AsyncMock(
        side_effect=lambda: calls.append("stop:independent")
    )
    manager.register(independent)
    task = asyncio.create_task(cli.run(["run", "child", "independent", "--no-sync"]))
    try:
        await asyncio.wait_for(loaded.wait(), 2)
        assert not task.done()
        assert manager.loaded == {"base", "independent"}
        assert "stop:base" not in calls
        assert "stop:independent" not in calls
        assert "core:stop" not in calls
        assert [
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("modules load ")
        ] == [
            "modules load failed: base -> \033[31mchild[x]\033[0m",
            "modules load success: independent",
        ]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "stop:independent" in calls
    assert calls[-1] == "core:stop"
    assert not manager.loaded


async def test_run_without_modules_runs_only_the_kernel(application):
    manager, calls, core_ready = application
    task = asyncio.create_task(cli.run(["run", "--no-sync"]))
    try:
        await asyncio.wait_for(core_ready.wait(), 2)
        assert not manager.loaded
        assert not manager.dependencies
        assert calls == ["core:start"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == ["core:start", "core:stop"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--unknown"],
        ["run", "--install-miss"],
        ["run", "--child"],
        ["run", "-child"],
        ["--modules", "child"],
    ],
)
async def test_cli_rejects_unknown_or_abbreviated_flags_without_starting_kernel(
    application, capsys, arguments
):
    manager, calls, _ = application
    with pytest.raises(SystemExit) as caught:
        await cli.run(arguments)
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not manager.loaded
    assert calls == []


@pytest.mark.parametrize(
    "arguments", [[], ["run", "unknown", "--no-sync"], ["run", "chi", "--no-sync"]]
)
async def test_cli_rejects_missing_command_or_unknown_modules(
    application, capsys, arguments
):
    manager, calls, _ = application
    with pytest.raises(SystemExit) as caught:
        await cli.run(arguments)
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not manager.loaded
    assert calls == []


@pytest.mark.parametrize("flag", ["--help", "-h"])
async def test_cli_help_lists_commands_without_discovering_modules(
    application, capsys, flag
):
    manager, calls, _ = application
    with pytest.raises(SystemExit) as caught:
        await cli.run([flag])
    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "run" in text and "sync" in text and "install" in text
    assert "--child" not in text and "--base" not in text
    assert not manager.loaded
    assert calls == []


async def test_run_help_describes_positionals_without_module_discovery(
    application, monkeypatch, capsys
):
    manager, calls, _ = application
    discover = Mock(side_effect=AssertionError("Help discovered modules"))
    monkeypatch.setattr(manager, "discover", discover)
    listing = Mock(side_effect=AssertionError("Help listed modules"))
    monkeypatch.setattr(manager, "list", listing)
    with pytest.raises(SystemExit) as caught:
        await cli.run(["run", "--help"])
    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "[modules ...]" in text
    assert "--no-sync" in text and "--sync" in text
    assert "--locked" in text and "--frozen" in text
    assert "--install-missing" not in text
    assert "--modules" not in text
    discover.assert_not_called()
    listing.assert_not_called()
    assert calls == []


@pytest.mark.parametrize(
    "action, arguments, expected, keywords",
    [
        (
            "add",
            ["child"],
            ("child",),
            {"manifest": None, "ref": None, "no_sync": False, "repair": False},
        ),
        (
            "add",
            ["child", "--no-sync"],
            ("child",),
            {"manifest": None, "ref": None, "no_sync": True, "repair": False},
        ),
        (
            "add",
            ["--manifest", "package.yml"],
            (),
            {"manifest": "package.yml", "ref": None, "no_sync": False, "repair": False},
        ),
        (
            "install",
            ["https://github.com/example/plugin", "--ref", "v1"],
            ("https://github.com/example/plugin",),
            {"ref": "v1", "no_sync": False, "repair": False},
        ),
        (
            "remove",
            ["child", "base"],
            ("child", "base"),
            {"purge": False, "no_sync": False, "repair": False},
        ),
        (
            "remove",
            ["child", "--purge"],
            ("child",),
            {"purge": True, "no_sync": False, "repair": False},
        ),
        ("sync", [], (), {"locked": False, "frozen": False, "repair": False}),
        ("sync", ["--locked"], (), {"locked": True, "frozen": False, "repair": False}),
        ("sync", ["--frozen"], (), {"locked": False, "frozen": True, "repair": False}),
        ("sync", ["--repair"], (), {"locked": False, "frozen": False, "repair": True}),
        ("lock", [], (), {"check": False, "repair": False}),
        ("lock", ["--check"], (), {"check": True, "repair": False}),
        ("uninstall", ["child"], ("child",), {"repair": False}),
        ("migrate", [], (), {}),
    ],
)
async def test_cli_module_commands_finish_without_starting_modules(
    application,
    monkeypatch,
    action,
    arguments,
    expected,
    keywords,
):
    manager, calls, _ = application
    operation = AsyncMock()
    monkeypatch.setattr(manager, action, operation)
    await cli.run([action, *arguments])
    operation.assert_awaited_once_with(*expected, **keywords)
    assert not manager.loaded
    assert calls == []


@pytest.mark.parametrize("flag", ["--manifest", "--package", "-r"])
async def test_cli_package_install_uses_batch_operation(application, monkeypatch, flag):
    manager, calls, _ = application
    operation = AsyncMock()
    monkeypatch.setattr(manager, "install_package", operation)
    await cli.run(["install", flag, "package.yml"])
    operation.assert_awaited_once_with("package.yml", no_sync=False, repair=False)
    assert calls == []


async def test_cli_init_creates_sources_without_starting_services(application, capsys):
    manager, calls, _ = application
    registered = dict(manager.modules)
    await cli.run(["init", "example"])
    directory = manager.modules_dir / "example"
    assert (directory / "main.py").is_file()
    assert (directory / "src/shared/events.py").is_file()
    assert capsys.readouterr().out == f"Initialized module: {directory}\n"
    assert dict(manager.modules) == registered
    assert not manager.loaded
    assert calls == []


def test_cli_init_reports_existing_sources_without_a_traceback(application, capsys):
    manager, _, _ = application
    directory = manager.modules_dir / "example"
    directory.mkdir(parents=True)
    (directory / "main.py").write_text("existing source")
    with pytest.raises(SystemExit) as caught:
        cli.main(["init", "example"])
    assert caught.value.code == 1
    assert "already exists" in capsys.readouterr().err
    assert (directory / "main.py").read_text() == "existing source"


@pytest.mark.parametrize(
    "arguments",
    [
        ["module", "add", "child"],
        ["init"],
        ["init", "one", "two"],
        ["remove"],
        ["sync", "--unknown"],
        ["list", "child"],
        ["add", "child", "--no-install"],
        ["sync", "--no-install"],
        ["run", "--sync", "--no-sync"],
        ["sync", "--locked", "--frozen"],
        ["run", "--locked", "--frozen"],
        ["install", "one.zip", "two.zip"],
        ["install"],
        ["install", "one.zip", "-r", "package.yml"],
        ["install", "-r", "package.yml", "--ref", "v1"],
        ["install", "one.zip", "--package", "package.yml"],
        ["install", "--package", "package.yml", "--ref", "v1"],
        ["install", "--package"],
        ["install", "-r"],
    ],
)
async def test_cli_rejects_invalid_deployment_commands_without_starting_kernel(
    application, capsys, arguments
):
    manager, calls, _ = application
    with pytest.raises(SystemExit) as caught:
        await cli.run(arguments)
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not manager.loaded
    assert calls == []


@pytest.mark.parametrize("action", ["add", "remove", "sync", "list"])
@pytest.mark.parametrize("flag", ["--install-missing", "-i"])
async def test_cli_rejects_module_commands_with_runtime_options(
    application, capsys, action, flag
):
    manager, calls, _ = application
    names = ["child"] if action in {"add", "remove"} else []
    with pytest.raises(SystemExit) as caught:
        await cli.run([action, *names, flag])
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not manager.loaded
    assert calls == []


@pytest.mark.parametrize(
    "options", [[], ["--sync"], ["--locked"], ["--frozen"], ["--no-sync"]]
)
async def test_run_prepares_environment_before_discovery_and_startup(
    application, monkeypatch, options
):
    manager, calls, _ = application
    prepare = AsyncMock()
    monkeypatch.setattr(manager, "prepare_run", prepare)
    discover = manager.discover

    def discover_after_preparation():
        prepare.assert_awaited_once_with(
            "child",
            no_sync="--no-sync" in options,
            locked="--locked" in options,
            frozen="--frozen" in options,
            force_sync="--sync" in options,
        )
        return discover()

    monkeypatch.setattr(manager, "discover", discover_after_preparation)
    task = asyncio.create_task(cli.run(["run", "child", *options]))
    try:
        await asyncio.wait_for(manager.modules["child"].ready.wait(), 2)
        assert calls == ["core:start", "start:base", "start:child"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_help_does_not_import_or_initialize_unavailable_entries(
    application, capsys, monkeypatch
):
    manager, _, _ = application
    package = f"modules_{uuid4().hex}"
    root = manager.modules_dir / package
    root.mkdir()
    (root / "__init__.py").touch()
    monkeypatch.syspath_prepend(str(root.parent))
    manager.modules_dir = root
    manager.package = package
    for name in ("empty_entry", "class_only"):
        directory = manager.modules_dir / name
        directory.mkdir()
        (directory / "__init__.py").touch()
        (directory / "main.py").write_text(
            "from alira.kernel.module import BaseModule\nclass Demo(BaseModule): pass\n"
            if name == "class_only"
            else ""
        )
    with pytest.raises(SystemExit) as caught:
        await cli.run(["run", "--help"])
    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "[modules ...]" in text
    assert "--empty_entry" not in text and "--class_only" not in text
    assert manager.unavailable == set()
    assert not any(name.startswith(package + ".") for name in __import__("sys").modules)


async def test_run_rejects_unavailable_source_before_kernel_start(
    application, monkeypatch, capsys
):
    manager, calls, _ = application
    directory = manager.modules_dir / "empty_entry"
    directory.mkdir(parents=True)
    (directory / "main.py").touch()
    monkeypatch.setattr(manager, "discover", lambda: manager.modules)
    with pytest.raises(SystemExit) as caught:
        await cli.run(["run", "empty_entry", "--no-sync"])
    assert caught.value.code == 2
    assert "Unknown or unavailable modules: empty_entry" in capsys.readouterr().err
    assert calls == []


def test_dependency_error_has_actionable_nonzero_cli_exit(monkeypatch, capsys):
    from alira.kernel.module.dependencies import DependencyError

    failure = DependencyError(
        "missing dependency fastapi",
        module="web",
        missing="fastapi",
        hint="uv run alira sync",
    )
    monkeypatch.setattr(cli, "run", AsyncMock(side_effect=failure))
    with pytest.raises(SystemExit) as caught:
        cli.main(["run", "web"])
    assert caught.value.code == 1
    error = capsys.readouterr().err
    assert "web" in error and "fastapi" in error
    assert "uv run alira sync" in error


def test_discovery_load_error_has_clean_nonzero_cli_exit(
    tmp_path, monkeypatch, capsys, module_manager_factory
):
    package = f"modules_{uuid4().hex}"
    root = tmp_path / package
    folder = root / "demo"
    folder.mkdir(parents=True)
    (root / "__init__.py").touch()
    (folder / "__init__.py").touch()
    (folder / "main.py").write_text(
        "from alira.kernel.module import BaseModule\n"
        "module = BaseModule('other', '1')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        cli, "module_manager", module_manager_factory(root, package=package)
    )
    with pytest.raises(SystemExit) as caught:
        cli.main(["run", "demo", "--no-sync"])
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Module name 'other' does not match directory 'demo'\n"


def test_grouped_dependency_errors_keep_actionable_cli_output(monkeypatch, capsys):
    from alira.kernel.module.dependencies import DependencyError

    failure = ExceptionGroup(
        "install failed",
        [
            DependencyError(
                "missing dependency fastapi",
                module="web",
                hint="uv run alira sync",
            )
        ],
    )
    monkeypatch.setattr(cli, "run", AsyncMock(side_effect=failure))
    with pytest.raises(SystemExit) as caught:
        cli.main(["sync", "web"])
    assert caught.value.code == 1
    error = capsys.readouterr().err
    assert "uv run alira sync" in error
    assert "Traceback" not in error


async def test_list_imports_declarations_and_versions_without_lifecycle_or_dependency_checks(
    tmp_path, monkeypatch, module_manager_factory, capsys
):
    package = f"listing_{uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").touch()
    for name in ("demo", "pending"):
        folder = root / name
        folder.mkdir()
        (folder / "__init__.py").touch()
        (folder / "main.py").write_text("")
    (root / "demo/main.py").write_text(
        "from alira.kernel.module import BaseModule\n"
        'def declared_version(): return "1.2.3"\n'
        "class Demo(BaseModule):\n"
        "    async def startup(self): import missing_startup_dependency\n"
        "    async def shutdown(self): import missing_shutdown_dependency\n"
        "    async def health(self): import missing_health_dependency\n"
        'module=Demo("demo", declared_version(), required_modules=["absent>=9"], '
        'required_kernel=">=99", requirements_txt="missing.txt")\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    manager = module_manager_factory(root, package=package)
    manager._dependencies.ledger.path.parent.mkdir(parents=True)
    manager._dependencies.ledger.path.write_text("{broken record")
    monkeypatch.setattr(cli, "module_manager", manager)
    for method in ("start", "stop", "load", "check_health"):
        monkeypatch.setattr(
            manager, method, AsyncMock(side_effect=AssertionError("Lifecycle invoked"))
        )
    monkeypatch.setattr(
        manager._registry,
        "graph",
        Mock(side_effect=AssertionError("Dependency resolution invoked")),
    )
    monkeypatch.setattr(
        manager._dependencies.ledger,
        "read",
        Mock(side_effect=AssertionError("Ledger read")),
    )
    await cli.run(["list"])
    assert capsys.readouterr().out.splitlines() == [
        "NAME     VERSION",
        "demo     1.2.3",
        "pending  -",
    ]
    assert f"{package}.demo.main" in sys.modules
    assert not manager.loaded
    assert not manager._runtime.active
