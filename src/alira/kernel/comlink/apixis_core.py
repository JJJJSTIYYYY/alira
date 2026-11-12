"""Kernel-owned Apixis lifecycle and event communication facade."""

from typing import Any

import apixis
from apixis import EventType


class ApixisCore:
    """Kernel-owned lifecycle service and plugin event communication facade.

    Import apixis_core and event types from ``alira.kernel.comlink``. The kernel
    starts this service before modules and stops it afterwards. Instances share
    Apixis's process-global runtime; constructing another instance creates no
    isolated bus and starts no background services.

    Communication methods:
        subscribe(*event_names, **options): Apixis decorator for case-sensitive
            event-name/glob patterns. Options include group, priority, filters,
            timeout and background dispatch. Handler names are process-global;
            use module-specific names and register during startup.
        unsubscribe(handler_name=None, *, group=None, missing_ok=True): Remove
            one named handler or an exact group. Supply exactly one selector.
            Unknown selections are ignored unless missing_ok is false. Already
            captured dispatch chains are not revoked.
        wait_for_event(event_name, *, point="received", filter=None,
            time_out=None, keep_listening=False): Async context manager yielding
            an EventWaiter. Entry registers listeners before the body executes.
            Call waiter.wait() after publishing; it returns an ApixEvent for one
            string pattern, or a pattern/event dict for a list or set of patterns.
        await_for(event_name, **wait_options): Register and immediately wait,
            returning the same event/dict result, or an EventWaiter on a deferred
            timeout when keep_listening is true. Registration begins only when
            this coroutine executes, not when it is constructed or scheduled.
        get_handler(handler_name): Return the registered ApixEventHandler or None.
        is_registered(handler_name): Return whether that handler name is registered.

    Wait options:
        point selects receipt before foreground handlers or processing after
        them; background handlers are not awaited. filter excludes glob matches.
        time_out is seconds or None for no deadline; expiry normally raises
        TimeoutError. With keep_listening true, the returned waiter may remain
        registered: await its get_result() or close it when abandoning the wait.
        waiter.wait(time_out=...) sets its own deadline; omitting that argument
        clears a previously configured deadline. Normal completion, exceptions
        and cancellation clean up non-deferred listeners.

    Notes:
        The six subscription/query methods are direct Apixis aliases, preserving
        their native signatures and documentation. Use wait_for_event for
        request/reply readiness, and unique/correlated result names for concurrent
        requests. Remove a module's subscription group during shutdown. Bus objects
        and subscriptions belong to the owning event loop; these methods are not
        a bridge for publishing from worker threads.

    Example:
        from uuid import uuid4

        from alira.kernel.comlink import EventType, apixis_core

        # Within an async function, register before publishing the request.
        reply_name = f"example.reply.{uuid4().hex}"
        async with apixis_core.wait_for_event(reply_name) as waiter:
            await apixis_core.post_event(
                event_type=EventType.TRANSIENT,
                event_name="example.request",
                context={"reply_to": reply_name},
            )
            response = await waiter.wait(time_out=30)
    """

    subscribe = staticmethod(apixis.subscribe)
    unsubscribe = staticmethod(apixis.unsubscribe)
    wait_for_event = staticmethod(apixis.wait_for_event)
    await_for = staticmethod(apixis.await_for)
    get_handler = staticmethod(apixis.get_handler)
    is_registered = staticmethod(apixis.is_registered)

    async def start(self) -> None:
        """Start the shared Apixis core as part of the kernel lifecycle.

        Returns:
            None once Apixis has scheduled its consumers. This does not wait for queued
            events to complete. Repeated starts reuse the framework's shared services.

        Raises:
            Exception: A framework startup failure, propagated without translation.
        """
        await apixis.start_core()

    async def stop(self) -> None:
        """Pause the shared event core without constructing it if it has not been used.

        Returns:
            None after the framework consumer/pipe stop operations complete.

        Notes:
            Apixis retains components, subscriptions and queued events for restart;
            in-flight dispatch/background handlers are not joined by this call. Modules
            must stop owned tasks and unregister handlers before the kernel stops it.
        """
        await apixis.stop_core()

    async def post_event(
        self, *, event_type: EventType, event_name: str, context: Any = None
    ) -> None:
        """Create and publish an event on the shared Apixis pipe.

        Args:
            event_type: Apixis event category, such as EventType.TRANSIENT.
            event_name: Name matched against registered handler subscription patterns.
            context: Optional business payload or runtime context. The facade forwards
                the same object without copying or serializing it.

        Returns:
            None once queue admission completes. This waits for backpressure capacity,
            not handler execution or a business result. Use wait_for_event for replies.

        Raises:
            Exception: An event-core startup, event validation or queue error from Apixis.
            asyncio.CancelledError: Publication was cancelled while awaiting admission.

        Notes:
            Uses the current event-loop runtime and Apixis's dispatch-permit/context
            propagation rules. This method is not safe to call from a different thread
            by running a second asyncio loop.
        """
        pipe = await apixis.aget_event_pipe()
        await pipe.post_event(
            event_type=event_type, event_name=event_name, context=context
        )

    def get_version(self) -> str:
        """Return the installed Apixis version string without starting the core."""
        return apixis.VERSION


apixis_core = ApixisCore()
