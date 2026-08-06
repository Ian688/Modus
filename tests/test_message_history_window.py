def _set_message_times(db, session_id: str, timestamps: list[float]) -> None:
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        assert len(rows) == len(timestamps)
        for row, timestamp in zip(rows, timestamps, strict=True):
            conn.execute(
                "UPDATE messages SET created_at=? WHERE id=?",
                (timestamp, int(row["id"])),
            )


def test_get_messages_returns_recent_tail_in_chronological_order(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("bounded context")
    for index in range(6):
        db.add_message(session["id"], "user", f"message-{index}")
    _set_message_times(db, session["id"], [10, 20, 30, 40, 50, 60])

    messages = db.get_messages(session["id"], limit=3)

    assert [message["content"] for message in messages] == [
        "message-3", "message-4", "message-5",
    ]


def test_get_messages_uses_id_to_stabilize_equal_timestamps(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("stable context")
    for index in range(5):
        db.add_message(session["id"], "assistant", f"same-time-{index}")
    _set_message_times(db, session["id"], [100] * 5)

    messages = db.get_messages(session["id"], limit=3)

    assert [message["content"] for message in messages] == [
        "same-time-2", "same-time-3", "same-time-4",
    ]
    assert [message["id"] for message in messages] == sorted(
        message["id"] for message in messages
    )


def test_legacy_window_keeps_messages_closest_to_typed_boundary(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("migrated history")
    for index in range(6):
        db.add_message(session["id"], "user", f"legacy-{index}")
    db.add_message(session["id"], "user", "modern-typed-copy")
    _set_message_times(db, session["id"], [100] * 6 + [300])
    db.create_run("run-typed-boundary", session["id"], "default")
    db.upsert_run_event(session["id"], {
        "event_id": "evt-typed-boundary", "run_id": "run-typed-boundary",
        "channel_id": "user_host", "parent_event_id": None,
        "sequence": 1, "timestamp": "now", "mode": "default",
        "actor": {"kind": "user", "id": "user", "label": "用户"},
        "type": "user_message", "status": "completed",
        "payload": {"markdown": "modern-typed-copy"},
    })
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE runs SET started_at=? WHERE run_id=?",
            (200, "run-typed-boundary"),
        )

    messages = db.get_legacy_messages(session["id"], limit=3)

    assert [message["content"] for message in messages] == [
        "legacy-3", "legacy-4", "legacy-5",
    ]
    assert [message["id"] for message in messages] == sorted(
        message["id"] for message in messages
    )
