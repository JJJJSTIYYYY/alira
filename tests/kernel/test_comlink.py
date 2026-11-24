"""Plugins communicate through the kernel using the real Apixis event runtime."""

import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest

from alira.kernel.comlink import (
    VERSION,
    ApixEvent,
    ApixisCore,
    ComlinkData,
    EventType,
    apixis_core,
    comlink,
)


def module_comlink(extension_point, extension_name=None):
    """Apply comlink to a test function as if it were defined in a business module."""

    def decorator(func):
        func.__module__ = "alira.modules.test.src.foo"
        return comlink(extension_point, extension_name)(func)

    return decorator


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


async def test_comlink_modifies_arguments_and_return_value(monkeypatch):
    calls = []
    debug = Mock()
    monkeypatch.setattr("alira.kernel.comlink.decorator.logger.debug", debug)

    @apixis_core.subscribe("test.compute.entry", group="comlink-decorator")
    async def modify_entry(event: ApixEvent):
        assert isinstance(event.context, ComlinkData)
        assert event.context.point == "entry"
        calls.append(("entry", event.context.args, dict(event.context.kwargs)))
        event.context.args = (event.context.args[0] + 1,)
        event.context.kwargs["factor"] = 3

    @apixis_core.subscribe("test.compute.exit", group="comlink-decorator")
    async def modify_exit(event: ApixEvent):
        assert event.context.point == "exit"
        calls.append(("exit", event.context.returns))
        event.context.returns *= 2

    @module_comlink("both")
    async def compute(value, *, factor=1):
        return value * factor

    try:
        assert await compute(1) == 12
    finally:
        apixis_core.unsubscribe(group="comlink-decorator")

    assert calls == [("entry", (1,), {}), ("exit", 6)]
    assert debug.call_args_list == [
        call("Comlink entry extension point completed by: ['modify_entry']"),
        call("Comlink exit extension point completed by: ['modify_exit']"),
    ]


async def test_comlink_extension_points_run_only_where_selected():
    calls = []

    @apixis_core.subscribe("test.entry_only.entry", group="comlink-decorator")
    async def record_entry(event: ApixEvent):
        calls.append(("entry", event.context.args))

    @apixis_core.subscribe("test.exit_only.exit", group="comlink-decorator")
    async def record_exit(event: ApixEvent):
        calls.append(("exit", event.context.returns))

    @module_comlink("entry")
    async def entry_only(value):
        return value

    @module_comlink("exit")
    async def exit_only(value):
        return value

    try:
        assert await entry_only(1) == 1
        assert await exit_only(2) == 2
    finally:
        apixis_core.unsubscribe(group="comlink-decorator")

    assert calls == [("entry", (1,)), ("exit", 2)]


async def test_comlink_uses_named_extension_events():
    @apixis_core.subscribe("test.named.entry", group="comlink-decorator")
    async def modify_entry(event: ApixEvent):
        event.context.args = (event.context.args[0] + 1,)

    @apixis_core.subscribe("test.named.exit", group="comlink-decorator")
    async def modify_exit(event: ApixEvent):
        event.context.returns *= 2

    @module_comlink("both", extension_name="named")
    async def calculate(value):
        return value

    try:
        assert await calculate(1) == 4
    finally:
        apixis_core.unsubscribe(group="comlink-decorator")


async def test_comlink_preserves_function_metadata():
    @module_comlink("exit")
    async def named(value: int) -> int:
        """Return the supplied value."""
        return value

    assert named.__name__ == "named"
    assert named.__doc__ == "Return the supplied value."
    assert await named(1) == 1


def test_comlink_validates_extension_point_and_async_function():
    with pytest.raises(ValueError, match="extension_point"):
        comlink("before")
    with pytest.raises(TypeError, match="async"):
        comlink("both")(lambda: None)


def test_comlink_rejects_functions_outside_modules():
    async def operation():
        return None

    with pytest.raises(ValueError, match="alira.modules"):
        comlink("both")(operation)

    operation.__module__ = "alira.modules_extra"
    with pytest.raises(ValueError, match="alira.modules"):
        comlink("both")(operation)

    operation.__module__ = "alira.modules"
    with pytest.raises(ValueError, match="alira.modules"):
        comlink("both")(operation)


async def test_comlink_logs_extension_handler_failure(monkeypatch):
    warning = Mock()
    debug = Mock()
    monkeypatch.setattr("alira.kernel.comlink.decorator.logger.warning", warning)
    monkeypatch.setattr("alira.kernel.comlink.decorator.logger.debug", debug)

    @apixis_core.subscribe("test.operation.entry", group="comlink-decorator")
    async def broken_entry(event: ApixEvent):
        raise ValueError("broken extension")

    @module_comlink("entry")
    async def operation():
        return "unused"

    try:
        assert await operation() == "unused"
    finally:
        apixis_core.unsubscribe(group="comlink-decorator")

    warning.assert_called_once()
    error = warning.call_args.args[0]
    assert isinstance(error, RuntimeError)
    assert "broken extension" in str(error)
    debug.assert_not_called()


async def test_comlink_keeps_original_data_when_event_is_cancelled(monkeypatch):
    exception = Mock()
    debug = Mock()
    monkeypatch.setattr("alira.kernel.comlink.decorator.logger.exception", exception)
    monkeypatch.setattr("alira.kernel.comlink.decorator.logger.debug", debug)
    monkeypatch.setattr(apixis_core, "post_and_wait", AsyncMock(return_value=None))

    @module_comlink("both")
    async def compute(value):
        return value + 1

    assert await compute(1) == 2
    assert exception.call_count == 2
    messages = [str(logged.args[0]) for logged in exception.call_args_list]
    assert all(isinstance(logged.args[0], RuntimeError) for logged in exception.call_args_list)
    assert all("cancelled" in message for message in messages)
    assert any("entry" in message for message in messages)
    assert any("exit" in message for message in messages)
    debug.assert_not_called()
