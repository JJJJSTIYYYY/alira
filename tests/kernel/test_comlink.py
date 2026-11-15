"""Plugins communicate through the kernel using the real Apixis event runtime."""

from unittest.mock import AsyncMock

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
        async with apixis_core.expect_event("comlink.reply") as waiter:
            request = await apixis_core.post_and_wait(
                event_type=EventType.TRANSIENT,
                event_name="comlink.request",
                context=payload,
                time_out=1,
            )
            assert not request.has_error
            reply = await waiter.wait(time_out=1)
        assert reply.event_name == "comlink.reply"
        assert reply.context is payload
    finally:
        apixis_core.unsubscribe(group="comlink-test")
    assert not apixis_core.is_registered("respond")
    assert apixis_core.get_handler("respond") is None


@pytest.mark.parametrize(
    "name",
    [
        "subscribe",
        "unsubscribe",
        "post_event",
        "post_and_wait",
        "expect_event",
        "wait_event",
        "get_handler",
        "is_registered",
        "get_event_runtime_status",
    ],
)
def test_communication_interfaces_keep_upstream_signatures(name):
    import apixis

    assert getattr(apixis_core, name) is getattr(apixis, name)
