import os
import stat
import time

import pytest

from modus.skills import (
    SKILL_ACTIVE,
    SKILL_ARCHIVED,
    SKILL_STALE,
    SkillRepository,
)


def test_skill_repository_save_list_get_delete(tmp_path):
    repository = SkillRepository(tmp_path / "skills")

    created = repository.save(name="review-code", description="Code review", prompt="Review this code:")

    assert created.name == "review-code"
    assert repository.list_public() == [{"name": "review-code", "description": "Code review", "prompt": "Review this code:"}]
    assert repository.get("review-code").prompt == "Review this code:"
    assert stat.S_IMODE(os.stat(created.path).st_mode) == 0o600

    repository.delete("review-code")
    assert repository.list() == []


@pytest.mark.parametrize("name", ["../escape", "Contains Space", "UPPER", "", "a" * 65])
def test_skill_names_are_strict(tmp_path, name):
    with pytest.raises(ValueError):
        SkillRepository(tmp_path).save(name=name, description="", prompt="prompt")


def test_invalid_skill_file_is_not_exposed(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "bad.json").write_text('{"name":"Bad Name","prompt":"x"}')

    assert SkillRepository(root).list_public() == []


# ── Wave5 E2: skill lifecycle (active/stale/archived) + usage sidecar ──


def test_new_skill_is_active_with_usage_sidecar(tmp_path):
    repository = SkillRepository(tmp_path / "skills")
    created = repository.save(name="lifecycle", description="d", prompt="do it")

    assert created.status == SKILL_ACTIVE
    assert created.last_activity_at > 0
    assert (tmp_path / "skills" / "lifecycle.usage.json").exists()
    # to_wire stays backward compatible; lifecycle fields via to_wire_lifecycle.
    assert repository.list_public() == [{"name": "lifecycle", "description": "d", "prompt": "do it"}]
    wire = repository.list_public_lifecycle()[0]
    assert wire["status"] == SKILL_ACTIVE
    assert wire["usage_count"] == 0


def test_mark_used_bumps_sidecar_and_keeps_active(tmp_path):
    repository = SkillRepository(tmp_path / "skills")
    repository.save(name="used-skill", description="d", prompt="do it")

    assert repository.mark_used("used-skill") is True
    skill = repository.get("used-skill")
    assert skill.usage_count == 1
    assert skill.status == SKILL_ACTIVE

    assert repository.mark_used("used-skill") is True
    assert repository.get("used-skill").usage_count == 2
    # Unknown skill is a no-op, not an error.
    assert repository.mark_used("does-not-exist") is False


def test_curator_demotes_active_to_stale(tmp_path):
    repository = SkillRepository(tmp_path / "skills")
    repository.save(name="idle", description="d", prompt="do it")
    now = time.time()
    # The skill has had no activity for stale_after + margin.
    changed = repository.curate(now=now + 10, stale_after=1, archive_after=2)

    assert changed == ["idle"]
    assert repository.get("idle").status == SKILL_STALE


def test_curator_demotes_stale_to_archived(tmp_path):
    repository = SkillRepository(tmp_path / "skills")
    repository.save(name="older", description="d", prompt="do it")
    now = time.time()
    repository.curate(now=now + 10, stale_after=1, archive_after=2)
    assert repository.get("older").status == SKILL_STALE

    # Later curation with stale for > archive_after → archived.
    changed = repository.curate(now=now + 30, stale_after=1, archive_after=2)
    assert changed == ["older"]
    assert repository.get("older").status == SKILL_ARCHIVED
    # Archived skills are recoverable, never deleted.
    assert (tmp_path / "skills" / "older.json").exists()


def test_curator_never_deletes_and_active_use_resists_stale(tmp_path, monkeypatch):
    import modus.skills as skills_mod

    clock = {"t": 1_000.0}
    monkeypatch.setattr(skills_mod, "_now", lambda: clock["t"])
    repository = SkillRepository(tmp_path / "skills")
    repository.save(name="busy", description="d", prompt="do it")
    # Busy skill was used just now → stays active despite a stale threshold.
    clock["t"] += 0.1
    repository.mark_used("busy")
    changed = repository.curate(now=clock["t"] + 0.2, stale_after=1, archive_after=2)
    assert changed == []
    assert repository.get("busy").status == SKILL_ACTIVE


def test_skill_lifecycle_stale_archive(tmp_path):
    """超期 → active→stale→archived（验收测试）。"""
    repository = SkillRepository(tmp_path / "skills")
    repository.save(name="lifecycle-probe", description="d", prompt="p")
    now = time.time()

    repository.curate(now=now + 10, stale_after=1, archive_after=2)
    assert repository.get("lifecycle-probe").status == SKILL_STALE
    repository.curate(now=now + 30, stale_after=1, archive_after=2)
    assert repository.get("lifecycle-probe").status == SKILL_ARCHIVED
    # mark_used revives an archived skill.
    repository.mark_used("lifecycle-probe")
    assert repository.get("lifecycle-probe").status == SKILL_ACTIVE
    assert repository.get("lifecycle-probe").usage_count == 1
