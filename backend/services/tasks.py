"""Small cancellation-safe task helpers for owned writes and shutdown."""

import asyncio
import logging
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar, cast

logger = logging.getLogger("streamrip")

T = TypeVar("T")


async def await_task_completion(
    task: asyncio.Future[T],
    *,
    operation: str,
    suppress_inner_cancellation: bool = False,
) -> T | None:
    """Shield *task* through repeated caller cancellation, then propagate it.

    The caller remains cancelled: cancellation is only delayed until the owned
    task terminates. A cancellation originating from the inner task is handled
    separately so an already-cancelled inner task cannot make this loop spin.
    """
    caller = asyncio.current_task()
    initial_cancels = caller.cancelling() if caller is not None else 0
    caller_cancelled = False

    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            new_caller_cancel = (
                caller is not None and caller.cancelling() > initial_cancels
            )
            caller_cancelled = caller_cancelled or new_caller_cancel
            if not task.done():
                continue
            if task.cancelled():
                if caller_cancelled or not suppress_inner_cancellation:
                    raise
                return None
            try:
                result = task.result()
            except Exception as error:
                if not caller_cancelled:
                    raise
                logger.error(
                    "%s failed while caller cancellation was pending",
                    operation,
                    exc_info=(type(error), error, error.__traceback__),
                )
                raise asyncio.CancelledError from None
        except Exception as error:
            if not caller_cancelled and not (
                caller is not None and caller.cancelling() > initial_cancels
            ):
                raise
            logger.error(
                "%s failed while caller cancellation was pending",
                operation,
                exc_info=(type(error), error, error.__traceback__),
            )
            raise asyncio.CancelledError from None
        break

    if caller_cancelled or (
        caller is not None and caller.cancelling() > initial_cancels
    ):
        raise asyncio.CancelledError
    return result


async def run_thread_write(
    function: Callable[..., T],
    /,
    *args: Any,
    operation: str,
    **kwargs: Any,
) -> T:
    """Run a blocking write off-loop and retain it through cancellation."""
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, partial(function, *args, **kwargs))
    result = await await_task_completion(future, operation=operation)
    return cast(T, result)
