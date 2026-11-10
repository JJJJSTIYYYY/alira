import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alira import main
from alira.kernel.lifespan.auto_init import AutoInit
from alira.kernel.module import BaseModule


@pytest.fixture
def application(tmp_path, monkeypatch, module_manager_factory):
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
    manager.register(Module("inactive", required_modules=["missing"], requirements_txt="missing.txt"))

    class Registry(AutoInit):
        _instance = None

    async def start_core():
        calls.append("core:start")
        core_ready.set()

    registry = Registry()
    registry.register(SimpleNamespace(
        start=start_core, stop=AsyncMock(side_effect=lambda: calls.append("core:stop")),
    ))
    initialization = importlib.import_module("alira.kernel.initialize")
    monkeypatch.setattr(initialization, "auto_init", registry)
    monkeypatch.setattr(initialization, "initialize", lambda: registry.register(manager))
    monkeypatch.setattr(main, "module_manager", manager)
    return manager, calls, core_ready


async def test_cli_loads_only_requested_modules_and_prerequisites_after_kernel_start(application):
    manager, calls, _ = application
    task = asyncio.create_task(main.run(["--child", "--child"]))
    try:
        await asyncio.wait_for(manager.modules["child"].ready.wait(), 2)
        assert manager.loaded == {"base", "child"}
        assert calls == ["core:start", "start:base", "start:child"]
        assert not manager.dependencies
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == ["core:start", "start:base", "start:child", "stop:child", "stop:base", "core:stop"]


async def test_cli_without_flags_runs_only_the_kernel(application):
    manager, calls, core_ready = application
    task = asyncio.create_task(main.run([]))
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


@pytest.mark.parametrize("flag", ["--unknown", "--chi"])
async def test_cli_rejects_unknown_or_abbreviated_flags_and_cleans_up(application, capsys, flag):
    manager, calls, _ = application
    with pytest.raises(SystemExit) as caught:
        await main.run([flag])
    assert caught.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert not manager.loaded
    assert calls == ["core:start", "core:stop"]


async def test_cli_help_lists_discovered_modules_without_loading(application, capsys):
    manager, calls, _ = application
    with pytest.raises(SystemExit) as caught:
        await main.run(["--help"])
    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "--child" in text and "--base" in text
    assert not manager.loaded
    assert calls == ["core:start", "core:stop"]
