"""Group isolation, failure recovery, cancellation, and lifecycle serialization."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alira.lifespan.auto_init import AutoInit, auto_init


@pytest.fixture
def registry():
    class IsolatedAutoInit(AutoInit):
        _instance = None

    return IsolatedAutoInit()


def service(name, calls):
    return SimpleNamespace(
        start=AsyncMock(side_effect=lambda: calls.append(f"start:{name}")),
        stop=AsyncMock(side_effect=lambda: calls.append(f"stop:{name}")),
    )


def test_singleton_keeps_registrations(registry):
    registry.register(service("one", []))
    assert type(registry)() is registry
    assert len(registry._services) == 1
    assert AutoInit() is auto_init


async def test_groups_keep_global_order_and_stop_in_reverse(registry):
    calls = []
    for name, group in [("a", "core"), ("b", "tool"), ("c", "core")]:
        registry.register(service(name, calls), group=group)

    await registry.start("core")
    await registry.start()  # Only the tool service still needs starting.
    await registry.start("core")
    await registry.stop("core")
    await registry.start("core")
    await registry.stop()

    assert calls == [
        "start:a", "start:c", "start:b", "stop:c", "stop:a",
        "start:a", "start:c", "stop:c", "stop:b", "stop:a",
    ]


async def test_default_group_and_registration_after_start(registry):
    calls = []
    registry.register(service("a", calls))
    await registry.start()
    registry.register(service("b", calls))
    registry.register(service("c", calls), "tool")
    await registry.start("default")
    await registry.start()
    assert calls == ["start:a", "start:b", "start:c"]


async def test_empty_registry_is_safe_but_unknown_group_raises(registry):
    await registry.start()
    await registry.stop()
    registry.register(service("one", []), "core")
    for operation in (registry.start, registry.stop):
        with pytest.raises(ValueError, match="Unknown service group"):
            await operation("missing")
    with pytest.raises(ValueError, match="Unknown service group"):
        await registry.start("missing", restart=True)
    registry._services[0].start.assert_not_awaited()
    registry._services[0].stop.assert_not_awaited()


def test_registration_uses_identity_and_rejects_conflicting_groups(registry):
    first, second = service("same", []), service("same", [])
    # Equal and unhashable service objects must still register independently.
    second.start, second.stop = first.start, first.stop
    assert first == second
    registry.register(first, "core")
    registry.register(first, "core")
    registry.register(second, "core")
    assert len(registry._services) == 2
    with pytest.raises(ValueError, match="another group"):
        registry.register(first, "tool")


@pytest.mark.parametrize("group", ["", " ", None, 1])
def test_registration_rejects_invalid_group(registry, group):
    with pytest.raises(ValueError, match="non-empty string"):
        registry.register(service("one", []), group)


@pytest.mark.parametrize("method", ["start", "stop"])
def test_registration_checks_lifecycle_methods(registry, method):
    instance = service("one", [])
    delattr(instance, method)
    with pytest.raises(AttributeError, match=f"missing {method}"):
        registry.register(instance)
    setattr(instance, method, None)
    with pytest.raises(TypeError, match="must be callable"):
        registry.register(instance)


@pytest.mark.parametrize("group", [None, "tool"])
async def test_explicit_restart_stops_then_starts_only_selection(registry, group):
    calls = []
    registry.register(service("a", calls), "tool")
    registry.register(service("b", calls), "core")
    registry.register(service("c", calls), "tool")
    await registry.start()
    calls.clear()

    await registry.start(group, restart=True)
    assert calls == (
        ["stop:c", "stop:b", "stop:a", "start:a", "start:b", "start:c"]
        if group is None else ["stop:c", "stop:a", "start:a", "start:c"]
    )


async def test_start_failures_are_reported_and_only_failed_services_retry(registry):
    calls = []
    first, healthy, last = [service(name, calls) for name in ("a", "b", "c")]
    first.start.side_effect = [RuntimeError("first failed"), None]
    last.start.side_effect = [ValueError("last failed"), None]
    for instance in (first, healthy, last):
        registry.register(instance)

    with pytest.raises(ExceptionGroup, match="Failed to start") as caught:
        await registry.start()
    assert [str(exc) for exc in caught.value.exceptions] == [
        "first failed", "last failed",
    ]
    await registry.start()
    first.stop.assert_awaited_once()
    last.stop.assert_awaited_once()
    healthy.start.assert_awaited_once()
    healthy.stop.assert_not_awaited()
    assert first.start.await_count == last.start.await_count == 2
    await registry.stop()


async def test_failed_start_cleanup_must_succeed_before_retry(registry):
    instance = service("one", [])
    instance.start.side_effect = [RuntimeError("start failed"), None]
    instance.stop.side_effect = [RuntimeError("cleanup failed"), None]
    registry.register(instance)
    for message in ("start failed", "cleanup failed"):
        with pytest.raises(ExceptionGroup) as caught:
            await registry.start()
        assert str(caught.value.exceptions[0]) == message
    instance.start.assert_awaited_once()
    await registry.start()
    assert instance.start.await_count == instance.stop.await_count == 2


async def test_stop_continues_after_failures_and_restart_waits_for_cleanup(registry):
    calls = []
    first, second, third = [service(name, calls) for name in ("a", "b", "c")]
    for instance in (first, second, third):
        registry.register(instance)
    await registry.start()
    first.stop.side_effect = [RuntimeError("a failed"), None]
    third.stop.side_effect = [ValueError("c failed"), None]
    with pytest.raises(ExceptionGroup, match="Failed to stop") as caught:
        await registry.start(restart=True)
    assert [str(exc) for exc in caught.value.exceptions] == ["c failed", "a failed"]
    for instance in (first, second, third):
        instance.stop.assert_awaited_once()
        instance.start.assert_awaited_once()

    await registry.start()
    assert first.stop.await_count == third.stop.await_count == 2
    second.stop.assert_awaited_once()
    assert all(instance.start.await_count == 2 for instance in (first, second, third))


async def test_stop_cleans_services_started_directly(registry):
    instance = service("one", [])
    registry.register(instance)
    await instance.start()
    await registry.stop()
    instance.stop.assert_awaited_once()


@pytest.mark.parametrize("transition", ["start", "stop"])
async def test_cancellation_retains_cleanup_state_and_releases_lock(registry, transition):
    entered, release = asyncio.Event(), asyncio.Event()
    instance = service("one", [])
    registry.register(instance)

    async def blocked():
        entered.set()
        await release.wait()

    if transition == "stop":
        await registry.start()
    operation = getattr(instance, transition)
    operation.side_effect = blocked
    task = asyncio.create_task(getattr(registry, transition)())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    operation.side_effect = None
    await asyncio.wait_for(registry.start(), 1)
    assert instance.stop.await_count == (1 if transition == "start" else 2)
    assert instance.start.await_count == 2


@pytest.mark.parametrize("following", ["start", "stop"])
async def test_concurrent_lifecycle_operations_are_serialized(registry, following):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    instance = service("one", calls)

    async def blocked_start():
        calls.append("start")
        entered.set()
        await release.wait()

    instance.start.side_effect = blocked_start
    registry.register(instance)
    first = asyncio.create_task(registry.start())
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(getattr(registry, following)())
    await asyncio.sleep(0)
    assert not second.done()
    instance.stop.assert_not_awaited()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert calls == (["start"] if following == "start" else ["start", "stop:one"])
