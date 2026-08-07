"""Fenced self-report block parsing: plan/steps/summary/insight extraction."""

from __future__ import annotations

from modus.agent.self_report import extract_self_report_blocks, summarize_turn_blocks


def test_extracts_plan_steps_and_summary():
    text = (
        "I'll fix the bug.\n"
        "```plan\n"
        "## 阶段 1\n"
        "- 读源码\n"
        "- 定位根因\n"
        "```\n"
        "Then I ran steps:\n"
        "```steps\n"
        "1. read_file app.py\n"
        "2. edit_file fixed\n"
        "```\n"
        "```summary success\n"
        "- 修复了 bug\n"
        "- 测试通过\n"
        "```\n"
    )
    blocks = extract_self_report_blocks(text)
    assert set(blocks) == {"plan", "steps", "summary"}
    assert "定位根因" in blocks["plan"]["body"]
    assert blocks["summary"]["status"] == "success"
    assert "测试通过" in blocks["summary"]["body"]


def test_insight_and_unknown_kinds():
    text = (
        "```insight\n"
        "这个抽象值得提取\n"
        "```\n"
        "```not_a_kind\n"
        "ignored\n"
        "```\n"
    )
    blocks = extract_self_report_blocks(text)
    assert set(blocks) == {"insight"}
    assert "值得提取" in blocks["insight"]["body"]


def test_choice_block_is_parsed_not_executed():
    text = (
        "有几个可能的解释：\n"
        "```choice\n"
        "A. 方案一\n"
        "B. 方案二\n"
        "```\n"
    )
    blocks = extract_self_report_blocks(text)
    assert "A. 方案一" in blocks["choice"]["body"]


def test_malformed_or_empty_returns_empty():
    assert extract_self_report_blocks("") == {}
    assert extract_self_report_blocks("no fences here") == {}
    # Unterminated fence yields nothing (DOTALL cannot match a close).
    assert extract_self_report_blocks("```summary\nnever closed") == {}


def test_summarize_turn_bounds_and_omits_absent():
    long = "x" * 1000
    text = f"```plan\n{long}\n```\n```summary error\n- 失败了\n```\n"
    summary = summarize_turn_blocks(text)
    assert "plan" in summary
    assert len(summary["plan"]) <= 800 + 1  # bounded + ellipsis
    assert summary["summary"]["status"] == "error"
    # No steps/insight blocks -> omitted keys.
    assert "steps" not in summary
    assert "insight" not in summary


def test_persist_self_report_writes_run_working_memory():
    import asyncio
    from modus.desktop import db, memory
    from modus.desktop.default_runner import _persist_self_report

    sid = db.create_session("self-report-persist")["id"]
    run = db.create_run("run-self-report", sid, "default")
    db.add_message(
        sid, "assistant",
        content=(
            "我先计划：\n```plan\n- 读源码\n- 修复\n```\n"
            "```summary success\n- 完成\n```\n"
        ),
    )

    asyncio.run(_persist_self_report(type("S", (), {"db_id": sid})(), run["run_id"]))

    working = [m for m in db.list_memories(sid, scope="run")]
    assert any("self-report" == m.get("category") for m in working)
    assert any("Agent 自述" in m.get("content", "") for m in working)
    assert any("plan" in m.get("content", "") for m in working)


def test_persist_self_report_noop_without_fences():
    import asyncio
    from modus.desktop import db
    from modus.desktop.default_runner import _persist_self_report

    sid = db.create_session("self-report-nofence")["id"]
    run = db.create_run("run-no-fence", sid, "default")
    db.add_message(sid, "assistant", content="plain answer, no fences")

    asyncio.run(_persist_self_report(type("S", (), {"db_id": sid})(), run["run_id"]))

    working = [m for m in db.list_memories(sid, scope="run")]
    assert not any("self-report" == m.get("category") for m in working)
