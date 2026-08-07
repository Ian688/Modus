"""Read-only git history tools: git_log / git_show / git_blame.

Unlock feature-context, refactor, regression-triage, version-compare, debug and
history-aware symbol searches (function-map #2).  All read-only, output-bounded,
and never require approval.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

from modus.config import ModusConfig
from modus.tools.base import ToolContext
from modus.tools.builtins import get_builtin_tools
from modus.tools.git_tools import git_blame, git_log, git_show


@pytest.fixture
def git_repo(tmp_path):
    """A small git repo with two commits touching a file."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "first"], check=True)
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "second"], check=True)
    return tmp_path


def _ctx(repo):
    return ToolContext(cwd=str(repo), config=ModusConfig())


@pytest.mark.asyncio
async def test_git_log_lists_commits(git_repo):
    result = await git_log({"count": 10}, _ctx(git_repo))
    assert not result.is_error
    assert "first" in result.content
    assert "second" in result.content


@pytest.mark.asyncio
async def test_git_log_count_bounded(git_repo):
    result = await git_log({"count": 1}, _ctx(git_repo))
    lines = [l for l in result.content.splitlines() if l.strip()]
    assert len(lines) <= 1


@pytest.mark.asyncio
async def test_git_log_path_restriction(git_repo):
    (git_repo / "other.txt").write_text("z\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "add other"], check=True)
    result = await git_log({"count": 10, "path": "a.py"}, _ctx(git_repo))
    assert not result.is_error
    assert "other" not in result.content  # commit that only touched other.txt
    assert "second" in result.content


@pytest.mark.asyncio
async def test_git_show_shows_commit(git_repo):
    result = await git_show({"rev": "HEAD"}, _ctx(git_repo))
    assert not result.is_error
    assert "second" in result.content
    assert "a.py" in result.content  # touched file


@pytest.mark.asyncio
async def test_git_show_stat_mode(git_repo):
    result = await git_show({"rev": "HEAD", "stat": "stat"}, _ctx(git_repo))
    assert not result.is_error
    assert "second" in result.content


@pytest.mark.asyncio
async def test_git_show_requires_rev(git_repo):
    result = await git_show({}, _ctx(git_repo))
    assert result.is_error
    assert "rev" in result.content


@pytest.mark.asyncio
async def test_git_blame_attributes_lines(git_repo):
    result = await git_blame({"path": "a.py"}, _ctx(git_repo))
    assert not result.is_error
    # Line 2 (y = 2) belongs to the "second" commit.
    assert "second" in result.content or "2)" in result.content


@pytest.mark.asyncio
async def test_git_blame_requires_path(git_repo):
    result = await git_blame({}, _ctx(git_repo))
    assert result.is_error
    assert "path" in result.content


def test_git_history_tools_declared_read_only():
    tools = {t.name: t for t in get_builtin_tools()}
    for name in ("git_log", "git_show", "git_blame"):
        tool = tools[name]
        assert tool.is_read_only is True
        assert tool.danger_level == "safe"
        assert tool.requires_approval is False
        assert "exec" in tool.capabilities
