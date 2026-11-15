import asyncio
import importlib
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
):
    manager, calls, _ = application
    task = asyncio.create_task(cli.run(["run", "child", "base", "child"]))
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


async def test_run_without_modules_runs_only_the_kernel(application):
    manager, calls, core_ready = application
    task = asyncio.create_task(cli.run(["run"]))
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


@pytest.mark.parametrize("arguments", [[], ["run", "unknown"], ["run", "chi"]])
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
async def test_cli_help_lists_commands_without_discovering_modules(application, capsys, flag):
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
    assert "-i, --install-missing" in text
    assert "--modules" not in text
    discover.assert_not_called()
    listing.assert_not_called()
    assert calls == []


@pytest.mark.parametrize(
    "action, arguments, expected, keywords",
    [
        ("add", ["child"], ("child",), {"no_install": False}),
        ("add", ["child", "--no-install"], ("child",), {"no_install": True}),
        ("add", ["child", "-n"], ("child",), {"no_install": True}),
        (
            "install",
            ["https://github.com/example/plugin", "--ref", "v1"],
            ("https://github.com/example/plugin",),
            {"ref": "v1"},
        ),
        ("remove", ["child", "base"], ("child", "base"), {}),
        ("sync", [], (), {"no_install": False}),
        ("sync", ["--no-install"], (), {"no_install": True}),
        ("sync", ["-n"], (), {"no_install": True}),
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


@pytest.mark.parametrize("flag", ["--package", "-r"])
async def test_cli_package_install_uses_batch_operation(application, monkeypatch, flag):
    manager, calls, _ = application
    operation = AsyncMock()
    monkeypatch.setattr(manager, "install_package", operation)
    await cli.run(["install", flag, "package.yml"])
    operation.assert_awaited_once_with("package.yml")
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
        ["add"],
        ["remove"],
        ["sync", "--unknown"],
        ["list", "child"],
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
    "arguments, expected",
    [
        (["run", "--install-missing", "child"], ("child",)),
        (["run", "child", "--install-missing"], ("child",)),
        (["run", "child", "-i"], ("child",)),
        (["run", "-i", "child"], ("child",)),
        (["run", "child", "base", "--install-missing"], ("child", "base")),
        (["run", "child", "base", "-i"], ("child", "base")),
    ],
)
async def test_install_missing_is_explicit_opt_in(
    application, monkeypatch, arguments, expected
):
    manager, _, _ = application
    add = AsyncMock()
    monkeypatch.setattr(manager, "add", add)
    task = asyncio.create_task(cli.run(arguments))
    try:
        await asyncio.wait_for(manager.modules["child"].ready.wait(), 2)
        add.assert_awaited_once_with(*expected, background=True)
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
        await cli.run(["run", "empty_entry"])
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
        cli.main(["run", "demo"])
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
