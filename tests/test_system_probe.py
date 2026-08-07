"""system_probe: the Phase 2 read lens.

A pure-stdlib, schema-capped host snapshot that is safe to auto-allow (no
file/log content, no cmdline, bounded).  Verifies the declaration contract
(capability gate + approval auto-ALLOW + disclosure=none) and the payload
shape on the real host.
"""

from __future__ import annotations

import json
import sys

import pytest

from modus.config import ModusConfig
from modus.policy.approval import ApprovalDecision, ApprovalPolicy
from modus.tools.base import ToolContext, ToolResult
from modus.tools.builtins import get_builtin_tools
from modus.tools.capabilities import capabilities_granted
from modus.tools.executor import ToolExecutor
from modus.tools.registry import ToolRegistry
from modus.tools.system_probe import (
    _MAX_PROCESSES,
    _probe_cpu,
    _probe_disk,
    _probe_memory,
    _probe_platform,
    system_probe,
    system_probe_payload,
)


# ── declaration contract ──


def test_system_probe_is_free_read_lens():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    tool = tools["system_probe"]
    assert tool.requires_approval is False
    assert tool.is_read_only is True
    assert tool.danger_level == "safe"
    assert tool.data_disclosure == "none"
    assert tool.capabilities == ("filesystem",)
    # No opaque-parameter surface beyond the bounded knobs.
    assert "max_processes" in tool.parameters["properties"]
    assert "include_logs" in tool.parameters["properties"]


def test_system_probe_auto_allow_under_approval_policy():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    tool = tools["system_probe"]
    decision = ApprovalPolicy(ModusConfig().policy).evaluate(tool)
    assert decision is ApprovalDecision.ALLOW


def test_system_probe_passes_capability_gate():
    # Under the default (no grant) and a T1 filesystem-only lens.
    assert capabilities_granted(("filesystem",), None) is True
    assert capabilities_granted(("filesystem",), ["filesystem"]) is True
    # A run without filesystem is denied.
    assert capabilities_granted(("filesystem",), ["exec"]) is False


@pytest.mark.asyncio
async def test_system_probe_executes_through_executor():
    """An executor call succeeds and returns the schema-capped snapshot."""
    tools = {tool.name: tool for tool in get_builtin_tools()}
    registry = ToolRegistry()
    registry.register(tools["system_probe"])
    executor = ToolExecutor(registry)
    call = {"id": "sp", "type": "function",
            "function": {"name": "system_probe", "arguments": "{}"}}
    ctx = ToolContext(cwd=".", config=ModusConfig())
    results = await executor.execute_all([call], ctx)
    assert not results[0].is_error
    data = json.loads(results[0].content)
    assert data["schema"] == "modus.system.v1"
    assert data["platform"]["system"]
    assert "cpu" in data and "memory" in data and "disk" in data
    assert results[0].metadata["operation"] == "system_probe"


# ── payload shape on the real host ──


def test_payload_has_all_core_sections():
    snap = system_probe_payload(max_processes=3)
    assert snap["schema"] == "modus.system.v1"
    assert "platform" in snap and "cpu" in snap
    assert "memory" in snap and "disk" in snap
    assert isinstance(snap["processes"], list)


def test_processes_bounded_and_content_free():
    snap = system_probe_payload(max_processes=2)
    assert len(snap["processes"]) <= 2
    for proc in snap["processes"]:
        # Never leaks argv/cmdline — only a truncated name.
        assert "cmdline" not in proc
        assert "command" not in proc
        assert "pid" in proc
        name = proc.get("name") or ""
        assert len(name) <= 64


def test_logs_never_include_content():
    snap = system_probe_payload(include_logs=True)
    for entry in snap.get("logs", []):
        assert "path" in entry
        assert "content" not in entry
        assert "text" not in entry
        # If the dir is readable, sizes are numbers (or truncated flag).
        if entry.get("readable"):
            assert isinstance(entry.get("file_count", 0), int)
            assert isinstance(entry.get("total_bytes", 0), int)


def test_include_logs_false_omits_logs():
    snap = system_probe_payload(include_logs=False)
    assert "logs" not in snap


def test_platform_section_has_system():
    plat = _probe_platform()
    assert plat["system"]
    assert plat["machine"]


# ── per-source robustness (never fails the whole probe) ──


def test_cpu_probe_never_raises():
    cpu = _probe_cpu()
    assert "cpu_count" in cpu
    assert isinstance(cpu["cpu_count"], int)


def test_memory_probe_never_raises():
    mem = _probe_memory()
    # total present on this host, free is platform-dependent.
    assert "total_bytes" in mem
    assert "free_bytes" in mem


def test_disk_probe_never_raises():
    rows = _probe_disk()
    assert rows
    for row in rows:
        assert "path" in row
        if "error" not in row:
            assert row["total_bytes"] > 0


@pytest.mark.asyncio
async def test_handler_returns_bounded_json():
    result = await system_probe({"max_processes": 3}, ToolContext(cwd=".", config=ModusConfig()))
    assert isinstance(result, ToolResult)
    data = json.loads(result.content)
    assert data["schema"] == "modus.system.v1"
    assert len(data["processes"]) <= 3
