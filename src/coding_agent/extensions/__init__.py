from .permission_gate import DANGEROUS_PATTERNS, is_dangerous_command, permission_gate_extension
from .plan_mode import (
    MUTATING_TOOL_NAMES,
    PLAN_MODE_PROMPT,
    PLAN_MODE_TOOLS,
    is_safe_command,
    plan_mode_extension,
)
from .subagent import CHILD_MODEL_FACTORY_SERVICE, SubagentArgs, subagent_extension

# Preserve the package's original default factory spelling for compatibility.
extension = permission_gate_extension

__all__ = [
    "CHILD_MODEL_FACTORY_SERVICE",
    "DANGEROUS_PATTERNS",
    "MUTATING_TOOL_NAMES",
    "PLAN_MODE_PROMPT",
    "PLAN_MODE_TOOLS",
    "SubagentArgs",
    "extension",
    "is_dangerous_command",
    "is_safe_command",
    "permission_gate_extension",
    "plan_mode_extension",
    "subagent_extension",
]
