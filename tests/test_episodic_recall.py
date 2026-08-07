"""Episodic recall: prior-run conclusions injected into model context.

The agent can now learn from its own past runs: ``episodic_recall_text``
scores terminal prior runs against the current user message and
``SessionContextProvider.effective_history`` injects the relevant block.
"""

from __future__ import annotations

import pytest

from modus.agent.context import SessionContextProvider
from modus.desktop import db, memory
from modus.types import Message


def _seed_run(sid: str, run_id: str, *, markdown: str, state: str = "completed") -> None:
    db.create_run(run_id, sid, "default")
    db.upsert_run_event(sid, {
        "event_id": f"evt-{run_id}", "run_id": run_id, "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1, "timestamp": "now",
        "mode": "default", "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "host_response", "status": "completed",
        "payload": {"markdown": markdown}, "revision": 0, "part_id": "p1",
    })
    db.update_run(run_id, state=state, stop_reason="completed")


def test_episodic_recall_scores_terminal_runs_by_keyword():
    sid = db.create_session("epi-recall")["id"]
    _seed_run(sid, "run-a", markdown="结论：缓存层使用 Redis 已就绪")
    _seed_run(sid, "run-b", markdown="结论：数据库迁移完成")

    text = memory.episodic_recall_text(sid, "缓存层怎么做")

    assert text
    assert "PAST RUN RECALL" in text
    assert "Redis" in text
    assert "数据库迁移" not in text


def test_episodic_recall_ignores_current_run_and_nonterminal_runs():
    sid = db.create_session("epi-recall-2")["id"]
    _seed_run(sid, "run-current", markdown="结论：当前正在进行", state="running")
    _seed_run(sid, "run-done", markdown="结论：已完成的历史工作")

    text = memory.episodic_recall_text(sid, "结论 历史", current_run_id="run-current")

    assert text
    assert "run-done" in text
    assert "run-current" not in text


def test_episodic_recall_returns_empty_when_irrelevant():
    sid = db.create_session("epi-recall-3")["id"]
    _seed_run(sid, "run-x", markdown="结论：某个无关话题")

    assert memory.episodic_recall_text(sid, "完全不相关查询词") == ""


def test_episodic_recall_is_bounded():
    sid = db.create_session("epi-recall-4")["id"]
    for index in range(5):
        _seed_run(sid, f"run-{index}", markdown=f"结论：关于缓存层主题的非常长的内容 {index} " * 20)

    text = memory.episodic_recall_text(sid, "缓存层 主题", max_chars=800)

    assert text
    assert len(text) <= 800 + 200  # small slack for the header


def test_effective_history_injects_episodic_recall_when_relevant():
    sid = db.create_session("epi-recall-5")["id"]
    _seed_run(sid, "run-old", markdown="结论：审批流程已实现")

    session = type("S", (), {"db_id": sid, "main_history": []})()
    provider = SessionContextProvider()

    history = provider.effective_history(session, episodic_query="审批流程", current_run_id="current")

    system_msgs = [m.content for m in history if m.role == "system"]
    assert any("PAST RUN RECALL" in c for c in system_msgs)


def test_effective_history_omits_recall_when_no_query():
    sid = db.create_session("epi-recall-6")["id"]
    _seed_run(sid, "run-old", markdown="结论：审批流程已实现")

    session = type("S", (), {"db_id": sid, "main_history": []})()
    provider = SessionContextProvider()

    history = provider.effective_history(session)

    assert not any(
        "PAST RUN RECALL" in str(m.content) for m in history if m.role == "system"
    )


def test_effective_history_respects_retrieval_enabled_flag():
    from modus.config import MemoryConfig, ModusConfig

    sid = db.create_session("epi-recall-7")["id"]
    _seed_run(sid, "run-old", markdown="结论：审批流程已实现")

    cfg = ModusConfig()
    cfg.memory = MemoryConfig(retrieval_enabled=False)
    engine = type("E", (), {"config": cfg})()
    session = type("S", (), {"db_id": sid, "main_history": [], "engine": engine})()
    provider = SessionContextProvider()

    history = provider.effective_history(session, episodic_query="审批流程", current_run_id="c")

    assert not any(
        "PAST RUN RECALL" in str(m.content) for m in history if m.role == "system"
    )



def test_query_scoped_memory_injection_scores_against_request():
    sid = db.create_session("epi-recall-8")["id"]
    memory.add_memory(sid, "项目使用 pytest 与分层记忆", "fact")
    memory.add_memory(sid, "用户偏好 TypeScript 写前端", "preference")
    # Project scope is now reachable through query-scored injection.
    memory.add_memory(sid, "跨会话项目约束：不提交 node_modules", "constraint", scope="project")

    text = memory.get_memory_context(sid, query="pytest 测试")
    assert "pytest" in text
    assert "TypeScript" not in text
    assert "SESSION MEMORY" in text


def test_flat_dump_without_query_is_unchanged():
    sid = db.create_session("epi-recall-9")["id"]
    memory.add_memory(sid, "用户偏好 Python", "preference")

    text = memory.get_memory_context(sid)
    assert "Python" in text
