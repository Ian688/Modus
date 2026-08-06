from dataclasses import replace

import pytest

from modus.config import ModusConfig
from modus.policy.approval import ApprovalDecision, ApprovalPolicy
from modus.tools.base import Tool, object_schema


def _tool(*, danger: str = "safe", read_only: bool = True, requires_approval: bool = False) -> Tool:
    async def handler(_payload, _context):  # pragma: no cover - policy is pure
        raise AssertionError("policy must not execute a tool")

    return Tool(
        name="example",
        description="example",
        parameters=object_schema({}),
        handler=handler,
        danger_level=danger,
        is_read_only=read_only,
        requires_approval=requires_approval,
    )


def _policy(*, hitl_mode: str = "auto") -> ApprovalPolicy:
    config = ModusConfig(policy=replace(ModusConfig().policy, hitl_mode=hitl_mode))
    return ApprovalPolicy(config.policy)


def test_safe_read_only_tool_is_allowed_automatically():
    assert _policy().evaluate(_tool()) is ApprovalDecision.ALLOW


@pytest.mark.parametrize("danger", ["medium", "high"])
def test_mutating_or_high_risk_tool_requires_user_approval_in_auto_mode(danger):
    tool = _tool(danger=danger, read_only=False, requires_approval=True)
    assert _policy().evaluate(tool) is ApprovalDecision.ASK


def test_always_mode_requires_approval_even_for_safe_read_tool():
    assert _policy(hitl_mode="always").evaluate(_tool()) is ApprovalDecision.ASK


def test_explicit_session_allowlist_overrides_a_tool_that_normally_asks():
    tool = _tool(danger="high", read_only=False, requires_approval=True)
    assert _policy().evaluate(tool, session_decision="allow") is ApprovalDecision.ALLOW


def test_explicit_session_denylist_overrides_a_safe_tool():
    assert _policy().evaluate(_tool(), session_decision="deny") is ApprovalDecision.DENY


def test_unknown_hitl_mode_and_invalid_override_fail_closed():
    tool = _tool(danger="unknown", read_only=True)
    assert _policy(hitl_mode="unexpected").evaluate(tool) is ApprovalDecision.DENY
    assert _policy().evaluate(_tool(), session_decision="unexpected") is ApprovalDecision.DENY
