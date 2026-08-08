"""A2 拒绝回灌模型 + 规则记忆——agent 看到拒绝会改道。

Covers the deny/skip structured tool_result re-injection (is_error=False) and
the rule-memory persistence (rule_grants survive a store restart).
"""
from __future__ import annotations

import asyncio

import pytest

from modus.config import ModusConfig
from modus.policy.approval import SessionGrantStore
from modus.tools.base import ApprovalResponse, Tool, ToolContext, ToolResult, object_schema
from modus.tools.executor import ToolExecutor
from modus.tools.registry import ToolRegistry


def _command_tool() -> Tool:
    async def handler(_payload, _context):
        return ToolResult("should not run")

    return Tool(
        name="bash",
        description="shell",
        parameters=object_schema({"command": {"type": "string"}}, ["command"]),
        required_keys=["command"],
        handler=handler,
        is_read_only=False,
        danger_level="high",
        requires_approval=True,
    )


async def _run(tool: Tool, args: str, *, callback=None, store=None) -> ToolResult:
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry)
    ctx = ToolContext(cwd=".", config=ModusConfig(), approval_callback=callback, grant_store=store)
    call = {"id": "c", "function": {"name": tool.name, "arguments": args}}
    return (await executor.execute_all([call], ctx))[0]


@pytest.mark.asyncio
async def test_deny_returns_structured_non_error_tool_result():
    """deny 分支产出 is_error=False 的 ToolResult，content 含 reason。"""
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("bad")

    async def deny(_request):
        return ApprovalResponse.deny("rm -rf / is not allowed on this workspace")

    tool = Tool(
        "bash", "shell", object_schema({"command": {"type": "string"}}, ["command"]),
        handler, required_keys=["command"], is_read_only=False,
        danger_level="high", requires_approval=True,
    )
    result = await _run(tool, '{"command":"rm -rf /"}', callback=deny)

    assert executed is False
    assert result.is_error is False  # A2: not an error, it is information
    assert "NOT executed" in result.content
    assert "rm -rf / is not allowed" in result.content
    assert result.metadata.get("operation") == "approval-denied"
    assert result.metadata.get("approved") is False


@pytest.mark.asyncio
async def test_model_sees_denial_in_tool_result_payload():
    """deny 的 tool_result 会被 agent 记录为 tool 消息（下一轮 LLM 可见）。"""
    from modus.agent.strategies.react import ReActReasoner
    from modus.agent import QueryEngine
    from modus.tools.base import ApprovalResponse

    class OneToolClient:
        model_name = "test"
        provider_name = "test"

        async def chat(self, messages, tools, *, system_prompt):
            if not any(message.role == "tool" for message in messages):
                yield {
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": 0, "id": "danger-1",
                        "function": {"name": "bash", "arguments": '{"command":"rm -rf /"}'},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "changed course"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    async def handler(_payload, _context):
        return ToolResult("should not run")

    async def deny(_request):
        return ApprovalResponse.deny("blocked: rm -rf / is blacklisted")

    registry = ToolRegistry()
    registry.register(Tool(
        name="bash", description="shell", handler=handler,
        parameters=object_schema({"command": {"type": "string"}}, ["command"]),
        required_keys=["command"], is_read_only=False, danger_level="high", requires_approval=True,
    ))
    engine = QueryEngine(llm_client=OneToolClient(), tool_registry=registry, config=ModusConfig(), cwd=".")
    result = await engine.ask_complete_async("clean up", approval_callback=deny)
    # The agent saw the denial (non-error) and changed course, and the run
    # terminated normally.
    assert result.text == "changed course"


@pytest.mark.asyncio
async def test_skip_remains_non_error_but_explains_why():
    """skip 保持 fail-closed 非错误，且现在带 reason 回灌。"""
    async def handler(_payload, _context):
        return ToolResult("should not run")

    async def skip(_request):
        return ApprovalResponse.skip("deferring to manual step")

    tool = _command_tool()
    result = await _run(tool, '{"command":"git push"}', callback=skip)

    assert result.is_error is False
    assert "NOT executed" in result.content
    assert "deferring to manual step" in result.content
    assert result.metadata.get("operation") == "skipped"


@pytest.mark.asyncio
async def test_infrastructure_denial_stays_an_error():
    """政策/基础设施拒绝仍是错误（非 human deny）。"""
    # No callback at all → policy/transport denial → genuine error.
    tool = _command_tool()
    result = await _run(tool, '{"command":"rm -rf /"}', callback=None)
    assert result.is_error is True
    assert "approval" in result.content.lower()


@pytest.mark.asyncio
async def test_desktop_deny_reason_plumbed_to_waiting_waiter():
    """桌面结构化 deny（含 reason）会完整传给等待中的审批回调。"""
    from modus.desktop.events import RunEventEmitter
    from modus.desktop.server import DaoSession, resolve_pending_approval, wait_for_user_approval
    from modus.tools.base import ApprovalResponse

    class FakeWebSocket:
        async def send_json(self, _packet):
            pass

    websocket = FakeWebSocket()
    session = DaoSession(id="session", db_id="db")
    emitter = RunEventEmitter(run_id="run_a2", mode="default", send_json=websocket.send_json)

    waiting = asyncio.create_task(wait_for_user_approval(
        websocket, session, emitter,
        {"tool_name": "bash", "input": {"command": "rm -rf /"}, "danger_level": "high"},
        timeout=5,
    ))
    for _ in range(10):
        if session.pending_approvals:
            break
        await asyncio.sleep(0)
    approval_id = next(iter(session.pending_approvals))[1]

    assert resolve_pending_approval(
        session, emitter.run_id, approval_id,
        {"decision": "deny", "reason": "blacklisted command"},
    ) is True

    result = await waiting
    assert isinstance(result, ApprovalResponse)
    assert result.decision == "deny"
    assert result.reason == "blacklisted command"


@pytest.mark.asyncio
async def test_desktop_remember_pattern_plumbed_to_waiting_waiter():
    """桌面"记住规则"（remember_pattern）传给等待中的审批回调 → A2 规则记忆。"""
    from modus.desktop.events import RunEventEmitter
    from modus.desktop.server import DaoSession, resolve_pending_approval, wait_for_user_approval
    from modus.tools.base import ApprovalResponse

    class FakeWebSocket:
        async def send_json(self, _packet):
            pass

    websocket = FakeWebSocket()
    session = DaoSession(id="session", db_id="db")
    emitter = RunEventEmitter(run_id="run_rule", mode="default", send_json=websocket.send_json)

    waiting = asyncio.create_task(wait_for_user_approval(
        websocket, session, emitter,
        {"tool_name": "bash", "input": {"command": "cat a"}, "danger_level": "high"},
        timeout=5,
    ))
    for _ in range(10):
        if session.pending_approvals:
            break
        await asyncio.sleep(0)
    approval_id = next(iter(session.pending_approvals))[1]

    assert resolve_pending_approval(
        session, emitter.run_id, approval_id,
        {"decision": "approve", "remember_pattern": "cat *"},
    ) is True

    result = await waiting
    assert isinstance(result, ApprovalResponse)
    assert result.decision == "approve"
    assert result.remember is True
    assert result.remember_pattern == "cat *"


# ── rule memory (A2) ──


def test_rule_grants_persist(tmp_path):
    """rule:always → 重启后仍生效（写 JSONL 再加载）。"""
    store = SessionGrantStore(persist_path=str(tmp_path / "rules.jsonl"))
    store.add_rule("web_fetch", "example.com", "allow")
    store.add_rule("bash", "rm -rf *", "deny")

    fresh = SessionGrantStore(persist_path=str(tmp_path / "rules.jsonl"))
    fresh.load_rules()

    assert fresh.rule_for("web_fetch", "example.com") == "allow"
    assert fresh.rule_for("bash", "rm -rf /") == "deny"
    assert fresh.rule_for("bash", "cat a") is None


def test_rule_pattern_match_auto_allows():
    """命令模式匹配 → 自动放行（不打断）。"""
    store = SessionGrantStore()
    store.add_rule("bash", "cat *", "allow")
    assert store.rule_for("bash", "cat a") == "allow"
    assert store.rule_for("bash", "cat b") == "allow"
    assert store.rule_for("bash", "rm -rf /") is None


def test_rule_deny_autoblocks_matching_command():
    store = SessionGrantStore()
    store.add_rule("bash", "rm -rf *", "deny")
    assert store.rule_for("bash", "rm -rf /") == "deny"


@pytest.mark.asyncio
async def test_rule_memory_skips_callback_entirely():
    """命中规则 → 不再调用 callback，直接放行。"""
    calls = {"n": 0}

    async def handler(_payload, _context):
        return ToolResult("ran")

    async def callback(_request):
        calls["n"] += 1
        return "approve"

    store = SessionGrantStore()
    store.add_rule("bash", "cat *", "allow")

    tool = Tool(
        "bash", "shell", object_schema({"command": {"type": "string"}}, ["command"]),
        handler, required_keys=["command"], is_read_only=False,
        danger_level="high", requires_approval=True,
    )
    # The rule auto-allows `cat a` and `cat b` without ever asking the human.
    first = await _run(tool, '{"command":"cat a"}', callback=callback, store=store)
    second = await _run(tool, '{"command":"cat b"}', callback=callback, store=store)

    assert first.content == "ran"
    assert second.content == "ran"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_rule_deny_never_executes_handler():
    """命中 deny 规则 → 工具永不执行。"""
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("bad")

    async def callback(_request):
        return "approve"  # the human would approve, but the rule says no

    store = SessionGrantStore()
    store.add_rule("bash", "rm -rf *", "deny")

    tool = Tool(
        "bash", "shell", object_schema({"command": {"type": "string"}}, ["command"]),
        handler, required_keys=["command"], is_read_only=False,
        danger_level="high", requires_approval=True,
    )
    result = await _run(tool, '{"command":"rm -rf /"}', callback=callback, store=store)

    assert executed is False
    assert result.is_error is True  # policy denial
    assert "denied" in result.content.lower()
