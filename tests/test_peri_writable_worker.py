"""Writable Peri worker end-to-end: worktree create → write/commit → merge → cleanup.

These tests exercise the composition that the individual unit tests never join:
the registry stays writable inside ``execute_subtask``, writes land in the
worker's private worktree, revisions stay inside the worktree, and the merge
happens behind the ``merge_changes`` approval gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from modus.desktop.peri import _safe_subagent_registry, build_subagent_tool_registry, execute_subtask
from modus.desktop.worktree_orchestrator import cleanup_worktree, merge_worktree, prepare_worktrees
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.builtins import get_builtin_tools
from modus.tools.registry import ToolRegistry


@pytest.fixture
def guard_home(tmp_path, monkeypatch):
    """Point Path.home() at a temp dir so the home-anchored guard allows writes."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True,
    ).stdout


def init_repository(path):
    path.mkdir()
    git(path, "init")
    git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    git(path, "config", "user.name", "Modus Test")
    git(path, "config", "user.email", "modus@example.invalid")
    (path / "tracked.txt").write_text("base\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-m", "base")


def find_worker_worktree(private, ordinal=1):
    return next(p for p in private.rglob(f"*worker-{ordinal}") if p.is_dir())


async def _allow(_request):
    return "allow"


async def _deny(_request):
    return "deny"


class WritableToolClient:
    """Write a file, stage it, commit it, then answer — all inside the worktree."""

    def __init__(self, real_write: bool):
        self.real_write = real_write

    async def chat(self, messages, tools, *, system_prompt):
        if not any(message.role == "tool" for message in messages):
            all_names = {tool["function"]["name"] for tool in tools}
            assert "write_file" in all_names
            assert "git_add" in all_names
            assert "git_commit" in all_names
            calls = [
                {
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": i, "id": f"call-{i}",
                        "function": {"name": name, "arguments": args},
                    },
                }
                for i, (name, args) in enumerate([
                    ("write_file", '{"path":"feature.txt","content":"writable worker\\n"}'),
                    ("git_add", '{"path":"."}'),
                    ("git_commit", '{"message":"feature"}'),
                ])
            ]
            for call in calls:
                yield call
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "Committed feature in the worktree."}
        yield {"type": "message_end", "stop_reason": "end_turn"}


@pytest.mark.asyncio
async def test_writable_registry_survives_safe_subagent_refilter():
    """The writable intent is not stripped by the executor's safety refilter."""
    registry = build_subagent_tool_registry(get_builtin_tools(), writable=True)
    names = set(_safe_subagent_registry(registry, writable=True).list_names())
    assert {"write_file", "edit_file", "git_add", "git_commit"} <= names
    assert "bash" not in names


@pytest.mark.asyncio
async def test_writable_registry_without_flag_keeps_read_only_default(monkeypatch):
    """Without writable, the safety refilter still strips mutators."""
    registry = build_subagent_tool_registry(get_builtin_tools(), writable=True)
    names = set(_safe_subagent_registry(registry, writable=False).list_names())
    assert not {"write_file", "edit_file", "git_add", "git_commit"} & names


@pytest.mark.asyncio
async def test_execute_subtask_writable_writes_into_worktree_and_commits(monkeypatch, guard_home):
    """A writable worker's write/commit tools resolve inside its worktree cwd."""
    from modus.desktop import peri

    monkeypatch.setattr(peri, "create_llm_client", lambda _cfg: WritableToolClient(real_write=True))
    monkeypatch.setattr(peri, "load_config", lambda: _ConfigStub())

    workdir = guard_home / "wt"
    workdir.mkdir()
    output = await execute_subtask(
        {"description": "add a feature", "context": "local", "success_criteria": "committed"},
        {"provider": "test", "model": "sub", "api_key": "key"},
        "add the feature",
        tool_registry=build_subagent_tool_registry(get_builtin_tools(), writable=True),
        cwd=str(workdir), writable=True,
    )
    assert output == "Committed feature in the worktree."
    assert (workdir / "feature.txt").read_text() == "writable worker\n"


class _ConfigStub:
    class _Policy:
        hitl_mode = "auto"
        path_guard_enabled = True
        command_blacklist = []
        audit_log_path = ""

    policy = _Policy()
    tools = type("_Tools", (), {"timeout": 60.0, "batch_timeout": 90.0})()


@pytest.mark.asyncio
async def test_writable_worker_end_to_end_create_write_commit_merge_cleanup(tmp_path):
    """Full writable run: create → write/commit → merge → cleanup leaves main updated."""
    repo = tmp_path / "repo"
    init_repository(repo)
    private = tmp_path / "private"

    prepared = await prepare_worktrees(
        cwd=str(repo), worker_count=1, plan_id="e2e", data_root=str(private),
        approval=_allow,
    )
    assert prepared["ok"] is True
    wt = find_worker_worktree(private)
    assert str(wt) == prepared["created"][0]["path"]

    # Worker writes + commits inside the private worktree (real git boundary).
    (wt / "feature.txt").write_text("writable worker\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "feature")

    merged = await merge_worktree(
        cwd=str(repo), plan_id="e2e", data_root=str(private), ordinal=1,
        approval=_allow,
    )
    assert merged["ok"] is True
    assert "feature.txt" in git(repo, "ls-tree", "-r", "--name-only", "main")
    assert "writable worker" in (repo / "feature.txt").read_text()

    cleaned = await cleanup_worktree(
        cwd=str(repo), plan_id="e2e", data_root=str(private), ordinal=1,
    )
    assert cleaned["ok"] is True
    assert not list(private.rglob("*worker-1"))


@pytest.mark.asyncio
async def test_merge_denied_leaves_main_untouched(tmp_path):
    """Denying the merge gate must not merge worker changes into main."""
    repo = tmp_path / "repo"
    init_repository(repo)
    private = tmp_path / "private"

    await prepare_worktrees(
        cwd=str(repo), worker_count=1, plan_id="deny", data_root=str(private),
        approval=_allow,
    )
    wt = find_worker_worktree(private)
    (wt / "feature.txt").write_text("writable worker\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "feature")

    merged = await merge_worktree(
        cwd=str(repo), plan_id="deny", data_root=str(private), ordinal=1,
        approval=_deny,
    )
    assert merged["ok"] is False
    assert "feature.txt" not in git(repo, "ls-tree", "-r", "--name-only", "main")


@pytest.mark.asyncio
async def test_revision_cwd_is_worktree_not_base(monkeypatch, tmp_path):
    """A writable revision must resolve inside the worktree, never the base."""
    from modus.desktop import peri

    captured: dict[str, str] = {}

    class RevisionClient:
        async def chat(self, messages, tools, *, system_prompt):
            captured["cwd"] = _extract_cwd(system_prompt)
            if not any(message.role == "tool" for message in messages):
                yield {
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": 0, "id": "r1", "function": {
                            "name": "write_file", "arguments": '{"path":"rev.txt","content":"revised\\n"}',
                        },
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "revised output"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    def _extract_cwd(system_prompt: str) -> str:
        for line in (system_prompt or "").splitlines():
            if line.startswith("WORKING DIRECTORY:"):
                return line.split("WORKING DIRECTORY:", 1)[1].strip()
        return ""

    workdir = tmp_path / "wt"
    workdir.mkdir()
    monkeypatch.setattr(peri, "create_llm_client", lambda _cfg: RevisionClient())
    monkeypatch.setattr(peri, "load_config", lambda: _ConfigStub())

    await execute_subtask(
        {"description": "revise", "context": "local", "success_criteria": "done"},
        {"provider": "test", "model": "sub", "api_key": "key"},
        "revise the feature",
        tool_registry=build_subagent_tool_registry(get_builtin_tools(), writable=True),
        cwd=str(workdir), writable=True,
    )
    assert captured["cwd"] == str(workdir)
