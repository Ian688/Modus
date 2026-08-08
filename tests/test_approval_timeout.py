"""T3 approval timeout: the RunApprovalBroker must fail closed unattended.

An approval future with no human response resolves to ``deny`` after the
configured timeout (default 600s, configurable via runtime.approval_timeout_seconds
/ MODUS_APPROVAL_TIMEOUT) and the denial is audited — so a run can never hang
forever on the ASK gate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from modus.config import ModusConfig


def _isolated_broker(monkeypatch, audit_path: Path) -> None:
    """Patch module singletons so the broker tests never touch real config."""
    import modus.desktop.approvals as approvals
    import modus.config as config_mod

    from modus.desktop.approvals import RunApprovalBroker

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda env=None: ModusConfig(),
    )
    monkeypatch.setattr(
        approvals,
        "_config_timeout",
        lambda: 600.0,
    )
    # Record audit calls instead of writing to the real audit log.
    calls: list[dict] = []
    monkeypatch.setattr(
        approvals,
        "_audit_timeout_deny",
        lambda run_id, approval_id: calls.append({"run_id": run_id, "approval_id": approval_id}),
    )
    return calls


@pytest.mark.asyncio
async def test_approval_timeout_denies(monkeypatch, tmp_path):
    from modus.desktop.approvals import RunApprovalBroker

    _isolated_broker(monkeypatch, tmp_path)
    broker = RunApprovalBroker()
    future = asyncio.get_running_loop().create_future()
    broker.register("run-a", "appr-1", future, timeout=0.05)

    decision = await asyncio.wait_for(future, timeout=2.0)
    assert decision == "deny"
    # The broker denies the future; the approval flow owns removal.
    assert broker.pending_count() == 1
    broker.remove("run-a", "appr-1")
    assert broker.pending_count() == 0


@pytest.mark.asyncio
async def test_resolve_before_timeout_wins_and_cancels_timer(monkeypatch, tmp_path):
    from modus.desktop.approvals import RunApprovalBroker

    _isolated_broker(monkeypatch, tmp_path)
    broker = RunApprovalBroker()
    future = asyncio.get_running_loop().create_future()
    broker.register("run-a", "appr-1", future, timeout=5.0)

    assert broker.resolve("run-a", "appr-1", "allow") is True
    assert future.done()
    assert future.result() == "allow"
    # A second resolve (or the late timer) must be a no-op.
    assert broker.resolve("run-a", "appr-1", "deny") is False


@pytest.mark.asyncio
async def test_timeout_audits_denial(monkeypatch, tmp_path):
    from modus.desktop.approvals import RunApprovalBroker

    audit_calls = _isolated_broker(monkeypatch, tmp_path)
    broker = RunApprovalBroker()
    future = asyncio.get_running_loop().create_future()
    broker.register("run-audit", "appr-1", future, timeout=0.05)
    await asyncio.wait_for(future, timeout=2.0)

    assert audit_calls == [{"run_id": "run-audit", "approval_id": "appr-1"}]


@pytest.mark.asyncio
async def test_deny_stale_denies_aged_pending(monkeypatch, tmp_path):
    from modus.desktop.approvals import RunApprovalBroker

    audit_calls = _isolated_broker(monkeypatch, tmp_path)
    # Control the broker's monotonic clock so "stale" is deterministic.
    import modus.desktop.approvals as approvals_mod

    clock = {"now": 0.0}
    monkeypatch.setattr(approvals_mod.time, "monotonic", lambda: clock["now"])

    broker = RunApprovalBroker()
    f1 = asyncio.get_running_loop().create_future()
    f2 = asyncio.get_running_loop().create_future()
    broker.register("run-1", "a1", f1, timeout=600.0)
    broker.register("run-2", "a2", f2, timeout=600.0)

    # Both are young; nothing is stale.
    clock["now"] = 50.0
    assert broker.deny_stale(max_age_seconds=100.0) == 0
    # Advance the clock: both are now older than 100s.
    clock["now"] = 150.0
    denied = broker.deny_stale(max_age_seconds=100.0)
    assert denied == 2
    assert f1.done() and f1.result() == "deny"
    assert f2.done() and f2.result() == "deny"
    assert len(audit_calls) == 2


def test_config_approval_timeout_env_maps(monkeypatch):
    from modus.config import load_config

    config = load_config(env={"MODUS_APPROVAL_TIMEOUT": "42"})
    assert config.runtime.approval_timeout_seconds == 42.0


def test_config_approval_timeout_default():
    from modus.config import load_config

    assert load_config().runtime.approval_timeout_seconds == 600.0
