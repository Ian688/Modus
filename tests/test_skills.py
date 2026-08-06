import os
import stat

import pytest

from modus.skills import SkillRepository


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
