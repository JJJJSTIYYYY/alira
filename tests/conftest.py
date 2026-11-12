"""Isolate application graphs and asynchronous work through runtime APIs."""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from apixis.core.event import aget_event_loop, aget_event_pipe, factory
from apixis.core.graph import NodeGraph

from alira.kernel import auto_init
from alira.kernel.comlink import ApixisCore
from alira.kernel.lifespan import AutoInit
from alira.kernel.lifespan.resource_cleaner import ResourceCleaner
from alira.kernel.module import ModuleManager
from alira.shared.logger import logger


@pytest.fixture(scope="session", autouse=True)
def runtime_log_directory(tmp_path_factory):
    """Keep process-global diagnostic output out of the user's data directory."""
    writer = logger._writer
    original = writer.root
    writer.root = tmp_path_factory.mktemp("runtime-logs")
    try:
        yield
    finally:
        writer.stop()
        writer.root = original


@pytest.fixture
def module_manager_factory(tmp_path):
    sequence = 0

    def create(modules_dir=None, *, package="alira.modules"):
        nonlocal sequence
        state_dir = tmp_path / f"module-state-{sequence}"
        sequence += 1
        manager = ModuleManager._new_for_tests(state_dir=state_dir)
        if modules_dir is not None:
            manager.modules_dir = Path(modules_dir)
        manager.package = package
        return manager

    return create


@pytest.fixture
def auto_init_factory():
    return AutoInit._new_for_tests


@pytest.fixture
def resource_cleaner_factory():
    return ResourceCleaner._new_for_tests


@pytest_asyncio.fixture
async def core_lifecycle(auto_init_factory):
    """Use real core services without starting unrelated application storage."""

    registry = auto_init_factory()
    registry.register(logger)
    registry.register(ApixisCore())
    try:
        yield registry
    finally:
        if registry._started_services or registry._pending_stop:
            await registry.stop()


@pytest.fixture
def kernel_lifecycle(core_lifecycle, monkeypatch):
    initialization = importlib.import_module("alira.kernel.initialize")
    monkeypatch.setattr(initialization, "auto_init", core_lifecycle)
    monkeypatch.setattr(initialization, "initialize", lambda: None)
    return initialization


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def cleanup_event_runtime(event_runtime_capacity, monkeypatch):
    """Retire test graphs and cancel test-owned tasks without private registries."""
    if event_runtime_capacity is not None:
        loop_module = importlib.import_module("apixis.core.event.event_loop")
        monkeypatch.setattr(
            loop_module, "EVENT_LOOP_BACKPRESSURE", event_runtime_capacity
        )
        monkeypatch.setattr(factory, "_core", None)
    graphs = []
    managers = []
    original_services = list(auto_init._services)
    original_init = NodeGraph.__init__

    def track_graph(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        graphs.append(self)

    monkeypatch.setattr(NodeGraph, "__init__", track_graph)
    # Collection imports the tool implementation for tool/agent test suites.
    # Pure kernel tests must not import optional business dependencies for cleanup.
    tool_module = sys.modules.get("alira.modules.tool.src.utils.manager")
    if tool_module is not None:
        original_manager_init = tool_module.ToolManager.__init__

        def track_manager(self, *args, **kwargs):
            original_manager_init(self, *args, **kwargs)
            managers.append(self)

        monkeypatch.setattr(tool_module.ToolManager, "__init__", track_manager)
    baseline = asyncio.all_tasks()
    pipe = await aget_event_pipe()
    event_loop = await aget_event_loop()
    try:
        yield
    finally:
        for graph in reversed(graphs):
            graph.decompose(force=True)
        for manager in reversed(managers):
            await manager.stop()
        auto_init._services[:] = original_services
        tasks = asyncio.all_tasks() - baseline - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await pipe.stop()
        await event_loop.stop()
        await pipe.clear()


@pytest.fixture
def event_runtime_capacity():
    """Use ordinary runtime limits unless a boundary test requests a new core."""
    return
