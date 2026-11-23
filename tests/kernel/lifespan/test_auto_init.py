"""One-time service startup and reverse-order application shutdown."""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alira.kernel.lifespan.auto_init import AutoInit, auto_init


@pytest.fixture
def registry(auto_init_factory):
    return auto_init_factory()


@pytest.fixture
def lifecycle_errors(monkeypatch):
    errors = []
    lifecycle = importlib.import_module("alira.kernel.lifespan.auto_init")
    monkeypatch.setattr(
        lifecycle.logger,
        "exception",
        lambda message: errors.append((message, sys.exception())),
    )
    return errors


def service(name, calls):
    return SimpleNamespace(
        start=AsyncMock(side_effect=lambda: calls.append(f"start:{name}")),
        stop=AsyncMock(side_effect=lambda: calls.append(f"stop:{name}")),
    )


def test_singleton_keeps_registrations(registry):
    registry.register(service("one", []))
    assert len(registry._services) == 1
    assert AutoInit() is auto_init
    assert registry is not auto_init
    assert registry._services is not auto_init._services


async def test_isolated_registry_factory_preserves_active_instances(auto_init_factory):
    calls = []
    first = auto_init_factory()
    first.register(service("running", calls))
    await first.start()
    second = auto_init_factory()
    assert second is not first
    assert not second._services
    assert not second._started
    assert first._started
    assert AutoInit() is auto_init
    await second.stop()
    assert calls == ["start:running"]
    await first.stop()
    assert calls == ["start:running", "stop:running"]


async def test_services_start_once_and_stop_in_reverse(registry):
    calls = []
    for name in ("a", "b", "c"):
        registry.register(service(name, calls))
    await registry.start()
    with pytest.raises(RuntimeError, match="only be started once"):
        await registry.start()
    await registry.stop()
    await registry.stop()
    with pytest.raises(RuntimeError, match="only be started once"):
        await registry.start()
    assert calls == [
        "start:a",
        "start:b",
        "start:c",
        "stop:c",
        "stop:b",
        "stop:a",
    ]
    assert not registry._services


async def test_late_registration_preserves_cleanup_for_directly_started_services(
    registry,
):
    calls = []
    registry.register(service("a", calls))
    await registry.start()
    late = service("late", calls)
    registry.register(late)
    await late.start()
    await registry.stop()
    assert calls == ["start:a", "start:late", "stop:late", "stop:a"]


async def test_registration_during_start_is_started_in_the_same_pass(registry):
    calls = []
    parent = service("parent", calls)
    child = service("child", calls)

    async def start_parent():
        calls.append("start:parent")
        registry.register(child)

    parent.start.side_effect = start_parent
    registry.register(parent)
    await registry.start()
    await registry.stop()
    assert calls == ["start:parent", "start:child", "stop:child", "stop:parent"]


async def test_empty_registry_is_safe(registry):
    await registry.start()
    await registry.stop()
    await registry.stop()


def test_registration_uses_identity(registry):
    first, second = service("same", []), service("same", [])
    # Equal and unhashable service objects must register independently.
    second.start, second.stop = first.start, first.stop
    assert first == second
    registry.register(first)
    registry.register(first)
    registry.register(second)
    assert len(registry._services) == 2


@pytest.mark.parametrize("method", ["start", "stop"])
def test_registration_checks_lifecycle_methods(registry, method):
    instance = service("one", [])
    delattr(instance, method)
    with pytest.raises(AttributeError, match=f"missing {method}"):
        registry.register(instance)
    setattr(instance, method, None)
    with pytest.raises(TypeError, match="must be callable"):
        registry.register(instance)


async def test_start_failure_is_logged_and_propagated_without_starting_later_services(
    registry, lifecycle_errors
):
    calls = []
    first, broken, last = [service(name, calls) for name in ("first", "broken", "last")]
    failure = RuntimeError("kernel startup failed")
    broken.start.side_effect = failure
    for instance in (first, broken, last):
        registry.register(instance)
    with pytest.raises(RuntimeError, match="kernel startup failed") as caught:
        await registry.start()
    assert caught.value is failure
    assert lifecycle_errors == [("Error starting SimpleNamespace", failure)]
    assert calls == ["start:first"]
    last.start.assert_not_awaited()
    with pytest.raises(RuntimeError, match="only be started once"):
        await registry.start()
    await registry.stop()
    assert calls == ["start:first", "stop:last", "stop:broken", "stop:first"]


async def test_shutdown_errors_are_logged_immediately_without_retry(
    registry, lifecycle_errors
):
    calls = []
    first, second, third = [service(name, calls) for name in ("a", "b", "c")]
    first_failure, third_failure = RuntimeError("a failed"), ValueError("c failed")
    first.stop.side_effect = first_failure
    third.stop.side_effect = third_failure

    async def stop_second():
        assert lifecycle_errors == [("Error stopping SimpleNamespace", third_failure)]
        calls.append("stop:b")

    second.stop.side_effect = stop_second
    for instance in (first, second, third):
        registry.register(instance)
    await registry.start()
    await registry.stop()
    assert lifecycle_errors == [
        ("Error stopping SimpleNamespace", third_failure),
        ("Error stopping SimpleNamespace", first_failure),
    ]
    await registry.stop()
    for instance in (first, second, third):
        instance.stop.assert_awaited_once()
    assert calls[-1] == "stop:b"
    assert not registry._services


async def test_stop_cleans_services_started_directly(registry):
    instance = service("one", [])
    registry.register(instance)
    await instance.start()
    await registry.stop()
    instance.stop.assert_awaited_once()


async def test_cancelled_start_is_cleaned_up_without_restarting(registry):
    entered = asyncio.Event()
    instance = service("one", [])
    registry.register(instance)

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    instance.start.side_effect = blocked
    task = asyncio.create_task(registry.start())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await registry.stop()
    instance.start.assert_awaited_once()
    instance.stop.assert_awaited_once()


async def test_module_manager_uses_the_registry_lifecycle(
    registry, tmp_path, module_manager_factory
):
    from alira.kernel.module import BaseModule

    calls = []

    class Module(BaseModule):
        async def startup(self):
            calls.append("module:start")

        async def shutdown(self):
            calls.append("module:stop")

    manager = module_manager_factory(tmp_path)
    manager.register(Module("example", "1"))
    registry.register(service("core", calls))
    registry.register(manager)
    await registry.start()
    assert not manager.loaded
    await manager.load("example")
    assert manager.loaded == {"example"}
    await registry.stop()
    assert not manager.loaded
    assert calls == ["start:core", "module:start", "module:stop", "stop:core"]


async def test_application_cleans_failed_modules_before_stopping_core(
    registry, tmp_path, monkeypatch, module_manager_factory
):
    from alira.kernel.module import BaseModule

    initialization = importlib.import_module("alira.kernel.initialize")
    calls = []

    class BrokenModule(BaseModule):
        async def startup(self):
            calls.append("module:start")
            raise RuntimeError("startup failed")

        async def shutdown(self):
            calls.append("module:stop")

    manager = module_manager_factory(tmp_path)
    manager.register(BrokenModule("broken", "1"))
    registry.register(service("core", calls))
    monkeypatch.setattr(initialization, "auto_init", registry)
    monkeypatch.setattr(
        initialization, "initialize", lambda: registry.register(manager)
    )
    async with initialization.lifespan():
        await manager.load("broken")
        assert calls == ["start:core", "module:start", "module:stop"]
    assert calls == ["start:core", "module:start", "module:stop", "stop:core"]
    assert not manager.loaded
    assert not registry._services


async def test_lifespan_cleans_all_registered_services_after_kernel_start_failure(
    registry, monkeypatch, lifecycle_errors
):
    initialization = importlib.import_module("alira.kernel.initialize")
    calls = []
    first, broken, last = [service(name, calls) for name in ("first", "broken", "last")]
    failure = RuntimeError("startup failed")
    broken.start.side_effect = failure
    for instance in (first, broken, last):
        registry.register(instance)
    monkeypatch.setattr(initialization, "auto_init", registry)
    monkeypatch.setattr(initialization, "initialize", lambda: None)
    with pytest.raises(RuntimeError, match="startup failed") as caught:
        async with initialization.lifespan():
            pytest.fail("Application body must not run after kernel startup fails")
    assert caught.value is failure
    assert lifecycle_errors == [("Error starting SimpleNamespace", failure)]
    assert calls == ["start:first", "stop:last", "stop:broken", "stop:first"]


async def test_lifespan_preserves_body_failure_when_shutdown_also_fails(
    registry, monkeypatch, lifecycle_errors
):
    initialization = importlib.import_module("alira.kernel.initialize")
    instance = service("one", [])
    cleanup_failure = RuntimeError("cleanup failed")
    instance.stop.side_effect = cleanup_failure
    registry.register(instance)
    monkeypatch.setattr(initialization, "auto_init", registry)
    monkeypatch.setattr(initialization, "initialize", lambda: None)
    with pytest.raises(ValueError, match="body failed"):
        async with initialization.lifespan():
            raise ValueError("body failed")
    assert lifecycle_errors == [("Error stopping SimpleNamespace", cleanup_failure)]
