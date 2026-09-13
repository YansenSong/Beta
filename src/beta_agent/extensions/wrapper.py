from __future__ import annotations
import inspect
from typing import Any
from ..tools import Tool
from ..types import ToolResult
from .types import ExtensionTool

def wrap_registered_tool(registered: ExtensionTool[Any], runner) -> Tool[Any]:
    async def execute(args, tool_ctx):
        active_before = runner.get_active_tools()
        value = registered.handler(args, runner.create_context(), tool_ctx)
        if inspect.isawaitable(value):
            value = await value
        result = value if isinstance(value, ToolResult) else ToolResult(content=str(value))
        active_after = runner.get_active_tools()
        # Match Pi's guard: if execution removed any previously active tool, suppress addedToolNames.
        if all(name in active_after for name in active_before):
            before = set(active_before)
            added = [name for name in active_after if name not in before]
            if added:
                result.added_tool_names = list(dict.fromkeys([*result.added_tool_names, *added]))
        return result
    return Tool(name=registered.name, description=registered.description, args_model=registered.args_model, handler=execute, execution_mode=registered.execution_mode)
