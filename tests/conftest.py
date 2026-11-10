"""Isolate application graphs and asynchronous work through runtime APIs."""

import asyncio
import importlib
from pathlib import Path

import pytest
import pytest_asyncio
from apixis.core.event import aget_event_loop, aget_event_pipe, factory
from apixis.core.graph import NodeGraph

from alira.modules.tool.utils.manager import ToolManager
from alira.utils.logger import logger
from alira.kernel import auto_init
from alira.kernel.lifespan.auto_init import ApixisCore, AutoInit
from alira.kernel.module import ModuleManager


@pytest.fixture
def module_manager_factory():
    def create(modules_dir=None, *, package="alira.modules"):
        class IsolatedModuleManager(ModuleManager):
            _instance = None

        manager = IsolatedModuleManager()
        if modules_dir is not None:
            manager.modules_dir = Path(modules_dir)
        manager.package = package
        return manager

    return create


@pytest_asyncio.fixture
async def core_lifecycle():
    """Use real core services without starting unrelated application storage."""

    class IsolatedAutoInit(AutoInit):
        _instance = None

    registry = IsolatedAutoInit()
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
    original_manager_init = ToolManager.__init__

    def track_graph(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        graphs.append(self)

    def track_manager(self, *args, **kwargs):
        original_manager_init(self, *args, **kwargs)
        managers.append(self)

    monkeypatch.setattr(NodeGraph, "__init__", track_graph)
    monkeypatch.setattr(ToolManager, "__init__", track_manager)
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
