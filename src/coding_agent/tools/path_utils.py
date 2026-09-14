from __future__ import annotations

from pathlib import Path


def resolve_tool_path(cwd: str | Path, path: str) -> Path:
    """Resolve a Coding Tool path relative to ``cwd``.

    This helper deliberately only defines path resolution.  It does not provide
    containment, trust, or sandbox guarantees; those policies belong to a
    separate product/runtime boundary.
    """

    if not isinstance(path, str):
        raise TypeError(f"Tool path must be a string, got {type(path).__name__}")

    try:
        base = Path(cwd).expanduser().resolve()
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        return candidate.resolve()
    except (OSError, RuntimeError) as exc:
        # Keep the user-supplied spelling in the diagnostic so a model can
        # correct a bad path instead of only seeing a normalized path.
        raise ValueError(f"Unable to resolve tool path {path!r}: {exc}") from exc
