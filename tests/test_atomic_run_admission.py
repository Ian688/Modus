"""Atomic database boundary for provider-free Run admission."""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def admission_db(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    return db


def _admit(db, *, run_id: str, session_id: str, request_id: str = ""):
    return db.create_run_admission(
        run_id, session_id, "default",
        client_request_id=request_id,
        client_request_fingerprint=f"fingerprint:{request_id}",
        config_snapshot={"schema": "modus.run-config.v1", "api_key": "secret"},
        root_title="用户任务", root_description="本次运行的根任务",
        root_actor_id="primary", root_actor_label="主持人",
        assigned_model_id="model-a",
    )


def test_create_run_admission_commits_running_run_and_root_as_one_fact(admission_db):
    db = admission_db
    session = db.create_session("atomic admission")

    run = _admit(
        db, run_id="run-atomic-admission", session_id=session["id"],
        request_id="request-atomic-admission",
    )

    root = db.get_run_task("task_run-atomic-admission_root")
    assert run["state"] == "running"
    assert run["session_id"] == session["id"]
    assert run["projection_revision"] == 2
    assert run["config_snapshot"]["api_key"] == "***"
    assert root is not None
    assert (
        root["run_id"], root["session_id"], root["task_kind"],
        root["status"], root["attempt"], root["assigned_model_id"],
    ) == (
        run["run_id"], session["id"], "root", "running", 1, "model-a",
    )


def test_create_run_admission_root_insert_failure_rolls_back_run(
    admission_db, monkeypatch,
):
    db = admission_db
    session = db.create_session("root rollback")
    real_conn = db._get_conn

    class FailingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *args):
            return self._conn.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, parameters=()):
            if "INSERT INTO run_tasks" in sql:
                raise sqlite3.OperationalError("injected root write failure")
            return self._conn.execute(sql, parameters)

    monkeypatch.setattr(db, "_get_conn", lambda: FailingConnection(real_conn()))
    with pytest.raises(sqlite3.OperationalError, match="injected root write failure"):
        _admit(
            db, run_id="run-root-write-failed", session_id=session["id"],
            request_id="request-root-write-failed",
        )
    monkeypatch.setattr(db, "_get_conn", real_conn)

    assert db.get_run("run-root-write-failed") is None
    assert db.get_run_task("task_run-root-write-failed_root") is None
    assert db.get_run_by_client_request_id("request-root-write-failed") is None


def test_create_run_admission_conflicts_are_total_noops(admission_db):
    db = admission_db
    owner = db.create_session("owner")
    other = db.create_session("other")
    first = _admit(
        db, run_id="run-admission-owner", session_id=owner["id"],
        request_id="request-owned",
    )
    original_root = db.get_run_task("task_run-admission-owner_root")

    assert _admit(
        db, run_id="run-admission-owner", session_id=other["id"],
        request_id="request-other",
    ) == {}
    assert _admit(
        db, run_id="run-other-id", session_id=other["id"],
        request_id="request-owned",
    ) == {}

    assert db.get_run("run-admission-owner") == first
    assert db.get_run_task("task_run-admission-owner_root") == original_root
    assert db.get_run("run-other-id") is None
    assert db.get_run_by_client_request_id("request-other") is None


def test_create_run_admission_returns_committed_snapshot_without_second_connection(
    admission_db, monkeypatch,
):
    db = admission_db
    session = db.create_session("single connection result")
    real_conn = db._get_conn
    opens = 0

    def count_connections():
        nonlocal opens
        opens += 1
        if opens > 1:
            raise sqlite3.OperationalError("unexpected post-commit read")
        return real_conn()

    monkeypatch.setattr(db, "_get_conn", count_connections)
    run = _admit(
        db, run_id="run-single-connection", session_id=session["id"],
        request_id="request-single-connection",
    )

    assert opens == 1
    assert run["run_id"] == "run-single-connection"
    assert run["state"] == "running"


def test_fail_run_admission_atomically_fails_owned_run_and_root(admission_db):
    db = admission_db
    session = db.create_session("admission compensation")
    _admit(db, run_id="run-compensation", session_id=session["id"])

    assert db.fail_run_admission(
        "run-compensation", expected_session_id=session["id"],
        stop_reason="admission_transport_failed",
    ) is True
    run = db.get_run("run-compensation")
    root = db.get_run_task("task_run-compensation_root")
    assert (run["state"], run["stop_reason"]) == (
        "failed", "admission_transport_failed",
    )
    assert root is not None and root["status"] == "failed"
    assert db.get_run_events("run-compensation") == []


def test_fail_run_admission_rejects_wrong_owner_and_post_event_relabel(admission_db):
    db = admission_db
    owner = db.create_session("owner")
    other = db.create_session("other")
    _admit(db, run_id="run-owned", session_id=owner["id"])

    assert db.fail_run_admission(
        "run-owned", expected_session_id=other["id"],
    ) is False
    assert db.get_run("run-owned")["state"] == "running"
    assert db.get_run_task("task_run-owned_root")["status"] == "running"

    db.upsert_run_event(owner["id"], {
        "event_id": "evt-provider-started", "run_id": "run-owned",
        "task_id": "task_run-owned_root", "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1,
        "timestamp": "2026-08-03T00:00:00.000Z", "mode": "default",
        "actor": {"kind": "system", "id": "system", "label": "system"},
        "type": "run_started", "status": "started",
        "payload": {"state": "running"},
    })
    assert [event["event_id"] for event in db.get_run_events("run-owned")] == [
        "evt-provider-started",
    ]
    assert db.fail_run_admission(
        "run-owned", expected_session_id=owner["id"],
    ) is False
    assert db.get_run("run-owned")["state"] == "running"
    assert db.get_run_task("task_run-owned_root")["status"] == "running"


def test_fail_run_admission_statement_failure_rolls_back_run_and_root(
    admission_db, monkeypatch,
):
    db = admission_db
    session = db.create_session("compensation rollback")
    _admit(db, run_id="run-compensation-rollback", session_id=session["id"])
    before_run = db.get_run("run-compensation-rollback")
    before_root = db.get_run_task("task_run-compensation-rollback_root")
    real_conn = db._get_conn

    class FailingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *args):
            return self._conn.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, parameters=()):
            if "UPDATE run_tasks SET status='failed'" in sql:
                raise sqlite3.OperationalError("injected compensation root failure")
            return self._conn.execute(sql, parameters)

    monkeypatch.setattr(db, "_get_conn", lambda: FailingConnection(real_conn()))
    with pytest.raises(
        sqlite3.OperationalError, match="injected compensation root failure",
    ):
        db.fail_run_admission(
            "run-compensation-rollback", expected_session_id=session["id"],
        )
    monkeypatch.setattr(db, "_get_conn", real_conn)

    assert db.get_run("run-compensation-rollback") == before_run
    assert db.get_run_task("task_run-compensation-rollback_root") == before_root


@pytest.mark.parametrize("reason", ["completed", "provider_failed", "cancelled"])
def test_fail_run_admission_rejects_non_admission_stop_reason(admission_db, reason):
    db = admission_db
    session = db.create_session("invalid failure reason")
    _admit(db, run_id=f"run-invalid-{reason}", session_id=session["id"])

    with pytest.raises(ValueError, match="invalid admission failure stop reason"):
        db.fail_run_admission(f"run-invalid-{reason}", stop_reason=reason)
    assert db.get_run(f"run-invalid-{reason}")["state"] == "running"
