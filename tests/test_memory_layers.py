"""Layered memory: consolidation, retrieval, working readback, project scope.

Covers the auto-memorize consolidation entry point, keyword+recency semantic
retrieval, episodic run-history search, working-memory readback into context,
and project-scope writes.
"""

from __future__ import annotations

import json

import pytest

from modus.desktop import db, memory


def _session() -> str:
    return db.create_session("mem-layers")["id"]


@pytest.mark.asyncio
async def test_consolidate_run_memories_writes_semantic_memories(monkeypatch):
    sid = _session()

    async def fake_call(provider, model, messages, system_prompt, **kw):
        return json.dumps({"memories": [
            {"category": "fact", "content": "项目使用 FastAPI"},
            {"category": "preference", "content": "用户偏好简洁代码"},
        ]})

    monkeypatch.setattr("modus.desktop.peri._call_llm", fake_call)
    written = await memory.consolidate_run_memories(
        session_id=sid, user_message="u", assistant_text="a",
        provider="test", model="m", api_key="k",
    )
    assert written == 2
    contents = [m["content"] for m in memory.get_memories(sid)]
    assert "项目使用 FastAPI" in contents
    assert "用户偏好简洁代码" in contents


@pytest.mark.asyncio
async def test_consolidate_best_effort_on_bad_llm_output(monkeypatch):
    sid = _session()

    async def bad_call(provider, model, messages, system_prompt, **kw):
        raise RuntimeError("model down")

    monkeypatch.setattr("modus.desktop.peri._call_llm", bad_call)
    # Never raises.
    assert await memory.consolidate_run_memories(
        session_id=sid, user_message="u", assistant_text="a",
        provider="test", model="m", api_key="k",
    ) == 0
    assert memory.get_memories(sid) == []


def test_search_memories_ranks_by_keyword_and_recency():
    sid = _session()
    memory.add_memory(sid, "用户偏好 Python", "preference")
    memory.add_memory(sid, "项目使用 JavaScript 写前端", "fact")

    hits = memory.search_memories(sid, "Python 偏好")
    assert hits and "Python" in hits[0]["content"]

    # Unrelated query finds nothing.
    assert memory.search_memories(sid, "区块链挖矿") == []


def test_project_scope_memory_writes_and_retrieves():
    sid = _session()
    memory.add_memory(sid, "跨会话项目约束：不提交 node_modules", "constraint", scope="project")

    project_rows = [m for m in db.list_memories(sid, scope="project")]
    assert project_rows and "node_modules" in project_rows[0]["content"]
    # Session scope is separate.
    assert db.list_memories(sid, scope="session") == []


def test_working_memory_readback_into_context():
    sid = _session()
    # Write run-scope working memory the way the orchestration ledger does.
    run = db.create_run("run-wm", sid, "default")
    db.add_memory_record(
        session_id=sid, scope="run", run_id=run["run_id"],
        content="Peri 分析结论：后端用 FastAPI", category="analysis",
    )
    ctx = memory.get_memory_context(sid, include_working=True)
    assert "Peri 分析结论" in ctx
    # Without working memory the readback is absent.
    ctx_plain = memory.get_memory_context(sid, include_working=False)
    assert "Peri 分析结论" not in ctx_plain


def test_recent_working_memory_prefers_newest_runs():
    sid = _session()
    old_run = db.create_run("run-wm-old", sid, "default")
    new_run = db.create_run("run-wm-new", sid, "default")
    # An old, memory-dense run must not crowd out a newer run's memories.
    for index in range(4):
        db.add_memory_record(
            session_id=sid, scope="run", run_id=old_run["run_id"],
            content=f"OLD memory {index}", category="analysis",
        )
    db.add_memory_record(
        session_id=sid, scope="run", run_id=new_run["run_id"],
        content="NEW memory", category="analysis",
    )

    recent = memory._recent_working_memory(sid, limit=2)

    contents = [m["content"] for m in recent]
    # The newest run is visited first, so its memory claims the first slot.
    assert contents[0] == "NEW memory"


def test_search_run_history_finds_episodic_events():
    sid = _session()
    run = db.create_run("run-epi", sid, "default")
    db.upsert_run_event(sid, {
        "event_id": "evt-1", "run_id": run["run_id"], "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1, "timestamp": "now",
        "mode": "default", "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "host_response", "status": "completed",
        "payload": {"markdown": "结论：缓存层已就绪"}, "revision": 0, "part_id": "p1",
    })
    db.update_run(run["run_id"], state="completed", stop_reason="completed")

    hits = memory.search_run_history(sid, "缓存层")
    assert hits and hits[0]["run_id"] == run["run_id"]
    assert "缓存层" in hits[0]["transcript"]


def test_save_memory_tool_requires_session(monkeypatch):
    from modus.config import ModusConfig
    from modus.tools.base import ToolContext
    from modus.tools.builtins import save_memory

    cfg = ModusConfig()
    ctx = ToolContext(cwd="/tmp", config=cfg, session_id=None)

    async def run():
        return await save_memory({"content": "x"}, ctx)

    import asyncio
    result = asyncio.run(run())
    assert result.is_error is True


def test_save_memory_tool_persists_with_session(monkeypatch):
    from modus.config import ModusConfig
    from modus.tools.base import ToolContext
    from modus.tools.builtins import save_memory

    sid = _session()
    cfg = ModusConfig()
    ctx = ToolContext(cwd="/tmp", config=cfg, session_id=sid)

    async def run():
        return await save_memory({"content": "记住用 Python", "category": "preference"}, ctx)

    import asyncio
    result = asyncio.run(run())
    assert result.is_error is False
    contents = [m["content"] for m in memory.get_memories(sid)]
    assert "记住用 Python" in contents
