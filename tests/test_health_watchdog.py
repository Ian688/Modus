"""T3 health watchdog: explicit action table, each action audited.

The watchdog samples process-level health (disk, audit size, DB size, pending
approvals, browser page) and takes only explicitly enumerated actions:
PRUNE_CACHE / RECYCLE_BROWSER / DENY_STALE_APPROVALS / NOTIFY_USER.  Every
action is written to the AuditLog.  Fail-safe: a probe that raises produces no
action.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import modus.health as health


def _reset_probes(monkeypatch) -> None:
    """Restore the probe namespace so each test is deterministic."""
    for name in (
        "disk_free_mb", "audit_size", "db_size",
        "pending_approval_max_age", "browser_alive", "notify",
    ):
        monkeypatch.setattr(health.Probes, name, None)
    health._wire_default_probes()


def _audit_recorder(tmp_path, monkeypatch):
    from modus.policy.audit_log import AuditLog

    audit_path = tmp_path / "audit.jsonl"
    log = AuditLog(audit_path)
    monkeypatch.setattr(
        health, "_audit", lambda entry: log.record(
            tool_name="watchdog",
            input_data={k: v for k, v in entry.items() if k not in {"outcome", "action"}},
            outcome=f"watchdog.{entry.get('action') or 'action'}",
            approver="system", cwd="", phase="watchdog",
        ),
    )
    return audit_path, log


@pytest.mark.asyncio
async def test_watchdog_prune_actions_logged(tmp_path, monkeypatch):
    """Low disk -> PRUNE_CACHE action emitted and audited."""
    _reset_probes(monkeypatch)
    audit_path, log = _audit_recorder(tmp_path, monkeypatch)

    prune_calls = {"count": 0}
    monkeypatch.setattr(health.Probes, "disk_free_mb", lambda: 100.0)  # very low
    monkeypatch.setattr(health.Probes, "audit_size", lambda: 10)
    monkeypatch.setattr(health.Probes, "db_size", lambda: 20)
    monkeypatch.setattr(health.Probes, "browser_alive", lambda: True)
    monkeypatch.setattr(health.Probes, "pending_approval_max_age", lambda: None)

    import modus.desktop.db as db

    monkeypatch.setattr(
        db, "prune_expired",
        lambda config=None: prune_calls.update(count=prune_calls["count"] + 1) or {},
    )
    monkeypatch.setattr(db, "checkpoint_now", lambda: 0)

    report = await health.sample()
    assert health.PRUNE_CACHE in report["actions"]
    assert prune_calls["count"] == 1
    # The action was audited.
    events = log.tail(limit=10)
    assert any(e["tool_name"] == "watchdog" and e["outcome"] == "watchdog.PRUNE_CACHE" for e in events)


@pytest.mark.asyncio
async def test_watchdog_recycles_dead_browser(tmp_path, monkeypatch):
    _reset_probes(monkeypatch)
    audit_path, log = _audit_recorder(tmp_path, monkeypatch)

    monkeypatch.setattr(health.Probes, "disk_free_mb", lambda: 10_000.0)
    monkeypatch.setattr(health.Probes, "audit_size", lambda: 10)
    monkeypatch.setattr(health.Probes, "db_size", lambda: 20)
    monkeypatch.setattr(health.Probes, "browser_alive", lambda: False)
    monkeypatch.setattr(health.Probes, "pending_approval_max_age", lambda: None)

    closed = {"count": 0}
    import modus.tools.browser as browser

    async def _fake_close():
        closed["count"] += 1

    monkeypatch.setattr(browser, "_close_browser", _fake_close)

    report = await health.sample()
    assert health.RECYCLE_BROWSER in report["actions"]
    assert closed["count"] == 1
    events = log.tail(limit=10)
    assert any(e["outcome"] == "watchdog.RECYCLE_BROWSER" for e in events)


@pytest.mark.asyncio
async def test_watchdog_denies_stale_approvals(tmp_path, monkeypatch):
    _reset_probes(monkeypatch)
    audit_path, log = _audit_recorder(tmp_path, monkeypatch)

    monkeypatch.setattr(health.Probes, "disk_free_mb", lambda: 10_000.0)
    monkeypatch.setattr(health.Probes, "audit_size", lambda: 10)
    monkeypatch.setattr(health.Probes, "db_size", lambda: 20)
    monkeypatch.setattr(health.Probes, "browser_alive", lambda: True)
    monkeypatch.setattr(health.Probes, "pending_approval_max_age", lambda: 700.0)  # stale

    denied = {"count": 0}
    import modus.desktop.approvals as approvals

    def _fake_deny_stale(**kwargs):
        denied["count"] += 1
        return 2

    monkeypatch.setattr(approvals.approval_broker, "deny_stale", _fake_deny_stale)

    report = await health.sample()
    assert health.DENY_STALE_APPROVALS in report["actions"]
    assert denied["count"] == 1
    events = log.tail(limit=10)
    assert any(e["outcome"] == "watchdog.DENY_STALE_APPROVALS" for e in events)


@pytest.mark.asyncio
async def test_watchdog_notifies_user_on_disk_pressure(tmp_path, monkeypatch):
    _reset_probes(monkeypatch)
    _audit_recorder(tmp_path, monkeypatch)

    monkeypatch.setattr(health.Probes, "disk_free_mb", lambda: 50.0)
    monkeypatch.setattr(health.Probes, "audit_size", lambda: 10)
    monkeypatch.setattr(health.Probes, "db_size", lambda: 20)
    monkeypatch.setattr(health.Probes, "browser_alive", lambda: True)
    monkeypatch.setattr(health.Probes, "pending_approval_max_age", lambda: None)

    notified = []
    monkeypatch.setattr(health.Probes, "notify", lambda message: notified.append(message))

    report = await health.sample()
    assert health.NOTIFY_USER in report["actions"]
    assert notified  # a banner was scheduled


@pytest.mark.asyncio
async def test_watchdog_failsafe_no_action_on_bad_probe(tmp_path, monkeypatch):
    """A raising probe degrades to 'no action' (never crashes the loop)."""
    _reset_probes(monkeypatch)
    _audit_recorder(tmp_path, monkeypatch)

    def _boom():
        raise OSError("disk probe broken")

    monkeypatch.setattr(health.Probes, "disk_free_mb", _boom)
    monkeypatch.setattr(health.Probes, "audit_size", lambda: 10)
    monkeypatch.setattr(health.Probes, "db_size", lambda: 20)
    monkeypatch.setattr(health.Probes, "browser_alive", lambda: True)
    monkeypatch.setattr(health.Probes, "pending_approval_max_age", lambda: None)

    report = await health.sample()
    assert report["disk_free_mb"] is None
    assert health.PRUNE_CACHE not in report["actions"]
    assert report["actions"] == []


@pytest.mark.asyncio
async def test_watchdog_healthy_no_actions(tmp_path, monkeypatch):
    _reset_probes(monkeypatch)
    _audit_recorder(tmp_path, monkeypatch)

    monkeypatch.setattr(health.Probes, "disk_free_mb", lambda: 50_000.0)
    monkeypatch.setattr(health.Probes, "audit_size", lambda: 10)
    monkeypatch.setattr(health.Probes, "db_size", lambda: 20)
    monkeypatch.setattr(health.Probes, "browser_alive", lambda: True)
    monkeypatch.setattr(health.Probes, "pending_approval_max_age", lambda: 10.0)

    report = await health.sample()
    assert report["actions"] == []


def test_watchdog_start_is_idempotent():
    w = health.Watchdog(interval=0.05)
    w.start()
    w.start()  # second start must not create a second task
    assert w._task is not None
    import asyncio

    asyncio.get_event_loop().run_until_complete(w.stop())
