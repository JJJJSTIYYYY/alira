"""Shared failure responses and explicit registration of store actions."""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import cast

from alira.common.utils.logger import logger

type TaskHandler = Callable[..., Awaitable[dict]]
type FailureFactory = Callable[[Exception], dict]


def service_handler[Handler: TaskHandler](
    func: Handler | None = None,
    *,
    failure_factory: FailureFactory | None = None,
) -> Handler | Callable[[Handler], Handler]:
    """Return ordinary operation failures without swallowing cancellation.

    Services and executor actions use the same result contract. Lifecycle
    methods deliberately remain undecorated so startup/shutdown errors propagate.
    """

    def decorator(handler: Handler) -> Handler:
        @wraps(handler)
        async def wrapper(*args, **kwargs) -> dict:
            try:
                return await handler(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - operation failure boundary
                logger.exception(f"Service handler `{handler.__name__}` failed: {exc}")
                if failure_factory is not None:
                    return failure_factory(exc)
                return {
                    "success": False,
                    "messages": f"fail: {exc}",
                }

        return cast(Handler, wrapper)

    if func is None:
        return decorator
    return decorator(func)


def store_action[Handler: TaskHandler](handler: Handler) -> Handler:
    """Expose an executor method under its own name, using the service contract."""
    wrapped = cast(Handler, service_handler(handler))
    wrapped._handler_name = handler.__name__
    return wrapped


def identity_failure(exc: Exception) -> dict:
    """Keep user creation and verification failures in their identity format."""
    return {
        "success": False,
        "messages": {"msg": f"{type(exc).__name__}: {exc}", "uid": None},
    }
