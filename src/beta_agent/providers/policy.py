from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, Mapping

Transport = Literal["auto", "http", "sse", "websocket"]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    enabled: bool = True
    max_retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        for name in ("base_delay_seconds", "max_delay_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ProviderRequestOptions:
    session_id: str | None = None
    timeout_seconds: float | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    headers: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    transport: Transport = "auto"
    reasoning: str | None = None
    thinking_budget_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and (
            not math.isfinite(self.timeout_seconds) or self.timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must be finite and non-negative")
        if self.thinking_budget_tokens is not None and self.thinking_budget_tokens < 0:
            raise ValueError("thinking_budget_tokens must be non-negative")
        if self.transport not in {"auto", "http", "sse", "websocket"}:
            raise ValueError(f"Unsupported transport: {self.transport!r}")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ProviderRequestOptionsPatch:
    session_id: str | None = None
    timeout_seconds: float | None = None
    retry: RetryPolicy | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    transport: Transport | None = None
    reasoning: str | None = None
    thinking_budget_tokens: int | None = None


def merge_provider_request_options(
    base: ProviderRequestOptions,
    patch: ProviderRequestOptionsPatch | ProviderRequestOptions,
) -> ProviderRequestOptions:
    values: dict[str, Any] = {}
    for name in ("session_id", "timeout_seconds", "retry", "transport", "reasoning", "thinking_budget_tokens"):
        value = getattr(patch, name)
        if value is not None:
            values[name] = value
    values["headers"] = {**base.headers, **dict(patch.headers)}
    values["metadata"] = {**base.metadata, **dict(patch.metadata)}
    return replace(base, **values)

