"""Kernel-owned Apixis lifecycle and event communication facade."""

import apixis


class ApixisCore:
    """Kernel lifecycle service exposing Apixis 3.0.0 communication APIs.

    Import bus and event types from ``alira.kernel.comlink``. The kernel
    starts this service before modules and stops it afterwards. Instances share
    Apixis's process-global runtime; constructing another instance creates no
    isolated bus and starts no background services.

    Communication methods:
        subscribe(*event_names, **options): Register a handler for case-sensitive
            event-name/glob patterns. Handler names are process-global; use a
            module-specific name and group, and register during startup.
        unsubscribe(handler_name=None, *, group=None, missing_ok=True): Remove
            one named handler or an exact group. Supply exactly one selector.
            Already captured dispatch chains retain their handler references.
        post_event(*, event_type=EventType.DEFAULT, event_name, context=None):
            Publish an event, waiting for queue admission rather than processing.
        post_and_wait(*, event_type=EventType.DEFAULT, event_name, context=None,
            time_out=None): Publish and wait for this exact
            event's foreground processing, correlated by event_id. Inspect the
            returned event's has_error/error_stack for handler failures. Background
            handlers and events published by handlers are not awaited. A timeout
            or cancellation does not retract admitted events or stop their handlers.
        expect_event(event_name, *, point="received", filter=None, predicate=None,
            time_out=None): Async context manager registering
            listeners before the body executes. Publish inside it, then call
            waiter.wait(). Replies received before wait() are retained.
        wait_event(event_name, **wait_options): Register and immediately wait.
            Registration begins when this coroutine executes, not when it is
            constructed or scheduled; use expect_event to register before publishing.
        get_handler_meta(handler_name): Return an independent metadata snapshot
            of the registered handler, or None.
        is_registered(handler_name): Return whether that handler name is registered.
        get_event_runtime_status(): Read an immutable runtime status snapshot on
            the owning loop without creating or starting the core.

    Wait options:
        point selects receipt before ordinary foreground handlers or processing
        after them; background handlers are not awaited. A string pattern returns
        one ApixEvent; a list/set returns a pattern/event mapping after all patterns
        match. filter excludes glob matches; predicate synchronously tests event
        contents on the owner loop and fails the wait if it raises.
        time_out is seconds or None for no deadline. expect_event fixes its
        deadline at registration; waiter.wait() preserves it. post_and_wait bounds
        queue admission and processing together. Timeout raises TimeoutError.
        Core closure or cancellation of a matching event returns None. Completion,
        timeout, cancellation and context exit release listeners. Use an outer
        asyncio.timeout to limit an entire request/reply flow.

    Notes:
        Communication methods are direct Apixis aliases, retaining upstream
        signatures, defaults and docstrings. Create, register, publish and wait
        on the shared asyncio loop. The kernel starts the core before modules
        register subscriptions or compile graphs. Remove module subscription
        groups and stop owned tasks before kernel shutdown.

    Example:
        import asyncio
        from uuid import uuid4

        from alira.kernel.comlink import EventType, bus

        reply_name = f"example.reply.{uuid4().hex}"
        async with asyncio.timeout(30):
            async with bus.expect_event(reply_name) as waiter:
                await bus.post_event(
                    event_type=EventType.TRANSIENT,
                    event_name="example.request",
                    context={"reply_to": reply_name},
                )
                response = await waiter.wait()
    """

    subscribe = staticmethod(apixis.subscribe)
    unsubscribe = staticmethod(apixis.unsubscribe)
    post_event = staticmethod(apixis.post_event)
    post_and_wait = staticmethod(apixis.post_and_wait)
    expect_event = staticmethod(apixis.expect_event)
    wait_event = staticmethod(apixis.wait_event)
    get_handler_meta = staticmethod(apixis.get_handler_meta)
    is_registered = staticmethod(apixis.is_registered)
    get_event_runtime_status = staticmethod(apixis.get_event_runtime_status)

    async def start(self) -> None:
        """Create and start the shared event core on the owning asyncio loop.

        Returns:
            None after Apixis reaches CoreState.RUNNING. This does not wait for
            queued events to finish. Repeated starts preserve the live core and
            its subscriptions; starting after stop creates a fresh core.

        Raises:
            Exception: An upstream startup or owner-loop error, without translation.
        """
        apixis.create_core()
        await apixis.start_core()

    async def stop(self) -> None:
        """Stop consumption and permanently close the shared event core.

        Returns:
            None after Apixis reaches CoreState.CLOSED, with queued events
            discarded, active event work cancelled and subscriptions released.
            Safe before creation and on repeated calls. Use the owning loop.

        Notes:
            Modules must stop owned tasks and unregister handlers beforehand.
            Close is attempted even if stopping consumption fails.
            Upstream shutdown and owner-loop errors propagate without translation.
        """
        try:
            await apixis.stop_core()
        finally:
            await apixis.close_core()

    def get_version(self) -> str:
        """Return the installed Apixis version string without starting the core."""
        return apixis.VERSION


bus = ApixisCore()
