"""Provider-agnostic runtime primitives.

Keep this package initializer intentionally lightweight. Core modules such as
`types.py` import `runtime.errors` during bootstrap, so eager re-exports of
`runtime.events` would create a circular import back into `types.py`.

Import concrete primitives from their submodules, for example
`beta_agent.runtime.events` or `beta_agent.runtime.cancellation`.
"""

__all__: list[str] = []
