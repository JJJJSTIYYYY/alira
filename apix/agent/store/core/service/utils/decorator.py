from functools import wraps
from typing import Awaitable, Callable, TypeVar, cast

from apix.common.utils.logger import logger


TaskHandler = TypeVar(
    "TaskHandler",
    bound=Callable[..., Awaitable[dict]],
)
FailureFactory = Callable[[Exception], dict]


def service_handler(
    func: TaskHandler | None = None,
    *,
    failure_factory: FailureFactory | None = None,
) -> TaskHandler | Callable[[TaskHandler], TaskHandler]:
    """Trace service calls and normalize their unhandled failures."""

    def decorator(handler: TaskHandler) -> TaskHandler:
        @wraps(handler)
        async def wrapper(*args, **kwargs) -> dict:
            try:
                return await handler(*args, **kwargs)
            except Exception as exc:
                logger.exception(
                    f"Service handler `{handler.__name__}` failed: {exc}"
                )
                if failure_factory is not None:
                    return failure_factory(exc)
                return {
                    "success": False,
                    "messages": f"fail: {exc}",
                }

        return cast(TaskHandler, wrapper)

    if func is None:
        return decorator
    return decorator(func)
