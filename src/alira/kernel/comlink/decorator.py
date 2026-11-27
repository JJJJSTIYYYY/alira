"""Decorator-based extension points for asynchronous module callables.

The decorator is restricted to functions defined under ``alira.modules`` and
prefixes extension events with the owning module directory name.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any, Literal

from apixis import ApixEvent, EventType, post_and_wait

from alira.shared import logger

type ComlinkPoint = Literal["entry", "exit"]

_MODULES_PACKAGE = "alira.modules"
_MODULES_PACKAGE_PARTS = tuple(_MODULES_PACKAGE.split("."))


@dataclass(slots=True)
class ComlinkData:
    """Call data exposed to comlink extension handlers.

    Handlers replace ``args`` or ``kwargs`` at the entry point and ``returns``
    at the exit point. The decorator reads the payload back from the processed
    event, so each extension point can modify the decorated call.

    Additionally, handlers can set ``refuse`` to ``True`` at the entry
    point to skip the decorated function and return ``returns`` instead.
    """

    point: ComlinkPoint
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    returns: Any = None
    refuse: bool = False


def _completed_data(
    event: ApixEvent | None,
    point: ComlinkPoint,
    fallback: ComlinkData,
) -> ComlinkData:
    """Return the processed payload, logging cancelled or failed events."""
    if event is None:
        logger.exception(
            RuntimeError(
                f"Comlink {point} extension point was timeout, cancelled or the event core closed."
            )
        )
        return fallback
    if event.has_error:
        logger.warning(
            RuntimeError(
                f"Comlink {point} extension point failed: {event.error_stack}"
            )
        )
    else:
        logger.debug(f"Comlink {point} extension point completed by: {event.seen}")
    return event.context


def method_comlink(
    extension_point: Literal["entry", "exit", "both"],
    extension_name: str | None = None,
    time_out: float | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Run extension events before and after an asynchronous function.

    Args:
        extension_point: Which sides of the call publish an event. ``"entry"``
            posts the entry event with the call arguments before invocation;
            ``"exit"`` posts the exit event with the return value afterwards;
            ``"both"`` does both.
        extension_name: Base name of the extension events within the module.
            The decorator posts ``<module>.<extension_name>.entry`` and
            ``<module>.<extension_name>.exit``; ``None`` uses the decorated
            function's ``__name__`` as the base name.

    Returns:
        A decorator wrapping the async function. Handlers mutate the event's
        :class:`ComlinkData` payload to replace arguments or the return value.

    Raises:
        ValueError: If ``extension_point`` is unsupported or the decorated
            function is not defined under ``alira.modules``.
        TypeError: If the decorated object is not an async function.
    """
    if extension_point not in ("entry", "exit", "both"):
        raise ValueError(
            f"Invalid extension_point '{extension_point}'; "
            "must be 'entry', 'exit' or 'both'."
        )

    dispatch_entry = extension_point in ("entry", "both")
    dispatch_exit = extension_point in ("exit", "both")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not iscoroutinefunction(func):
            raise TypeError(f"method_comlink can only decorate async functions, got {func!r}.")

        module = func.__module__
        module_parts = module.split(".")
        if tuple(module_parts[:2]) != _MODULES_PACKAGE_PARTS or len(module_parts) < 3:
            raise ValueError(
                f"method_comlink can only decorate functions in a module under the "
                f"'{_MODULES_PACKAGE}' package (src/alira/modules), got {module!r}."
            )

        event_name = f"{module_parts[2]}.{extension_name or func.__name__}"
        entry_event = f"{event_name}.entry"
        exit_event = f"{event_name}.exit"

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if dispatch_entry:
                payload = ComlinkData("entry", args, kwargs)
                data = _completed_data(
                    await post_and_wait(
                        event_type=EventType.WORKFLOW,
                        event_name=entry_event,
                        context=payload,
                        time_out=time_out
                    ),
                    "entry",
                    payload,
                )
                if not isinstance(data, ComlinkData):
                    raise TypeError(
                        f"Comlink {entry_event} returned unexpected type: {type(data)}"
                    )
                if data.refuse:
                    return data.returns
                args, kwargs = data.args, data.kwargs

            returns = await func(*args, **kwargs)

            if dispatch_exit:
                payload = ComlinkData("exit", returns=returns)
                data = _completed_data(
                    await post_and_wait(
                        event_type=EventType.WORKFLOW,
                        event_name=exit_event,
                        context=payload,
                        time_out=time_out
                    ),
                    "exit",
                    payload,
                )
                if not isinstance(data, ComlinkData):
                    raise TypeError(
                        f"Comlink {exit_event} returned unexpected type: {type(data)}"
                    )
                returns = data.returns

            return returns

        return wrapper

    return decorator


__all__ = ["ComlinkData", "method_comlink"]
