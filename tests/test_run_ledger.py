import json


def test_projection_revision_tracks_only_committed_ledger_mutations(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("projection revision")
    db.create_run("run-projection", session["id"], "default")
    assert db.get_run("run-projection")["projection_revision"] == 0

    task = db.create_run_task(
        task_id="task-projection", run_id="run-projection",
        session_id=session["id"], ordinal=0, title="Task",
    )
    assert db.get_run("run-projection")["projection_revision"] == 1
    # Idempotent task creation does not mutate the task row.
    db.create_run_task(
        task_id="task-projection", run_id="run-projection",
        session_id=session["id"], ordinal=0, title="Different",
    )
    assert db.get_run("run-projection")["projection_revision"] == 1

    assert db.update_run_task(task["task_id"], status="running") is True
    assert db.get_run("run-projection")["projection_revision"] == 2
    assert db.update_run_task("missing-task", status="running") is False
    assert db.get_run("run-projection")["projection_revision"] == 2

    db.create_artifact_record(
        artifact_id="artifact-projection", run_id="run-projection",
        session_id=session["id"], kind="worker-response", title="Result",
        storage_path="artifact.txt", content_hash="hash", size_bytes=1,
    )
    assert db.get_run("run-projection")["projection_revision"] == 3

    event = {
        "event_id": "event-projection", "run_id": "run-projection",
        "channel_id": "user_host", "sequence": 1, "timestamp": "now",
        "mode": "default", "actor": {"kind": "host", "id": "host"},
        "type": "host_response", "status": "streaming",
        "payload": {"markdown": "new"}, "revision": 2,
    }
    db.upsert_run_event(session["id"], event)
    assert db.get_run("run-projection")["projection_revision"] == 4
    db.upsert_run_event(session["id"], {**event, "revision": 1, "payload": {"markdown": "old"}})
    assert db.get_run("run-projection")["projection_revision"] == 4

    assert db.update_run("run-projection", state="completed", stop_reason="completed") is True
    assert db.get_run("run-projection")["projection_revision"] == 5
    assert db.update_run("run-projection", state="failed", stop_reason="late") is False
    assert db.get_run("run-projection")["projection_revision"] == 5


def test_init_db_migrates_projection_revision_for_existing_runs(monkeypatch, tmp_path):
    import sqlite3

    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                workspace_id TEXT, mode TEXT NOT NULL, state TEXT NOT NULL,
                stop_reason TEXT, config_snapshot TEXT NOT NULL DEFAULT '{}',
                budget TEXT NOT NULL DEFAULT '{}', error TEXT,
                started_at REAL NOT NULL, updated_at REAL NOT NULL, ended_at REAL
            )""",
        )
    db.init_db()
    with db._get_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    assert "projection_revision" in columns


def test_run_ledger_terminal_state_is_irreversible(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("ledger")
    db.create_run("run-1", session["id"], "default")

    assert db.update_run(
        "run-1", state="completed", stop_reason="completed",
        budget={"total_tokens": 12},
    ) is True
    assert db.update_run("run-1", state="failed", stop_reason="late_error") is False
    run = db.get_run("run-1")
    assert run["state"] == "completed"
    assert run["stop_reason"] == "completed"
    assert run["budget"]["total_tokens"] == 12


def test_run_config_snapshot_is_redacted_and_immutable(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("snapshot")
    first = {
        "schema": "modus.run-config.v1", "host_model_id": "model-a",
        "roles": {"host": {"model_id": "model-a", "api_key": "secret-a"}},
    }
    second = {"schema": "modus.run-config.v1", "host_model_id": "model-b"}

    db.create_run("run-snapshot", session["id"], "default", config_snapshot=first)
    db.create_run("run-snapshot", session["id"], "default", config_snapshot=second)

    run = db.get_run("run-snapshot")
    assert run["config_snapshot"]["host_model_id"] == "model-a"
    assert run["config_snapshot"]["roles"]["host"]["api_key"] == "***"


def test_startup_marks_only_nonterminal_runs_interrupted(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("restart")
    db.create_run("active", session["id"], "moa")
    db.create_run("finished", session["id"], "default")
    db.update_run("finished", state="completed", stop_reason="completed")

    assert db.interrupt_nonterminal_runs() == 1
    assert db.get_run("active")["state"] == "interrupted"
    assert db.get_run("active")["stop_reason"] == "process_restart"
    assert db.get_run("finished")["state"] == "completed"


def test_startup_recovery_settles_tasks_and_appends_replayable_terminal_event(
    monkeypatch, tmp_path,
):
    from modus.desktop import db
    from modus.desktop.workbench import build_workbench_snapshot

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("interrupted timeline")
    db.create_run("run-restart", session["id"], "peri")
    root = db.create_run_task(
        task_id="task-root", run_id="run-restart", session_id=session["id"],
        ordinal=-1, task_kind="root", title="用户任务",
    )
    pending = db.create_run_task(
        task_id="task-pending", run_id="run-restart", session_id=session["id"],
        ordinal=0, task_kind="worker", title="尚未启动",
    )
    completed = db.create_run_task(
        task_id="task-completed", run_id="run-restart", session_id=session["id"],
        ordinal=1, task_kind="worker", title="已经完成",
    )
    db.update_run_task(root["task_id"], status="running", increment_attempt=True)
    db.update_run_task(completed["task_id"], status="completed", increment_attempt=True)
    db.upsert_run_event(session["id"], {
        "event_id": "evt-thinking", "run_id": "run-restart",
        "channel_id": "user_host", "parent_event_id": None,
        "sequence": 1, "timestamp": "before-restart", "mode": "peri",
        "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "host_thinking", "status": "streaming",
        "payload": {"text": "still working", "streaming": True},
    })

    assert db.interrupt_nonterminal_runs() == 1
    # Startup may run more than once in tests or embedded applications.  The
    # second pass must not append another terminal event or alter attempts.
    assert db.interrupt_nonterminal_runs() == 0

    run = db.get_run("run-restart")
    assert (run["state"], run["stop_reason"]) == ("interrupted", "process_restart")
    tasks = {task["task_id"]: task for task in db.list_run_tasks("run-restart")}
    assert (tasks[root["task_id"]]["status"], tasks[root["task_id"]]["attempt"]) == (
        "cancelled", 1,
    )
    assert (tasks[pending["task_id"]]["status"], tasks[pending["task_id"]]["attempt"]) == (
        "cancelled", 0,
    )
    assert (
        tasks[completed["task_id"]]["status"], tasks[completed["task_id"]]["attempt"]
    ) == ("completed", 1)

    events = db.get_run_events("run-restart")
    assert [event["type"] for event in events] == ["host_thinking", "run_error"]
    terminal = events[-1]
    assert terminal["sequence"] == 2
    assert terminal["timestamp"].endswith("Z")
    assert terminal["status"] == "cancelled"
    assert terminal["task_id"] == root["task_id"]
    assert terminal["payload"] == {
        "code": "process_restart",
        "message": "Desktop process restarted before the run reached a terminal state",
        "retryable": True,
        "stop_reason": "process_restart",
    }

    snapshot = build_workbench_snapshot(session["id"])
    restored = snapshot["runs"][0]
    assert restored["state"] == "interrupted"
    assert {task["status"] for task in restored["tasks"]} == {"cancelled", "completed"}


def test_approval_ledger_redacts_input_and_is_one_shot(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("approval")
    db.create_run("run-a", session["id"], "default")
    db.create_approval(
        approval_id="approval-a", run_id="run-a", tool_name="web_fetch",
        input_hash="hash", input_data={"api_key": "secret", "url": "https://example.test"},
    )

    assert db.resolve_approval_record("approval-a", "allow") is True
    assert db.resolve_approval_record("approval-a", "deny") is False
    with db._get_conn() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE approval_id='approval-a'").fetchone()
    assert json.loads(row["input"])["api_key"] == "***"
    assert row["decision"] == "allow"
    assert row["resolution_reason"] == "user_decision"


def test_event_ledger_redacts_nested_secrets(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("events")
    db.create_run("run-e", session["id"], "default")
    db.upsert_run_event(session["id"], {
        "event_id": "evt", "run_id": "run-e", "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1, "timestamp": "now", "mode": "default",
        "actor": {"kind": "tool", "id": "tool", "label": "tool"},
        "type": "tool_call", "status": "completed",
        "payload": {"input": {"authorization": "Bearer secret", "api_key": "sk-1234567890abcdef"}},
    })
    payload = db.get_run_events("run-e")[0]["payload"]
    assert payload["input"] == {"authorization": "***", "api_key": "***"}


def test_session_run_history_replays_multiple_runs_in_order(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("history")
    for index in (1, 2):
        run_id = f"run-{index}"
        db.create_run(run_id, session["id"], "default")
        db.upsert_run_event(session["id"], {
            "event_id": f"evt-{index}", "run_id": run_id,
            "channel_id": "user_host", "parent_event_id": None,
            "sequence": 1, "timestamp": f"now-{index}", "mode": "default",
            "actor": {"kind": "user", "id": "user", "label": "user"},
            "type": "user_message", "status": "completed",
            "payload": {"markdown": f"message-{index}"},
        })
        db.update_run(run_id, state="completed", stop_reason="completed")

    history = db.get_session_run_history(session["id"])

    assert [item["run"]["run_id"] for item in history] == ["run-1", "run-2"]
    assert [item["events"][0]["payload"]["markdown"] for item in history] == ["message-1", "message-2"]


def test_startup_denies_pending_approvals_for_interrupted_runs(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("restart approval")
    db.create_run("run-pending", session["id"], "default")
    db.create_approval(
        approval_id="approval-pending", run_id="run-pending", tool_name="bash",
        input_hash="hash", input_data={"command": "echo ok"},
    )

    assert db.interrupt_nonterminal_runs() == 1

    with db._get_conn() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE approval_id='approval-pending'").fetchone()
    assert row["decision"] == "deny"
    assert row["resolution_reason"] == "process_restart"
