"""Wave5 E2: background review fork + provenance gate + skill lifecycle.

Covers the deterministic trigger (N-turn interval + action validation axiom),
the provenance gate (fork writes only into curator territory), the fork's
provenance-gated registry (no workspace writer), the ``deposit_review`` tool,
and the auto-authority memory disclosure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from modus.agent import turn_finalizer
from modus.agent.turn_finalizer import (
    build_deposit_tool,
    build_fork_registry,
    maybe_spawn_background_review,
    provenance_gate,
    review_trigger,
    spawn_background_review,
    turn_executed_tool,
)
from modus.config import ModusConfig
from modus.desktop import db as desktop_db
from modus.runtime.budget import RunBudget, RunLimits
from modus.skills import SkillRepository
from modus.tools.base import ToolContext


class FakeReviewClient:
    """Minimal LLM client: completes immediately with no tool calls."""

    def __init__(self) -> None:
        self.model_name = "fake"
        self.provider_name = "test"
        self.max_context_window = 128_000

    async def chat(self, messages, tools, *, system_prompt):
        yield {"type": "text_delta", "text": "distilled, nothing durable"}
        yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _budget_with(turns: int, *, tool_successes: int = 0) -> RunBudget:
    budget = RunBudget(RunLimits(max_turns=max(30, turns)))
    for turn in range(1, turns + 1):
        budget.begin_turn()
        budget.record_turn(
            turn=turn, text_chars=10, tool_calls=1 if tool_successes else 0,
            tool_successes=tool_successes, tool_errors=0 if tool_successes else 1,
            tokens=5, stop_reason="end_turn",
        )
    return budget


# ── deterministic trigger (N-turn interval + action validation) ──


def test_review_trigger_requires_n_turns_and_action():
    # Below the interval: no review even with successful tool execution.
    budget = _budget_with(5, tool_successes=2)
    assert review_trigger(budget, interval=10) is False
    # Above the interval AND a successful tool call: review spawns.
    budget = _budget_with(12, tool_successes=3)
    assert review_trigger(budget, interval=10) is True


def test_learning_gated_on_tool_execution():
    """无工具调用的闲聊不触发学习（GenericAgent 行动验证公理）。"""
    # 12 turns of pure talk: no successful tool execution → no learning.
    budget = _budget_with(12, tool_successes=0)
    assert review_trigger(budget, interval=10) is False


def test_turn_executed_tool_detects_successful_execution():
    budget = _budget_with(3, tool_successes=1)
    assert turn_executed_tool(budget) is True
    assert turn_executed_tool(_budget_with(3, tool_successes=0)) is False
    # Malformed budget never raises.
    assert turn_executed_tool(None) is False


def test_review_trigger_respects_last_review_turn_spacing():
    budget = _budget_with(22, tool_successes=1)
    # 22 - 12 = 10 → triggers again after a spaced review.
    assert review_trigger(budget, interval=10, last_review_turn=12) is True
    # 22 - 15 = 7 < 10 → does not re-review too soon.
    assert review_trigger(budget, interval=10, last_review_turn=15) is False


# ── provenance gate ──


def test_provenance_gate_accepts_curator_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("MODUS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(turn_finalizer, "_SKILLS_ROOT", tmp_path / "skills")
    roots = turn_finalizer.curator_roots()
    assert all(provenance_gate(root) for root in roots)
    assert provenance_gate(tmp_path / "skills" / "review-code.json")
    assert provenance_gate(tmp_path / "memories" / "session-x")


def test_provenance_gate_blocks_workspace_write(tmp_path, monkeypatch):
    monkeypatch.setenv("MODUS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(turn_finalizer, "_SKILLS_ROOT", tmp_path / "skills")
    # A fork must never be allowed to write into the user's workspace.
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    assert provenance_gate(workspace / "src" / "app.py") is False
    assert provenance_gate(tmp_path / "something-else") is False
    assert provenance_gate("/") is False


# ── provenance-gated fork registry ──


def test_fork_registry_excludes_workspace_writers():
    registry = build_fork_registry()
    names = set(registry.list_names())
    # The fork's ONLY mutation surface is deposit_review.
    assert "deposit_review" in names
    for writer in ("write_file", "edit_file", "bash", "office_exec", "spawn_process", "run_tests"):
        assert writer not in names, f"fork must not expose {writer}"
    # Read-only inspection tools remain available.
    assert "read_file" in names
    assert "search_memory" in names


# ── deposit_review ──


async def _deposit(payload, *, session_id=None, repo=None):
    tool = build_deposit_tool(skill_repository=repo)
    ctx = ToolContext(cwd="/tmp", config=ModusConfig(), session_id=session_id)
    return await tool.execute(payload, ctx)


@pytest.mark.asyncio
async def test_deposit_review_writes_skill_to_curator(tmp_path):
    repo = SkillRepository(tmp_path / "skills")
    result = await _deposit(
        {"type": "skill", "content": "聚合 Excel 时用 read_only + iter_rows", "name": "excel-aggregate",
         "description": "Efficient Excel aggregation"},
        repo=repo,
    )
    assert result.is_error is False
    skill = repo.get("excel-aggregate")
    assert skill is not None
    assert skill.status == "active"
    assert "read_only" in skill.prompt
    assert (tmp_path / "skills" / "excel-aggregate.usage.json").exists()


@pytest.mark.asyncio
async def test_deposit_review_memory_is_auto_authority_and_disclosed(tmp_path):
    sid = desktop_db.create_session("fork-deposit")["id"]
    result = await _deposit(
        {"type": "memory", "content": "项目约束：office 大文件分析用 worker", "category": "constraint",
         "source_run": "run-abc", "provenance": "self_report"},
        session_id=sid,
    )
    assert result.is_error is False
    memories = desktop_db.list_memories(sid, scope="session")
    assert memories
    from modus.desktop.memory import authority_of_memory, get_memories_text

    assert authority_of_memory(memories[0]) == "auto"
    # Injection carries the unverified disclosure.
    text = get_memories_text(sid)
    assert "auto-extracted 未经验证" in text
    assert "run:run-abc" in (memories[0].get("source_ids") or [])


@pytest.mark.asyncio
async def test_deposit_review_rejects_bad_type_and_missing_content():
    bad_type = await _deposit({"type": "bash", "content": "rm -rf /"})
    assert bad_type.is_error is True
    no_content = await _deposit({"type": "memory", "content": "  "})
    assert no_content.is_error is True


@pytest.mark.asyncio
async def test_deposit_review_skill_name_is_strict(tmp_path):
    repo = SkillRepository(tmp_path / "skills")
    result = await _deposit(
        {"type": "skill", "content": "some procedure", "name": "Bad Name!"},
        repo=repo,
    )
    assert result.is_error is False  # name slugified, not rejected
    # The unsafe name cannot have been written verbatim.
    assert not (tmp_path / "skills" / "Bad Name!.json").exists()


# ── background spawn ──


@pytest.mark.asyncio
async def test_spawn_background_review_returns_task(tmp_path):
    task = spawn_background_review(
        session_id="sid", run_id="rid", cwd="/tmp",
        llm_client=FakeReviewClient(), config=ModusConfig(),
        run_summary="summary", self_report="report",
        skill_repository=SkillRepository(tmp_path / "skills"),
    )
    assert task is not None
    await asyncio.wait_for(task, timeout=10)
    assert task.done()


@pytest.mark.asyncio
async def test_maybe_spawn_background_review_gates_and_tracks(tmp_path):
    budget = _budget_with(12, tool_successes=2)
    cfg = ModusConfig()
    task = maybe_spawn_background_review(
        session_id="sid", run_id="rid", cwd="/tmp",
        llm_client=FakeReviewClient(), config=cfg, budget=budget,
        skill_repository=SkillRepository(tmp_path / "skills"),
    )
    assert task is not None
    await asyncio.wait_for(task, timeout=10)
    # The last-review turn is now recorded so a quick second run does not re-review.
    assert turn_finalizer._LAST_REVIEW_TURN.get("sid") == 12


@pytest.mark.asyncio
async def test_maybe_spawn_background_review_requires_session_and_action(tmp_path):
    cfg = ModusConfig()
    # No session → no fork.
    assert maybe_spawn_background_review(
        session_id=None, run_id="rid", cwd="/tmp",
        llm_client=FakeReviewClient(), config=cfg,
        budget=_budget_with(12, tool_successes=2),
    ) is None
    # Session but no successful tool execution → no fork (action axiom).
    assert maybe_spawn_background_review(
        session_id="sid", run_id="rid", cwd="/tmp",
        llm_client=FakeReviewClient(), config=cfg,
        budget=_budget_with(12, tool_successes=0),
    ) is None


class _DepositingReviewClient:
    """Fork LLM: asks for one memory deposit, then finishes."""

    def __init__(self) -> None:
        self.model_name = "fake"
        self.provider_name = "test"
        self.max_context_window = 128_000
        self._used_tool = False

    async def chat(self, messages, tools, *, system_prompt):
        if not self._used_tool and not any(m.role == "tool" for m in messages):
            self._used_tool = True
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0, "id": "t1",
                    "function": {"name": "deposit_review", "arguments": (
                        '{"type":"memory","content":"fork 沉淀：用 read_only 聚合 Excel",'
                        '"category":"fact","source_run":"run-fork"}'
                    )},
                },
            }
            yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


@pytest.mark.asyncio
async def test_background_fork_deposits_auto_memory_end_to_end(tmp_path):
    """Fork runs through the ReAct loop, auto-approves the gated deposit, and
    the deposited memory carries authority=auto."""
    from modus.desktop.memory import authority_of_memory

    sid = desktop_db.create_session("fork-e2e")["id"]
    task = spawn_background_review(
        session_id=sid, run_id="run-fork", cwd="/tmp",
        llm_client=_DepositingReviewClient(), config=ModusConfig(),
        run_summary="s", self_report="r",
        skill_repository=SkillRepository(tmp_path / "skills"),
    )
    assert task is not None
    await asyncio.wait_for(task, timeout=15)

    memories = desktop_db.list_memories(sid, scope="session")
    assert any("read_only" in (m.get("content") or "") for m in memories)
    deposited = next(m for m in memories if "read_only" in (m.get("content") or ""))
    assert authority_of_memory(deposited) == "auto"
    assert "run:run-fork" in (deposited.get("source_ids") or [])


# ── memory-layer authority retrieval (E2) ──


def test_authority_retrieval_ranks_confirmed_above_auto():
    from modus.desktop import memory

    sid = desktop_db.create_session("auth-rank")["id"]
    memory.add_memory(sid, "用户偏好 Python 精简代码", "preference", authority="auto")
    memory.add_memory(sid, "用户偏好 Python 清晰风格", "preference", authority="confirmed")

    hits = memory.search_memories(sid, "Python 偏好")
    assert hits
    # Equal keyword overlap → the confirmed memory outranks the auto one.
    assert authority_of_memory_here(hits[0]) == "confirmed"


def authority_of_memory_here(mem):
    from modus.desktop.memory import authority_of_memory

    return authority_of_memory(mem)
