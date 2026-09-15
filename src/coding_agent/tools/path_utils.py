from __future__ import annotations

from pathlib import Path


def resolve_tool_path(cwd: str | Path, path: str) -> Path:
    """相对 ``cwd`` 解析 Coding Tool path。

    这个 helper 刻意只定义 path resolution，不提供 containment、trust 或 sandbox guarantee；
    这些 policy 属于独立的产品/Runtime boundary。
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
        # diagnostic 中保留用户原始输入的拼写，这样 model 能修正错误 path，
        # 而不只是看到一个 normalized path。
        raise ValueError(f"Unable to resolve tool path {path!r}: {exc}") from exc
