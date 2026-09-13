from pathlib import Path

from beta_agent import SkillCatalog


def test_skill_catalog_loads_metadata_not_body(tmp_path: Path):
    skill_dir = tmp_path / "db"
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\nname: database-debugging\ndescription: Diagnose database failures\n---\nSECRET BODY DETAILS\n",
        encoding="utf-8",
    )

    catalog = SkillCatalog.discover(tmp_path)
    prompt = catalog.prompt_fragment()
    assert "database-debugging" in prompt
    assert "Diagnose database failures" in prompt
    assert "SECRET BODY DETAILS" not in prompt
    assert str(path.resolve()) in prompt
