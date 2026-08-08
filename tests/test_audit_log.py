"""AuditLog: A1 scope / resource_key fields + rotation/degradation retention.

Covers the Wave3 A1 audit dimension — a recorded decision carries the scope
level and the resource key that produced it — while keeping the conservative
storage policy (rotation, in-memory degradation) intact.
"""

from __future__ import annotations

import json

import pytest

from modus.policy.audit_log import AuditLog


# ── A1 scope fields ──


def test_record_writes_scope_and_resource_key(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(
        tool_name="bash",
        input_data={"command": "cat a"},
        outcome="approved",
        approver="human",
        cwd="/tmp",
        scope="per-resource",
        resource_key="cat a",
    )
    entries = log.tail(10)
    assert len(entries) == 1
    assert entries[0]["scope"] == "per-resource"
    assert entries[0]["resource_key"] == "cat a"


def test_record_scope_optional_backward_compatible(tmp_path):
    """Legacy callers without a scope dimension omit both fields."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(
        tool_name="read_file",
        input_data={"path": "x.py"},
        outcome="allowed",
        approver="policy",
        cwd="/tmp",
    )
    entries = log.tail(10)
    assert len(entries) == 1
    assert "scope" not in entries[0]
    assert "resource_key" not in entries[0]


def test_record_resource_key_redacted(tmp_path):
    """A resource key carrying a credential is redacted before persistence."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(
        tool_name="web_fetch",
        input_data={"url": "http://x/"}, outcome="denied", approver="human",
        cwd="/tmp", scope="per-invocation",
        resource_key="http://a.io/?token=sk-abcdefghijklmnopqrstuvwxyz",
    )
    entry = log.tail(10)[0]
    assert "token=" in entry["resource_key"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in entry["resource_key"]


def test_record_scope_and_verification_coexist(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(
        tool_name="bash", input_data={"command": "make"}, outcome="deny",
        approver="system", cwd="", phase="approval",
        scope="per-invocation", resource_key="make",
        verification={"resolution_reason": "approval_timeout"},
    )
    entry = log.tail(10)[0]
    assert entry["scope"] == "per-invocation"
    assert entry["resource_key"] == "make"
    assert entry["verification"] == {"resolution_reason": "approval_timeout"}


# ── grant records carry the audit scope (A1) ──


def test_session_grant_exposes_audit_scope():
    from modus.policy.approval import ApprovalScope, SessionGrant, SessionGrantStore

    store = SessionGrantStore()
    store.record_grant("bash", "cat a", "approve")
    grant = store.lookup("bash", "cat a")
    assert grant is not None
    assert grant.audit_scope() == ApprovalScope.PER_RESOURCE.value
    assert grant.resource_key == "cat a"

    rule_grant = SessionGrant(
        tool="bash", resource_key="cat a", decision="approve",
        created_at=0.0, pattern="cat *",
    )
    assert rule_grant.audit_scope() == ApprovalScope.PER_RESOURCE.value


# ── conservative storage policy is preserved ──


def test_record_scope_survives_rotation(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, rotate_bytes=150, rotate_keep=2)
    for index in range(6):
        log.record(
            tool_name="bash", input_data={"command": f"echo {index}"},
            outcome="approved", approver="human", cwd="/tmp",
            scope="per-resource", resource_key=f"echo {index}",
        )
    # Rotation happened (a rolled copy exists) and the newest entry retains the
    # scope fields.
    copies = list(tmp_path.glob("audit-*.jsonl"))
    assert copies
    latest = log.tail(10)[-1]
    assert latest["scope"] == "per-resource"
    assert "resource_key" in latest


def test_record_scope_degraded_to_memory(tmp_path, monkeypatch):
    """A write failure still buffers the scoped event in memory."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)

    def _explode(_self, _event):
        raise OSError("disk full")

    monkeypatch.setattr(AuditLog, "_append", _explode)
    log.record(
        tool_name="bash", input_data={"command": "make"}, outcome="deny",
        approver="system", cwd="", phase="approval",
        scope="per-invocation", resource_key="make",
    )
    assert log.degraded is True
    assert len(log.degraded_memory) == 1
    assert log.degraded_memory[0]["scope"] == "per-invocation"
    assert log.degraded_memory[0]["resource_key"] == "make"
