"""Plugins communicate through the kernel using the real Apixis event runtime."""

import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest

from alira.kernel.comlink import (
    VERSION,
    ApixEvent,
    ApixisCore,
    EventType,
    apixis_core,
)


async def test_lifecycle_delegates_to_apixis(monkeypatch):
    import apixis

    lifecycle = Mock()
    create, start, stop, close = Mock(), AsyncMock(), AsyncMock(), AsyncMock()
    for name, method in (("create", create), ("start", start), ("stop", stop), ("close", close)):
        lifecycle.attach_mock(method, name)
    monkeypatch.setattr(apixis, "create_core", create)
    monkeypatch.setattr(apixis, "close_core", close)
    monkeypatch.setattr(apixis, "start_core", start)
    monkeypatch.setattr(apixis, "stop_core", stop)
    core = ApixisCore()
    await core.start()
    await core.stop()
    assert lifecycle.mock_calls == [call.create(), call.start(), call.stop(), call.close()]
    start.assert_awaited_once_with()
    stop.assert_awaited_once_with()
    close.assert_awaited_once_with()
    assert core.get_version() == VERSION


async def test_plugin_request_reply_preserves_payload_and_group_cleanup():
    payload = {"value": 42}

    @apixis_core.subscribe("comlink.request", group="comlink-test")
    async def respond(event: ApixEvent):
        assert event.context is payload
        await apixis_core.post_event(
            event_type=EventType.TRANSIENT,
            event_name="comlink.reply",
            context=event.context,
        )

    try:
        assert apixis_core.is_registered("respond")
        assert apixis_core.get_handler_meta("respond") is not None
        async with apixis_core.expect_event("comlink.reply", time_out=1) as waiter:
            request = await apixis_core.post_and_wait(
                event_type=EventType.TRANSIENT,
                event_name="comlink.request",
                context=payload,
                time_out=1,
            )
            assert not request.has_error
            reply = await waiter.wait()
        assert reply.event_name == "comlink.reply"
        assert reply.context is payload
    finally:
        apixis_core.unsubscribe(group="comlink-test")
    assert not apixis_core.is_registered("respond")
    assert apixis_core.get_handler_meta("respond") is None


@pytest.mark.parametrize(
    "name",
    [
        "subscribe",
        "unsubscribe",
        "post_event",
        "post_and_wait",
        "expect_event",
        "wait_event",
        "get_handler_meta",
        "is_registered",
        "get_event_runtime_status",
    ],
)
def test_communication_interfaces_keep_upstream_signatures(name):
    import apixis

    assert getattr(apixis_core, name) is getattr(apixis, name)


async def test_start_is_running_and_stop_closes_event_work():
    import apixis

    core = ApixisCore()
    await core.start()
    upstream = apixis.get_core()
    assert upstream.state is apixis.CoreState.RUNNING
    await core.start()
    assert apixis.get_core() is upstream

    started, cleaned = asyncio.Event(), asyncio.Event()

    @core.subscribe("comlink.blocked", name="comlink-blocked")
    async def blocked(event):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async with core.expect_event("comlink.never") as waiter:
        await core.post_event(event_name="comlink.blocked")
        await asyncio.wait_for(started.wait(), 1)
        await core.stop()
        assert await waiter.wait() is None
    assert upstream.state is apixis.CoreState.CLOSED
    assert cleaned.is_set()
    status = core.get_event_runtime_status()
    assert not status.running and status.wait_listeners == 0
    assert status.event_queue_size == 0
    assert status.event_loop.dispatch_tasks == 0
    assert not core.is_registered("comlink-blocked")
    await core.stop()
    await core.start()
    assert apixis.get_core() is not upstream
    assert apixis.get_core().state is apixis.CoreState.RUNNING
    assert not core.is_registered("comlink-blocked")


async def test_stop_closes_even_when_consumption_stop_fails(monkeypatch):
    import apixis

    close = AsyncMock()
    monkeypatch.setattr(apixis, "stop_core", AsyncMock(side_effect=RuntimeError("stop failed")))
    monkeypatch.setattr(apixis, "close_core", close)
    with pytest.raises(RuntimeError, match="stop failed"):
        await ApixisCore().stop()
    close.assert_awaited_once_with()


async def test_wait_deadline_is_preserved_and_listeners_are_released():
    core = ApixisCore()
    async with core.expect_event("comlink.timeout", time_out=0) as waiter:
        with pytest.raises(TimeoutError):
            await waiter.wait()
    assert core.get_event_runtime_status().wait_listeners == 0
