from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    location: Path


class SkillCatalog:
    """只发现 Skill 元数据，不在启动阶段读取并注入完整正文。"""

    def __init__(self, skills: list[Skill]):
        self.skills = skills

    @classmethod
    def discover(cls, root: str | Path) -> "SkillCatalog":
        root_path = Path(root).resolve()
        skills: list[Skill] = []
        for path in sorted(root_path.glob("**/SKILL.md")):
            # 启动阶段只解析 frontmatter，正文仍留在文件系统中。
            # 当模型判断某个 Skill 与任务相关时，再通过普通 read Tool 按需读取正文。
            metadata = _parse_frontmatter(path.read_text(encoding="utf-8"))
            name = metadata.get("name")
            description = metadata.get("description")
            if name and description:
                skills.append(Skill(name=name, description=description, location=path.resolve()))
        return cls(skills)

    def prompt_fragment(self) -> str:
        if not self.skills:
            return ""

        # Catalog 进入 system prompt 的只有 name / description / location。
        # XML 只是为了让结构边界更清晰；escape 防止 Skill 元数据破坏标签结构。
        lines = ["<available_skills>"]
        for skill in self.skills:
            lines.extend(
                [
                    "  <skill>",
                    f"    <name>{escape(skill.name)}</name>",
                    f"    <description>{escape(skill.description)}</description>",
                    f"    <location>{escape(str(skill.location))}</location>",
                    "  </skill>",
                ]
            )
        lines.append("</available_skills>")
        return "\n".join(lines)


def _parse_frontmatter(text: str) -> dict[str, str]:
    # 这里故意保持最小实现，只支持当前 Skill 规范需要的简单 key: value。
    # 如果以后需要多行 YAML、数组等复杂字段，再替换成正式 YAML parser。
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip('"\'')
    return metadata
