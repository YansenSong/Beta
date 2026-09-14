import re
from beta_agent import Message
from beta_agent.extensions import ExtensionAPI, ToolCallDecision
DESTRUCTIVE=[re.compile(p,re.I) for p in [r"\brm\b",r"\brmdir\b",r"\bsudo\b",r"\bmv\b",r"\bcp\b",r">",r"\bgit\s+(add|commit|push|reset|checkout)\b"]]
SAFE=[re.compile(p,re.I) for p in [r"^\s*cat\b",r"^\s*ls\b",r"^\s*pwd\b",r"^\s*echo\b",r"^\s*grep\b",r"^\s*head\b",r"^\s*tail\b",r"^\s*find\b"]]
def is_safe_command(command:str)->bool:return not any(p.search(command) for p in DESTRUCTIVE) and any(p.search(command) for p in SAFE)
PLAN_MODE_TOOLS=["bash","subagent"]

def extension(pi:ExtensionAPI)->None:
    enabled=False; tools_before=None
    async def command(_args,ctx):
        nonlocal enabled,tools_before
        enabled=not enabled
        if enabled:
            tools_before=ctx.get_active_tools(); ctx.set_active_tools(list(dict.fromkeys([n for n in tools_before if n not in {"write","write_file","edit","delete_file"}] + PLAN_MODE_TOOLS)))
        else:
            ctx.set_active_tools(tools_before or ctx.get_active_tools()); tools_before=None
    pi.register_command("plan",description="切换只读 Plan Mode",handler=command)
    async def guard(event,ctx):
        if enabled and event.tool_call.name=="bash":
            command=str(getattr(event.args,"command",""))
            if not is_safe_command(command):return ToolCallDecision(block=True,reason=f"plan 模式禁止命令: {command}")
        return None
    pi.on("tool_call",guard)
    async def context(event,ctx):
        if not enabled:return None
        return [*event.messages,Message.user("[PLAN MODE ACTIVE] 只读探索模式：不要修改文件，先分析并制定计划。",extension="plan_mode")]
    pi.on("context",context)
