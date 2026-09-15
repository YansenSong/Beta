from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from beta_agent.skills import SkillCatalog
from beta_agent.tools import Tool


def build_coding_system_prompt(
    cwd: str | Path,
    tools: Sequence[Tool],
    skills: SkillCatalog,
    prefix: str | None = None,
    append: str | None = None,
) -> str:
    """根据 workspace 的实际 capability 组装 product prompt。"""

    workspace = Path(cwd).expanduser().resolve()
    sections: list[str] = []
    if prefix:
        sections.append(prefix)

    sections.append("你是一个编码助手。你可以读取文件、搜索代码、执行命令、精确编辑文件和写入新文件。")

    tool_lines = ["Available tools:"]
    for tool in tools:
        tool_lines.append(f"- {tool.name}: {tool.description}")
    sections.append("\n".join(tool_lines))

    sections.append(
        "Guidelines:\n"
        "- 先探索再修改，先理解相关源码和测试。\n"
        "- 精确修改优先使用 edit；创建或完整覆写文件才使用 write_file。\n"
        "- 修改后运行相关测试或其他验证命令。\n"
        "- 使用清晰、可复现的文件路径，不假设命令已经成功。\n"
        "- Tool 失败时先读取真实错误，再决定下一步。\n"
        "- 不要把没有执行过的动作或没有验证过的结果描述成事实。"
    )

    skill_fragment = skills.prompt_fragment()
    if skill_fragment:
        sections.append(
            "Skills are available as metadata below. Read a relevant SKILL.md with read_file when needed; "
            "the metadata is not the skill body.\n"
            + skill_fragment
        )

    sections.append(f"Current working directory: {workspace}")
    if append:
        sections.append(append)
    return "\n\n".join(sections)
