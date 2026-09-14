from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar


class CancellationToken:
    """A cooperative cancellation signal used alongside task cancellation."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        # Event.set() is idempotent; keeping cancel idempotent is part of the
        # public contract so callers can safely race or repeat abort().
        self._event.set()

    def throw_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError()

    async def wait(self) -> None:
        await self._event.wait()


T = TypeVar("T")


def accepts_cancellation(fn: Callable[..., Any]) -> bool:
    """Return whether *fn* explicitly accepts the cancellation keyword.

    Signature inspection avoids the dangerous ``TypeError then retry`` pattern:
    a TypeError raised inside a hook is a real hook failure, not evidence that
    the hook has an old signature.
    """

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters.values()
    cancellation_parameter = signature.parameters.get("cancellation")
    return (
        cancellation_parameter is not None
        and cancellation_parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
    ) or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )


async def call_with_optional_cancellation(
    fn: Callable[..., T | Awaitable[T]],
    *args: Any,
    cancellation: CancellationToken | None = None,
) -> T:
    """Call first-party or legacy callbacks with a compatible signature."""

    if cancellation is not None and accepts_cancellation(fn):
        value = fn(*args, cancellation=cancellation)
    else:
        value = fn(*args)
    if inspect.isawaitable(value):
        return await value
    return value
