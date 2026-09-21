from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar


class CancellationToken:
    """与 task cancellation 配合使用的协作式 cancellation signal。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        # Event.set() 本身是幂等的；cancel 保持幂等属于 public contract，
        # 这样调用方即使并发竞争或重复调用 abort() 也能安全处理。
        self._event.set()

    def throw_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError()

    async def wait(self) -> None:
        await self._event.wait()


T = TypeVar("T")


def accepts_cancellation(fn: Callable[..., Any]) -> bool:
    """返回 *fn* 是否显式接收 cancellation keyword。

    通过检查 signature 避免危险的 ``TypeError then retry`` 模式：
    hook 内部抛出的 TypeError 是真实的 hook failure，并不能说明
    hook 使用的是旧 signature。
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

# 调用一个可能支持、也可能不支持cancellation 参数的函数（调用函数的）
async def call_with_optional_cancellation(
    fn: Callable[..., T | Awaitable[T]],
    *args: Any,
    cancellation: CancellationToken | None = None,
) -> T:
    """使用兼容的 signature 调用 first-party 或 legacy callback。"""

    if cancellation is not None and accepts_cancellation(fn):
        value = fn(*args, cancellation=cancellation)
    else:
        value = fn(*args)
    if inspect.isawaitable(value):
        return await value
    return value
