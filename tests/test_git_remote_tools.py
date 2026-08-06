"""Host git remote/branch tools + git credential manager."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from modus.tools.base import ToolContext
from modus.tools import git_credentials as gc
from modus.tools import git_tools as gt


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True)
    return repo


@pytest.fixture
def ctx(git_repo):
    return ToolContext(cwd=str(git_repo), config=None)


def _run(coro):
    return asyncio.run(coro)


def test_branch_list_and_create(ctx):
    out = _run(gt.git_branch_list({}, ctx))
    assert not out.is_error

    created = _run(gt.git_branch_create({"branch": "feature"}, ctx))
    assert not created.is_error
    assert "feature" in created.content


def test_remote_add_list_and_reject_local(ctx):
    added = _run(gt.git_remote_add({"name": "origin", "url": "https://github.com/u/r.git"}, ctx))
    assert not added.is_error

    listed = _run(gt.git_remote_list({}, ctx))
    assert "origin" in listed.content

    # file:// and local paths are rejected outright.
    bad = _run(gt.git_remote_add({"name": "bad", "url": "file:///etc/passwd"}, ctx))
    assert bad.is_error
    bad2 = _run(gt.git_remote_add({"name": "bad2", "url": "/some/local/path"}, ctx))
    assert bad2.is_error


def test_fetch_without_remote_is_noop(ctx):
    # With no remote, `git fetch` exits 0 and reports nothing to fetch.
    out = _run(gt.git_fetch({}, ctx))
    assert out.is_error is False


def test_credential_roundtrip(monkeypatch, tmp_path):
    # Use a temp MODUS home so the JSON backend writes to a temp file.
    monkeypatch.setenv("MODUS_HOME", str(tmp_path))
    gc.set_git_credential("origin", "alice", "token1234")
    assert gc.has_git_credential("origin") is True
    assert gc.get_git_credential("origin") == "alice:token1234"
    # Only the last 4 chars are exposed as a hint.
    assert gc.git_credential_hint("origin") == "…1234"
    gc.clear_git_credential("origin")
    assert gc.has_git_credential("origin") is False
    assert gc.git_credential_hint("origin") == ""


def test_credential_clear_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("MODUS_HOME", str(tmp_path))
    gc.clear_git_credential("nonexistent")
    # No exception, no crash.


def test_remote_tools_registered_in_builtins():
    from modus.tools.builtins import _clone_tools

    tools = _clone_tools()
    names = {t.name for t in tools}
    for expected in (
        "git_clone", "git_remote_list", "git_remote_add", "git_remote_remove",
        "git_fetch", "git_pull", "git_push", "git_branch_list",
        "git_branch_create", "git_branch_checkout", "git_branch_merge",
        "git_credential_set", "git_credential_clear",
    ):
        assert expected in names, f"missing {expected}"
    # Mutating tools require approval; read-only listings do not.
    by_name = {t.name: t for t in tools}
    assert by_name["git_clone"].requires_approval is True
    assert by_name["git_push"].requires_approval is True
    assert by_name["git_remote_list"].requires_approval is False
