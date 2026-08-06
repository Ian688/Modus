import json

import pytest


def test_run_event_audit_upserts_streaming_event_by_stable_event_id(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("audit")
    event = {
        "event_id": "evt-stream", "run_id": "run-1", "channel_id": "user_host",
        "parent_event_id": None, "sequence": 2, "timestamp": "2026-07-29T10:00:00Z",
        "mode": "default", "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "host_response", "status": "streaming", "payload": {"markdown": "first"},
    }
    db.upsert_run_event(session["id"], event)
    event["status"] = "completed"
    event["payload"] = {"markdown": "first second"}
    db.upsert_run_event(session["id"], event)

    rows = db.get_run_events("run-1")
    assert len(rows) == 1
    assert rows[0]["event_id"] == "evt-stream"
    assert rows[0]["status"] == "completed"
    assert rows[0]["payload"] == {"markdown": "first second"}
    assert rows[0]["actor"] == {"kind": "host", "id": "primary", "label": "主持人"}


def test_run_event_audit_does_not_regress_streaming_revision(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("revision")
    base = {
        "event_id": "evt-revision", "run_id": "run-revision", "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1, "timestamp": "now",
        "mode": "default", "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "host_response", "status": "streaming", "payload": {"markdown": "new"},
        "revision": 2,
    }
    db.create_run("run-revision", session["id"], "default")
    db.upsert_run_event(session["id"], base)
    stale = {**base, "status": "completed", "payload": {"markdown": "old"}, "revision": 1}
    db.upsert_run_event(session["id"], stale)

    rows = db.get_run_events("run-revision")
    assert rows[0]["revision"] == 2
    assert rows[0]["payload"] == {"markdown": "new"}


def test_run_event_cursor_replays_inclusive_streaming_boundary(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("cursor")
    db.create_run("run-cursor", session["id"], "default")
    for sequence, event_id in ((1, "evt-one"), (2, "evt-two")):
        db.upsert_run_event(session["id"], {
            "event_id": event_id, "run_id": "run-cursor", "channel_id": "user_host",
            "parent_event_id": None, "sequence": sequence, "timestamp": "now",
            "mode": "default", "actor": {"kind": "host", "id": "primary", "label": "主持人"},
            "type": "host_response", "status": "completed", "payload": {"markdown": event_id},
        })

    rows = db.get_run_events_since("run-cursor", 2)
    assert [row["event_id"] for row in rows] == ["evt-two"]
    assert db.get_run_event_cursor("run-cursor") == 2


def test_nonterminal_event_after_run_settlement_is_ignored(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("late event boundary")
    run_id = "run-late-nonterminal"
    root_id = f"task_{run_id}_root"
    db.create_run(run_id, session["id"], "default")
    db.create_run_task(
        task_id=root_id, run_id=run_id, session_id=session["id"], ordinal=-1,
        task_kind="root", title="Root",
    )
    db.update_run_task(root_id, status="running")
    terminal = {
        "event_id": "evt-terminal", "run_id": run_id,
        "task_id": root_id, "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1,
        "timestamp": "2026-08-03T00:00:00Z", "mode": "default",
        "actor": {"kind": "system", "id": "system", "label": "system"},
        "type": "run_completed", "status": "completed",
        "payload": {"stop_reason": "completed", "budget": {}},
    }
    assert db.settle_run_event(session["id"], terminal) is True

    db.upsert_run_event(session["id"], {
        "event_id": "evt-late-tool", "run_id": run_id,
        "task_id": root_id, "channel_id": "user_host",
        "parent_event_id": None, "sequence": 2, "timestamp": "later",
        "mode": "default",
        "actor": {"kind": "tool", "id": "bash", "label": "bash"},
        "type": "tool_result", "status": "completed",
        "payload": {"output": "late side effect"},
    })

    assert [event["event_id"] for event in db.get_run_events(run_id)] == [
        "evt-terminal",
    ]


def test_run_event_cannot_cross_session_identity(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    owner = db.create_session("event owner")
    other = db.create_session("other session")
    run_id = "run-session-bound-event"
    db.create_run(run_id, owner["id"], "default")

    db.upsert_run_event(other["id"], {
        "event_id": "evt-cross-session", "run_id": run_id,
        "channel_id": "user_host", "parent_event_id": None,
        "sequence": 1, "timestamp": "now", "mode": "default",
        "actor": {"kind": "host", "id": "primary", "label": "host"},
        "type": "host_response", "status": "completed",
        "payload": {"markdown": "wrong conversation"},
    })

    assert db.get_run_events(run_id) == []


@pytest.mark.asyncio
async def test_emitter_audit_callback_persists_streaming_event_as_one_final_row(monkeypatch, tmp_path):
    from modus.desktop import db
    from modus.desktop.events import Actor, ChannelId, EventStatus, EventType, RunEventEmitter

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("emitter audit")
    sent: list[dict] = []

    async def send_json(packet: dict) -> None:
        sent.append(packet)

    emitter = RunEventEmitter(
        run_id="run-emitter", mode="default", send_json=send_json,
        audit_event=lambda event: db.upsert_run_event(session["id"], event),
    )
    first = await emitter.emit(
        EventType.HOST_RESPONSE, ChannelId.USER_HOST, Actor.host("primary"),
        {"markdown": "first"}, status=EventStatus.STREAMING,
    )
    await emitter.emit(
        EventType.HOST_RESPONSE, ChannelId.USER_HOST, Actor.host("primary"),
        {"markdown": " second"}, status=EventStatus.COMPLETED, event_id=first.event_id,
    )

    rows = db.get_run_events("run-emitter")
    assert len(rows) == 1
    assert rows[0]["sequence"] == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["payload"] == {"markdown": "first second"}
    assert len(sent) == 2
