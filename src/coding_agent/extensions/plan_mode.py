from __future__ import annotations

import re

from beta_agent import Message
from beta_agent.extensions import ExtensionAPI, ToolCallDecision

MUTATING_TOOL_NAMES = frozenset({"write", "write_file", "edit", "delete_file"})
PLAN_MODE_TOOLS = ("bash", "subagent")
PLAN_MODE_PROMPT = "[PLAN MODE ACTIVE] 只读探索模式：不要修改文件，先分析并制定计划。"

_DESTRUCTIVE_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\brm\b",
        r"\brmdir\b",
        r"\bsudo\b",
        r"\bmv\b",
        r"\bcp\b",
        r">",
        r"\bgit\s+(?:add|commit|push|reset|checkout|switch|restore|clean)\b",
    )
)
_SAFE_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"^\s*cat\b",
        r"^\s*ls\b",
        r"^\s*pwd\b",
        r"^\s*echo\b",
        r"^\s*grep\b",
        r"^\s*rg\b",
        r"^\s*head\b",
        r"^\s*tail\b",
        r"^\s*find\b",
        r"^\s*git\s+(?:status|diff|log|show)\b",
    )
)


def is_safe_command(command: str) -> bool:
    """返回 Plan Mode active 时是否允许执行该 shell command。"""

    return not any(pattern.search(command) for pattern in _DESTRUCTIVE_PATTERNS) and any(
        pattern.search(command) for pattern in _SAFE_PATTERNS
    )


def plan_mode_extension(api: ExtensionAPI) -> None:
    """为 Coding Agent 注册 opt-in 的只读 Plan Mode。

    extension 默认处于 disabled。``/plan`` 会保存当前 active tool 快照，移除 mutating tool，
    加入 Runtime 中实际可用的 Plan Mode helper，并向 model context 注入 planning instruction。
    再次执行 ``/plan`` 时，会精确恢复进入 Plan Mode 前的 tool 快照。
    """

    enabled = False
    tools_before: list[str] | None = None

    async def toggle(_args, ctx) -> None:
        nonlocal enabled, tools_before
        enabled = not enabled
        if enabled:
            tools_before = ctx.get_active_tools()
            requested = [name for name in tools_before if name not in MUTATING_TOOL_NAMES]
            requested.extend(name for name in PLAN_MODE_TOOLS if name not in requested)
            ctx.set_active_tools(requested)
            return

        ctx.set_active_tools(tools_before or ctx.get_active_tools())
        tools_before = None

    api.register_command("plan", description="切换只读 Plan Mode", handler=toggle)

    async def guard(event, ctx):
        del ctx
        if not enabled or event.tool_call.name != "bash":
            return None
        command = str(getattr(event.args, "command", ""))
        if is_safe_command(command):
            return None
        return ToolCallDecision(block=True, reason=f"plan 模式禁止命令: {command}")

    api.on("tool_call", guard)

    async def inject_context(event, ctx):
        del ctx
        if not enabled:
            return None
        return [
            *event.messages,
            Message.user(PLAN_MODE_PROMPT, extension="plan_mode"),
        ]

    api.on("context", inject_context)


# directory-based extension loading 使用的常规 module-level factory 名称。
extension = plan_mode_extension

__all__ = [
    "MUTATING_TOOL_NAMES",
    "PLAN_MODE_PROMPT",
    "PLAN_MODE_TOOLS",
    "extension",
    "is_safe_command",
    "plan_mode_extension",
]
