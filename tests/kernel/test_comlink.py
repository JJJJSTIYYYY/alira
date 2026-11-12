"""Plugins communicate through the kernel using the real Apixis event runtime."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from alira.kernel.comlink import (
    VERSION,
    ApixEvent,
    ApixisCore,
    EventType,
    apixis_core,
    get_handler_registry,
)


async def test_lifecycle_delegates_to_apixis(monkeypatch):
    import apixis

    start, stop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(apixis, "start_core", start)
    monkeypatch.setattr(apixis, "stop_core", stop)
    core = ApixisCore()
    await core.start()
    await core.stop()
    start.assert_awaited_once_with()
    stop.assert_awaited_once_with()
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
        assert apixis_core.get_handler("respond") is not None
        async with apixis_core.wait_for_event("comlink.reply") as waiter:
            await apixis_core.post_event(
                event_type=EventType.TRANSIENT,
                event_name="comlink.request",
                context=payload,
            )
            reply = await waiter.wait(time_out=1)
        assert reply.event_name == "comlink.reply"
        assert reply.context is payload
    finally:
        apixis_core.unsubscribe(group="comlink-test")
    assert not apixis_core.is_registered("respond")
    assert apixis_core.get_handler("respond") is None


@pytest.mark.parametrize("cancelled", [False, True])
async def test_wait_failure_releases_temporary_subscriptions(cancelled):
    registry = get_handler_registry()
    before = set(registry.registry)

    async def wait():
        async with apixis_core.wait_for_event("comlink.never") as waiter:
            if cancelled:
                raise asyncio.CancelledError
            await waiter.wait(time_out=0)

    with pytest.raises(asyncio.CancelledError if cancelled else TimeoutError):
        await wait()
    assert set(registry.registry) == before


@pytest.mark.parametrize(
    "name",
    [
        "subscribe",
        "unsubscribe",
        "wait_for_event",
        "await_for",
        "get_handler",
        "is_registered",
    ],
)
def test_subscription_interfaces_keep_upstream_signatures(name):
    import apixis

    assert getattr(apixis_core, name) is getattr(apixis, name)
