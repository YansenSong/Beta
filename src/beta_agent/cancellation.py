"""Compatibility facade for :mod:`beta_agent.runtime.cancellation`."""
from .runtime.cancellation import CancellationToken, accepts_cancellation, call_with_optional_cancellation

__all__ = ["CancellationToken", "accepts_cancellation", "call_with_optional_cancellation"]
