from __future__ import annotations

import re

from beta_agent.extensions import ExtensionAPI, ToolCallDecision

_DANGEROUS_PATTERNS = (
    re.compile(r"\brm\s+(?:-[A-Za-z]*r[A-Za-z]*|--recursive\b)", re.I),
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\b(?:chmod|chown)\b[^\n]*\b0?777\b", re.I),
)
DANGEROUS_PATTERNS = _DANGEROUS_PATTERNS


def is_dangerous_command(command: str) -> bool:
    return any(pattern.search(command) for pattern in _DANGEROUS_PATTERNS)


def permission_gate_extension(api: ExtensionAPI) -> None:
    async def guard(event, ctx):
        del ctx
        if event.tool_call.name != "bash":
            return None
        command = str(getattr(event.args, "command", ""))
        if is_dangerous_command(command):
            return ToolCallDecision(block=True, reason="危险命令被 permission-gate 拦截")
        return None

    api.on("tool_call", guard)


# 保留 Chapter 11 的 extension factory 拼写，兼容现有示例。
extension = permission_gate_extension

__all__ = ["DANGEROUS_PATTERNS", "extension", "is_dangerous_command", "permission_gate_extension"]
