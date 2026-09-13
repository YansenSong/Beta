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
    def __init__(self, skills: list[Skill]):
        self.skills = skills

    @classmethod
    def discover(cls, root: str | Path) -> "SkillCatalog":
        root_path = Path(root).resolve()
        skills: list[Skill] = []
        for path in sorted(root_path.glob("**/SKILL.md")):
            metadata = _parse_frontmatter(path.read_text(encoding="utf-8"))
            name = metadata.get("name")
            description = metadata.get("description")
            if name and description:
                skills.append(Skill(name=name, description=description, location=path.resolve()))
        return cls(skills)

    def prompt_fragment(self) -> str:
        if not self.skills:
            return ""
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
