"""Shared result handling and explicit registration of store actions."""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import cast

from alira.modules.database.src.utils.errors import (
    StoreCode,
    exception_code,
    failure_result,
)
from alira.shared.logger import logger

type TaskHandler = Callable[..., Awaitable[dict]]


def service_handler[Handler: TaskHandler](handler: Handler) -> Handler:
    """Return coded operation results while preserving cancellation.

    Args:
        handler: Async operation returning success and messages. Internal
            service results may also carry cache or pagination metadata.

    Returns:
        Wrapped operation defaulting code to 0 while preserving business codes.
        Exceptions become ``{"success": False, "code": int, "messages": str}``.
        Expected failures log without a traceback; unexpected failures retain
        server-side diagnostics. Lifecycle methods remain undecorated.
    """

    @wraps(handler)
    async def wrapper(*args, **kwargs) -> dict:
        try:
            result = await handler(*args, **kwargs)
            if result["success"]:
                result.setdefault("code", int(StoreCode.SUCCESS))
            return result
        except Exception as exc:  # noqa: BLE001 - operation failure boundary
            code = exception_code(exc)
            if code == StoreCode.INTERNAL_ERROR:
                logger.exception("Store operation %s failed", handler.__name__)
            else:
                logger.warning(
                    "Store operation %s failed: %s", handler.__name__, code.name
                )
            return failure_result(code)

    return cast(Handler, wrapper)


def store_action[Handler: TaskHandler](handler: Handler) -> Handler:
    """Expose an executor method under its own name.

    Args:
        handler: Async executor method accepting one action payload dictionary.

    Returns:
        Registered handler using the shared coded result contract.
    """
    wrapped = service_handler(handler)
    wrapped._handler_name = handler.__name__
    return wrapped
