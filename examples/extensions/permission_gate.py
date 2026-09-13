import re
from beta_agent.extensions import ExtensionAPI, ToolCallDecision
DANGEROUS_PATTERNS=[re.compile(r"\brm\s+(-rf?|--recursive)",re.I),re.compile(r"\bsudo\b",re.I),re.compile(r"\b(chmod|chown)\b.*777",re.I)]
def is_dangerous_command(command:str)->bool:return any(p.search(command) for p in DANGEROUS_PATTERNS)
def extension(pi:ExtensionAPI)->None:
    async def guard(event,ctx):
        if event.tool_call.name!="bash":return None
        command=str(getattr(event.args,"command", ""))
        if is_dangerous_command(command):return ToolCallDecision(block=True,reason="危险命令被 permission-gate 拦截")
        return None
    pi.on("tool_call",guard)
