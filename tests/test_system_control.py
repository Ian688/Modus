"""Cross-platform system control (Phase A5): ports + services.

port_list is a read-only lens (lsof/netstat); service_status is read-only;
service_restart is a T4 destructive action (requires approval).  Platform
backends split by os — tests pin declarations, approval policy, and the
lsof/netstat parsing; service commands are mocked so no real service is touched.
"""

from __future__ import annotations

import pytest

from modus.config import ModusConfig
from modus.policy.approval import ApprovalDecision, ApprovalPolicy
from modus.tools.base import ToolContext
from modus.tools.builtins import get_builtin_tools
from modus.tools.system_control import (
    _PORT_MAX,
    _service_backend,
    port_list,
    service_restart,
    service_status,
)


def _ctx():
    return ToolContext(cwd=".", config=ModusConfig())


# ── declaration contract ──


def test_system_control_tools_declared():
    tools = {t.name: t for t in get_builtin_tools()}
    for name in ("port_list", "service_status", "service_restart"):
        assert name in tools


def test_read_tools_auto_allow():
    tools = {t.name: t for t in get_builtin_tools()}
    policy = ApprovalPolicy(ModusConfig().policy)
    assert policy.evaluate(tools["port_list"]) is ApprovalDecision.ALLOW
    assert policy.evaluate(tools["service_status"]) is ApprovalDecision.ALLOW


def test_service_restart_approval_gated():
    tools = {t.name: t for t in get_builtin_tools()}
    tool = tools["service_restart"]
    assert tool.is_read_only is False
    assert tool.danger_level == "high"
    assert tool.requires_approval is True
    policy = ApprovalPolicy(ModusConfig().policy)
    assert policy.evaluate(tool) is ApprovalDecision.ASK


def test_service_backend_returns_platform():
    import os

    if os.name == "nt":
        assert _service_backend() == "windows"
    elif __import__("sys").platform == "darwin":
        assert _service_backend() == "launchctl"
    else:
        assert _service_backend() == "systemctl"


# ── port_list ──


@pytest.mark.asyncio
async def test_port_list_requires_no_args(monkeypatch):
    """port_list runs lsof and returns parsed rows (or empty)."""
    import subprocess

    fake_out = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\nnginx 123 user 10u IPv4 0x0 0t0 TCP *:80 (LISTEN)\n"

    def fake_run(cmd, **kw):
        class R:
            stdout = fake_out
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = await port_list({}, _ctx())
    assert not result.is_error
    assert "nginx" in result.content
    assert "123" in result.content


@pytest.mark.asyncio
async def test_port_list_empty(monkeypatch):
    import subprocess

    def fake_run(cmd, **kw):
        class R:
            stdout = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = await port_list({}, _ctx())
    assert not result.is_error
    assert "no listening" in result.content


# ── service_status / service_restart (mocked subprocess) ──


@pytest.mark.asyncio
async def test_service_status_mocked(monkeypatch):
    import subprocess

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "service: running\n"
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = await service_status({"service": "mysvc"}, _ctx())
    assert not result.is_error
    assert "running" in result.content


@pytest.mark.asyncio
async def test_service_restart_mocked(monkeypatch):
    import subprocess

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = await service_restart({"service": "mysvc"}, _ctx())
    assert not result.is_error
    assert "ok" in result.content
    assert calls  # the platform restart command ran


@pytest.mark.asyncio
async def test_service_restart_requires_name():
    result = await service_restart({}, _ctx())
    assert result.is_error


@pytest.mark.asyncio
async def test_service_status_requires_name():
    result = await service_status({}, _ctx())
    assert result.is_error
