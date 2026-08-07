"""Phase 0: capability declarations + deny-first grant gate.

Each tool declares the capability classes it needs (filesystem / exec /
network / memory / agent).  The executor denies any tool whose declared
capabilities are not all granted by the active run, BEFORE approval is even
considered.  ``None`` grants (the default) keep today's unrestricted behavior.
"""
from __future__ import annotations

import pytest

from modus.config import ModusConfig, load_config
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.builtins import get_builtin_tools
from modus.tools.capabilities import capabilities_granted
from modus.tools.executor import ToolExecutor
from modus.tools.registry import ToolRegistry

VALID = ("filesystem", "exec", "network", "memory", "agent")


def _executor_with(tool: Tool) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(registry)


async def _run(executor: ToolExecutor, call: dict, *, callback=None, granted=None) -> ToolResult:
    ctx = ToolContext(
        cwd=".", config=ModusConfig(), approval_callback=callback,
        granted_capabilities=granted,
    )
    return (await executor.execute_all([call], ctx))[0]


def _tool_call(name: str, args: str = "{}") -> dict:
    return {"id": "c", "type": "function", "function": {"name": name, "arguments": args}}


# ── pure gate logic ──


def test_grant_none_grants_everything():
    assert capabilities_granted((), None) is True
    assert capabilities_granted(("filesystem",), None) is True
    assert capabilities_granted(("filesystem", "exec"), None) is True


def test_explicit_grant_is_fail_closed():
    # Declared capability missing from the grant -> denied.
    assert capabilities_granted(("filesystem",), []) is False
    assert capabilities_granted(("filesystem", "exec"), ["filesystem"]) is False
    # No declared capability at all -> denied under any explicit grant.
    assert capabilities_granted((), ["filesystem"]) is False


def test_explicit_grant_allows_declared_capabilities():
    assert capabilities_granted(("filesystem",), ["filesystem"]) is True
    assert capabilities_granted(("filesystem", "exec"), ["filesystem", "exec"]) is True


# ── executor deny-first ──


@pytest.mark.asyncio
async def test_gate_denies_before_approval():
    """A tool not granted never reaches the approval callback."""
    called = []

    async def handler(_payload, _ctx):
        called.append("execute")
        return ToolResult("ran")

    async def callback(_request):
        called.append("approve")
        return "approve"

    tool = Tool(
        "bash", "shell", object_schema({}), handler,
        is_read_only=False, danger_level="high", requires_approval=True,
        capabilities=("exec",),
    )
    result = await _run(
        _executor_with(tool), _tool_call("bash"), callback=callback,
        granted=["filesystem"],
    )

    assert result.is_error
    assert "not granted capability" in result.content
    assert called == []  # neither approval nor execution


@pytest.mark.asyncio
async def test_gate_grants_runs_normally():
    """A granted tool executes through the normal approval path."""
    called = []

    async def handler(_payload, _ctx):
        called.append("execute")
        return ToolResult("ran")

    async def callback(_request):
        called.append("approve")
        return "approve"

    tool = Tool(
        "bash", "shell", object_schema({}), handler,
        is_read_only=False, danger_level="high", requires_approval=True,
        capabilities=("exec",),
    )
    result = await _run(
        _executor_with(tool), _tool_call("bash"), callback=callback,
        granted=["filesystem", "exec"],
    )

    assert not result.is_error
    assert result.content == "ran"
    assert called == ["approve", "execute"]


@pytest.mark.asyncio
async def test_no_grant_set_is_unrestricted():
    """With granted_capabilities=None, the gate does not interfere."""
    called = []

    async def handler(_payload, _ctx):
        called.append("execute")
        return ToolResult("ran")

    tool = Tool(
        "bash", "shell", object_schema({}), handler,
        is_read_only=False, danger_level="high", requires_approval=True,
        capabilities=("exec",),
    )
    result = await _run(_executor_with(tool), _tool_call("bash"), granted=None)

    assert result.is_error  # no approval callback -> approval policy denies
    assert "requires approval" in result.content
    assert called == []  # denied at approval, not at the capability gate


@pytest.mark.asyncio
async def test_read_only_allowed_tool_still_runs_under_grant():
    """An auto-allow read tool with a declared capability runs under the grant."""
    async def handler(_payload, _ctx):
        return ToolResult("read")

    tool = Tool(
        "read_file", "read", object_schema({}), handler,
        is_read_only=True, danger_level="safe", capabilities=("filesystem",),
    )
    result = await _run(_executor_with(tool), _tool_call("read_file"), granted=["filesystem"])

    assert not result.is_error
    assert result.content == "read"


# ── builtin declarations ──


def test_builtin_tools_declare_capabilities():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    # The four classes that drive the permission ladder on the static surface.
    declared = {cap for tool in tools.values() for cap in tool.capabilities}
    assert {"filesystem", "exec", "network", "memory"} <= declared


def test_tool_capability_classes_are_valid():
    for tool in get_builtin_tools():
        for cap in tool.capabilities:
            assert cap in VALID, f"{tool.name} declares invalid capability {cap!r}"


def test_lens_tools_are_filesystem_and_free():
    """T1 lens: read/scan tools stay auto-allow and declare filesystem."""
    tools = {tool.name: tool for tool in get_builtin_tools()}
    for name in ("read_file", "grep", "search_code", "list_dir", "glob"):
        assert tools[name].capabilities == ("filesystem",)
        assert tools[name].requires_approval is False


def test_exec_and_network_tools_declare_their_classes():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    assert tools["bash"].capabilities == ("exec",)
    assert tools["run_tests"].capabilities == ("exec",)
    assert tools["web_search"].capabilities == ("network",)
    assert tools["web_fetch"].capabilities == ("network",)
    assert tools["save_memory"].capabilities == ("memory",)
    assert tools["search_memory"].capabilities == ("memory",)


# ── config wiring ──


def test_capability_grant_env_wiring():
    cfg = load_config(env={"MODUS_POLICY_CAPABILITY_GRANT": "filesystem,exec"})
    assert cfg.policy.capability_grant == ["filesystem", "exec"]

    cfg_none = load_config(env={"MODUS_POLICY_CAPABILITY_GRANT": ""})
    assert cfg_none.policy.capability_grant is None

    cfg_default = load_config(env={})
    assert cfg_default.policy.capability_grant is None


def test_capability_grant_round_trips_through_dict():
    from modus.config import ModusConfig, _config_to_dict, _dict_to_config

    cfg = ModusConfig()
    cfg.policy.capability_grant = ["filesystem"]
    restored = _dict_to_config(_config_to_dict(cfg))
    assert restored.policy.capability_grant == ["filesystem"]


# ── Phase 0 approval-surface complement: impact classification + audit fields ──


def _tool(is_read_only=True, danger="safe", requires_approval=False):
    async def handler(_p, _c):
        return ToolResult("ok")

    return Tool(
        "t", "d", object_schema({}), handler,
        is_read_only=is_read_only, danger_level=danger,
        requires_approval=requires_approval,
        capabilities=("filesystem",),
    )


def test_impact_class_classification():
    from modus.tools.executor import _impact_class

    assert _impact_class(_tool(is_read_only=True)) == "read-only"
    # Any non-read-only tool is conservatively classified as mutating.
    assert _impact_class(_tool(is_read_only=False, danger="high")) == "mutating"
    assert _impact_class(_tool(is_read_only=False, danger="medium")) == "mutating"


@pytest.mark.asyncio
async def test_approval_request_carries_impact_class():
    seen = {}

    async def handler(_p, _c):
        return ToolResult("ok")

    async def callback(request):
        seen.update(request)
        return "approve"

    tool = Tool(
        "bash", "shell", object_schema({}), handler,
        is_read_only=False, danger_level="high", requires_approval=True,
        capabilities=("exec",),
    )
    result = await _run(_executor_with(tool), _tool_call("bash"), callback=callback)
    assert not result.is_error
    assert seen.get("impact_class") == "mutating"


def test_audit_record_writes_phase_and_verification(tmp_path):
    from modus.policy.audit_log import AuditLog

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(
        tool_name="write_file", input_data={"path": "x.py", "content": "c"},
        outcome="approved", approver="human", cwd="/tmp",
        phase="mutating",
        verification={"status": "pending", "attempts": 0},
    )

    entry = log.tail(10)[0]
    assert entry["phase"] == "mutating"
    assert entry["verification"] == {"status": "pending", "attempts": 0}
    assert entry["timestamp"]


def test_audit_record_defaults_phase_to_execution(tmp_path):
    """Backward compatibility: callers that omit the new fields keep working."""
    from modus.policy.audit_log import AuditLog

    path = tmp_path / "audit.jsonl"
    AuditLog(path).record(
        tool_name="read_file", input_data={"path": "x.py"},
        outcome="allowed", approver="policy", cwd="/tmp",
    )
    entry = AuditLog(path).tail(10)[0]
    assert entry["phase"] == "execution"
    assert "verification" not in entry
