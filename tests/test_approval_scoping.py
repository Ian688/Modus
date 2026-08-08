"""A1 作用域化审批缓存——批准 `cat a` 不放行 `rm -rf`。

Covers the scoped approval decision (SessionGrantStore + ApprovalPolicy
scoped_decision + executor wiring) and the noSessionCache rule for
high-risk tools.
"""
from __future__ import annotations

import pytest

from modus.config import ModusConfig
from modus.policy.approval import ApprovalDecision, ApprovalPolicy, SessionGrantStore
from modus.tools.base import ApprovalResponse, Tool, ToolContext, ToolResult, object_schema
from modus.tools.executor import ToolExecutor, _default_resource_key
from modus.tools.registry import ToolRegistry


def _command_tool(name: str = "bash", *, read_only: bool = False, danger: str = "high") -> Tool:
    async def handler(_payload, _context):  # pragma: no cover - policy is pure
        raise AssertionError("policy must not execute a tool")

    return Tool(
        name=name,
        description="shell",
        parameters=object_schema({"command": {"type": "string"}}, ["command"]),
        required_keys=["command"],
        handler=handler,
        is_read_only=read_only,
        danger_level=danger,
        requires_approval=not read_only,
    )


def _running_command_tool(handler) -> Tool:
    """A command tool wired to a real handler (executor tests)."""
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


def _fetch_tool() -> Tool:
    async def handler(_payload, _context):  # pragma: no cover
        raise AssertionError("policy must not execute a tool")

    return Tool(
        name="web_fetch",
        description="fetch",
        parameters=object_schema({"url": {"type": "string"}}, ["url"]),
        required_keys=["url"],
        handler=handler,
        is_read_only=True,
        danger_level="medium",
        capabilities=("network",),
    )


def _policy() -> ApprovalPolicy:
    return ApprovalPolicy(ModusConfig().policy)


# ── resource key extraction ──


def test_bash_resource_key_is_the_rewritten_command():
    assert _default_resource_key(_command_tool(), {"command": "cat a"}) == "cat a"
    # A different command is a different resource.
    assert _default_resource_key(_command_tool(), {"command": "rm -rf /"}) == "rm -rf /"


def test_web_fetch_resource_key_is_the_origin():
    key = _default_resource_key(_fetch_tool(), {"url": "https://example.com/path"})
    assert key == "example.com"


def test_web_fetch_origin_does_not_include_path():
    assert _default_resource_key(_fetch_tool(), {"url": "http://a.io/x?q=1"}) == "a.io"


def test_unscooped_tool_has_no_resource_key():
    async def handler(_payload, _context):  # pragma: no cover
        raise AssertionError

    tool = Tool("ls", "list", object_schema({}), handler, is_read_only=True)
    assert tool.resource_key({}) is None
    assert _default_resource_key(tool, {}) is None


# ── scoped_decision: approve one resource does not approve another ──


def test_bash_approve_command_not_generic():
    """批准 `cat a` 后 `rm -rf` 仍需 ASK（A1 验收第一条）。"""
    policy = _policy()
    store = SessionGrantStore()
    tool = _command_tool()

    # Nothing remembered yet: both need asking.
    assert policy.scoped_decision(tool, "cat a", store) is ApprovalDecision.ASK
    assert policy.scoped_decision(tool, "rm -rf /", store) is ApprovalDecision.ASK

    # Approve `cat a` and remember it.
    store.record_grant("bash", "cat a", "approve")

    # Same resource reuses; a different resource does not.
    assert policy.scoped_decision(tool, "cat a", store) is ApprovalDecision.ALLOW
    assert policy.scoped_decision(tool, "rm -rf /", store) is ApprovalDecision.ASK


def test_web_fetch_origin_scope():
    """批准 host A 后 host B 仍需 ASK（A1 验收第二条）。"""
    policy = _policy()
    store = SessionGrantStore()
    tool = _fetch_tool()

    store.record_grant("web_fetch", "example.com", "approve")

    # web_fetch is a noSessionCache (SSRF) tool: even the SAME origin is not
    # silently reused — but a different origin is still separately asked.
    assert policy.scoped_decision(tool, "example.com", store) is ApprovalDecision.ASK
    assert policy.scoped_decision(tool, "10.0.0.1", store) is ApprovalDecision.ASK


def test_per_resource_session_grant_reuses_same_key():
    """per-resource 记录 → 同 resource_key 复用。"""
    store = SessionGrantStore()
    store.record_grant("bash", "cat a", "approve")
    grant = store.lookup("bash", "cat a")
    assert grant is not None
    assert grant.decision == "approve"
    assert store.lookup("bash", "rm -rf /") is None


def test_grant_never_applies_without_resource_key():
    """无 resource_key（per-tool 工具）时 session grant 不生效。"""
    policy = _policy()
    store = SessionGrantStore()
    store.record_grant("tool", "x", "approve")

    async def handler(_p, _c):  # pragma: no cover
        raise AssertionError

    tool = Tool("tool", "t", object_schema({}), handler, is_read_only=False, danger_level="high")
    # A grant keyed on a resource can never match a tool with no resource key.
    assert policy.scoped_decision(tool, None, store) is ApprovalDecision.ASK


def test_no_session_cache_high_risk_never_reuses():
    """SSRF/敏感 exec 永远 ASK——session 内永不静默复用。"""
    policy = _policy()
    store = SessionGrantStore()
    store.record_grant("web_fetch", "example.com", "approve")

    # web_fetch is SSRF-prone: even a remembered grant is ignored.
    assert store.lookup("web_fetch", "example.com") is None
    assert policy.scoped_decision(_fetch_tool(), "example.com", store) is ApprovalDecision.ASK


def test_bash_remembered_command_reuses_but_other_command_does_not():
    """bash 按命令作用域：记住的 `cat a` 复用，`rm -rf` 仍 ASK。"""
    policy = _policy()
    store = SessionGrantStore()
    store.record_grant("bash", "cat a", "approve")

    assert policy.scoped_decision(_command_tool(), "cat a", store) is ApprovalDecision.ALLOW
    assert policy.scoped_decision(_command_tool(), "rm -rf /", store) is ApprovalDecision.ASK


def test_no_session_cache_applies_only_to_implicit_cache_not_rules():
    """noSessionCache 只约束隐式缓存；显式规则（A2）由人授权，可以生效。"""
    policy = _policy()
    store = SessionGrantStore()
    store.add_rule("bash", "cat *", "allow")
    assert policy.scoped_decision(_command_tool(), "cat b", store) is ApprovalDecision.ALLOW


# ── deny rules win ──


def test_deny_rule_overrides_everything():
    policy = _policy()
    store = SessionGrantStore()
    store.add_rule("bash", "rm -rf *", "deny")
    assert policy.scoped_decision(_command_tool(), "rm -rf /", store) is ApprovalDecision.DENY


def test_allow_rule_only_elevates_ask_never_denied_base():
    """规则允许最多把 ASK 提为 ALLOW；政策已 DENY 的不会被规则放行。"""
    policy = _policy()
    store = SessionGrantStore()
    store.add_rule("bash", "*", "allow")

    # An ASK tool with a per-tool rule is allowed.
    assert policy.scoped_decision(_command_tool(), None, store) is ApprovalDecision.ALLOW


def test_rule_pattern_matches_tokens():
    from modus.policy.approval import _rule_matches

    assert _rule_matches("cat *", "cat b")
    assert _rule_matches("cat a", "cat a")
    assert not _rule_matches("cat a", "rm -rf /")
    assert not _rule_matches("cat *", "rm -rf /")


# ── executor wiring ──


async def _run(tool: Tool, args: str, *, callback=None, store=None, granted=None) -> ToolResult:
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry)
    ctx = ToolContext(
        cwd=".", config=ModusConfig(), approval_callback=callback,
        grant_store=store, granted_capabilities=granted,
    )
    call = {"id": "c", "type": "function", "function": {"name": tool.name, "arguments": args}}
    return (await executor.execute_all([call], ctx))[0]


@pytest.mark.asyncio
async def test_executor_remember_records_session_grant():
    """r 记住本资源 → 后续同 resource_key 不再打断。"""
    calls = {"n": 0}

    async def handler(_payload, _context):
        return ToolResult("ran")

    async def callback(_request):
        calls["n"] += 1
        return ApprovalResponse.approve(remember=True)

    store = SessionGrantStore()
    tool = _running_command_tool(handler)

    # First call: approved and remembered.
    first = await _run(tool, '{"command":"cat a"}', callback=callback, store=store)
    assert first.content == "ran"
    assert store.lookup("bash", "cat a") is not None

    # Second identical call: the session grant is reused — no re-ask.
    second = await _run(tool, '{"command":"cat a"}', callback=callback, store=store)
    assert second.content == "ran"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_executor_no_remember_keeps_asking():
    """未显式记住时，每次仍走 callback。"""
    calls = {"n": 0}

    async def handler(_payload, _context):
        return ToolResult("ran")

    async def callback(_request):
        calls["n"] += 1
        return "approve"

    store = SessionGrantStore()
    tool = _running_command_tool(handler)

    await _run(tool, '{"command":"cat a"}', callback=callback, store=store)
    await _run(tool, '{"command":"cat a"}', callback=callback, store=store)

    assert calls["n"] == 2
    assert store.lookup("bash", "cat a") is None


@pytest.mark.asyncio
async def test_executor_remember_without_store_is_noop():
    """没有 grant_store 时 remember 是纯提示，不报错。"""
    async def handler(_payload, _context):
        return ToolResult("ran")

    async def callback(_request):
        return ApprovalResponse.approve(remember=True)

    tool = _running_command_tool(handler)
    result = await _run(tool, '{"command":"cat a"}', callback=callback, store=None)
    assert result.content == "ran"


@pytest.mark.asyncio
async def test_executor_approval_request_carries_resource_key():
    seen = {}

    async def handler(_payload, _context):
        return ToolResult("ran")

    async def callback(request):
        seen.update(request)
        return "approve"

    tool = _running_command_tool(handler)
    await _run(tool, '{"command":"cat a"}', callback=callback)
    assert seen.get("resource_key") == "cat a"


@pytest.mark.asyncio
async def test_executor_modify_remember_records_modified_resource():
    """m 改参 + 记住 → 以改后 payload 的 resource_key 记录（A1 步骤 4）。"""
    from modus.policy.approval import SessionGrantStore

    async def handler(payload, _context):
        return ToolResult("ran:" + payload.get("command", ""))

    async def callback(_request):
        return ApprovalResponse.modify({"command": "cat b"}, remember=True)

    store = SessionGrantStore()
    tool = _running_command_tool(handler)

    result = await _run(tool, '{"command":"cat a"}', callback=callback, store=store)
    assert result.content == "ran:cat b"
    # Remembered under the MODIFIED resource, not the original.
    assert store.lookup("bash", "cat b") is not None
    assert store.lookup("bash", "cat a") is None
