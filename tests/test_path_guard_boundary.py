"""Home-anchored PathGuard boundary tests."""
from pathlib import Path

import pytest

from modus.policy.path_guard import PathGuard, PathPolicyError


@pytest.fixture
def guard_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def test_relative_path_anchors_to_home(guard_home):
    guard = PathGuard()
    resolved = guard.validate("docs")
    assert resolved == (guard_home / "docs").resolve()


def test_relative_path_anchors_to_base(guard_home):
    guard = PathGuard()
    base = guard_home / "workspace"
    base.mkdir()
    resolved = guard.validate("src/app.py", base=base)
    assert resolved == (base / "src/app.py").resolve()


def test_absolute_path_inside_home_is_allowed(guard_home):
    target = guard_home / "inside.txt"
    target.write_text("x", encoding="utf-8")
    assert PathGuard().validate(str(target)) == target.resolve()


def test_escape_out_of_home_is_rejected(tmp_path, guard_home):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PathPolicyError):
        PathGuard().validate(str(outside))


def test_parent_escape_from_workspace_is_rejected(tmp_path, guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    # ../.. from workspace lands outside home.
    with pytest.raises(PathPolicyError):
        PathGuard().validate("../..", base=workspace)


def test_symlink_escape_out_of_home_is_rejected(tmp_path, guard_home):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = guard_home / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathPolicyError):
        PathGuard().validate(str(link / "secret.txt"))


def test_system_root_is_rejected_even_inside_home(tmp_path, guard_home):
    # A symlink inside home pointing at a system root must be rejected even
    # though the link itself resolves inside home.
    target = Path("/etc")
    link = guard_home / "etc-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink to /etc not permitted in this environment")
    with pytest.raises(PathPolicyError):
        PathGuard().validate(str(link / "hosts"))


def test_is_allowed_is_non_raising(guard_home):
    inside = guard_home / "ok.txt"
    inside.write_text("x", encoding="utf-8")
    guard = PathGuard()
    assert guard.is_allowed(str(inside))
    assert not guard.is_allowed("/etc/hosts")
