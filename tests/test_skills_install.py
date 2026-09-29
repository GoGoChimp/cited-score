"""Tests for skills_install.py - deploying the bundled Rubric skill pack into an AI tool's skills dir.
The target dir is a tmp path so the real ~/.claude/skills is never touched."""
import os
import skills_install, skills_data


def test_bundle_has_the_pack():
    assert "rubric" in skills_data.SKILLS
    assert skills_install.bundled_count() == len(skills_data.SKILLS) >= 9


def test_install_writes_every_skill(tmp_path):
    res = skills_install.install(str(tmp_path))
    assert res["ok"] is True
    assert set(res["installed"]) == set(skills_data.SKILLS)
    for name in skills_data.SKILLS:
        p = tmp_path / name / "SKILL.md"
        assert p.exists()
        assert p.read_text(encoding="utf-8").startswith("---")   # frontmatter intact


def test_installed_reflects_state(tmp_path):
    assert skills_install.installed(str(tmp_path)) == []
    skills_install.install(str(tmp_path))
    assert set(skills_install.installed(str(tmp_path))) == set(skills_data.SKILLS)


def test_install_is_idempotent(tmp_path):
    skills_install.install(str(tmp_path))
    res = skills_install.install(str(tmp_path))   # again, no error, same set
    assert set(res["installed"]) == set(skills_data.SKILLS)
