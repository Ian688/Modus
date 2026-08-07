from modus.types import Message


def test_semantic_compression_config_loads_and_env_overrides():
    from modus.config import CompressionConfig, load_config

    config = load_config(env={
        "MODUS_COMPRESSION_SEMANTIC": "true",
        "MODUS_COMPRESSION_SEMANTIC_INPUT_CHARS": "12000",
    })

    assert isinstance(config.features.compression, CompressionConfig)
    assert config.features.compression.semantic is True
    assert config.features.compression.semantic_input_chars == 12000


def test_render_omitted_bounds_and_labels():
    from modus.desktop.summarizer import render_omitted

    rendered = render_omitted([
        Message(role="user", content="request one"),
        Message(role="assistant", content="answer one"),
        Message(role="tool", content="result one", tool_call_id="1"),
    ], max_chars=1_000)

    assert "[USER] request one" in rendered
    assert "[ASSISTANT] answer one" in rendered
    assert "[TOOL] result one" in rendered


def test_render_omitted_truncates_when_over_budget():
    from modus.desktop.summarizer import render_omitted

    rendered = render_omitted([
        Message(role="user", content=f"payload {'a' * 200}"),
        Message(role="assistant", content="later turn"),
    ], max_chars=100)

    assert "later turn" not in rendered
    assert "remaining omitted messages" in rendered


def test_semantic_summary_used_when_enabled_and_key_present(monkeypatch):
    from modus.config import ModusConfig
    from modus.desktop import server
    from modus.desktop.server import DaoSession, _maybe_compress_history

    captured: dict = {}

    async def fake_summarize(**kwargs):
        captured.update(kwargs)
        return "项目改用 pytest 分层记忆，DeepSeek 无 embedding。"

    monkeypatch.setattr("modus.desktop.summarizer.summarize_omitted", fake_summarize)

    config = ModusConfig()
    config.features.compression.trigger_tokens = 1
    config.features.compression.tail_messages = 2
    config.features.compression.semantic = True
    config.features.compression.semantic_input_chars = 9_000
    config.llm.api_key = "secret-key"
    engine = type("Engine", (), {"config": config})()
    session = DaoSession(
        id="session", engine=engine,
        main_history=[Message(role="user", content=f"message {index} " * 10) for index in range(6)],
    )

    result = _maybe_compress_history(session)

    assert captured["api_key"] == "secret-key"
    assert captured["messages"][0].role == "user"
    assert captured["max_input_chars"] == 9_000
    assert result["semantic"] is True
    assert "pytest" in result["summary"]
    assert result["omitted_count"] == 4
    # The summary is still injected as a reference-only system message.
    assert session.main_history[0].role == "system"
    assert "REFERENCE ONLY" in str(session.main_history[0].content)


def test_semantic_summary_falls_back_without_api_key():
    from modus.config import ModusConfig
    from modus.desktop.server import DaoSession, _maybe_compress_history

    config = ModusConfig()
    config.features.compression.trigger_tokens = 1
    config.features.compression.tail_messages = 2
    config.features.compression.semantic = True
    config.llm.api_key = ""
    engine = type("Engine", (), {"config": config})()
    session = DaoSession(
        id="session", engine=engine,
        main_history=[Message(role="user", content=f"message {index} " * 10) for index in range(6)],
    )

    result = _maybe_compress_history(session)

    assert "semantic" not in result
    assert "earlier messages were omitted" in result["summary"]


def test_semantic_summary_falls_back_on_model_failure(monkeypatch):
    from modus.config import ModusConfig
    from modus.desktop.server import DaoSession, _maybe_compress_history

    async def boom(**kwargs):
        raise RuntimeError("summarizer down")

    monkeypatch.setattr("modus.desktop.summarizer.summarize_omitted", boom)

    config = ModusConfig()
    config.features.compression.trigger_tokens = 1
    config.features.compression.tail_messages = 2
    config.features.compression.semantic = True
    config.llm.api_key = "secret-key"
    engine = type("Engine", (), {"config": config})()
    session = DaoSession(
        id="session", engine=engine,
        main_history=[Message(role="user", content=f"message {index} " * 10) for index in range(6)],
    )

    result = _maybe_compress_history(session)

    assert "semantic" not in result
    assert "earlier messages were omitted" in result["summary"]


def test_nested_compression_config_loads_and_environment_overrides_apply():
    from modus.config import CompressionConfig, load_config

    config = load_config(env={
        "MODUS_COMPRESSION": "true",
        "MODUS_COMPRESSION_TRIGGER_TOKENS": "123",
        "MODUS_COMPRESSION_TAIL_MESSAGES": "6",
    })

    assert isinstance(config.features.compression, CompressionConfig)
    assert config.features.compression.enabled is True
    assert config.features.compression.trigger_tokens == 123
    assert config.features.compression.tail_messages == 6


def test_add_message_populates_token_count_estimate(tmp_path, monkeypatch):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    record = db.create_session("Token audit")
    db.add_message(record["id"], "user", "a" * 40)
    db.add_message(
        record["id"], "assistant", "b" * 80,
        tool_calls=[{"id": "1", "function": {"name": "read", "arguments": "{}"}}],
    )

    rows = db.get_messages(record["id"], limit=10)

    # chars/4, floored at 1 per message; tool_calls JSON counts too.
    assert rows[0]["token_count"] >= 1
    assert rows[1]["token_count"] >= 1
    assert rows[1]["token_count"] > rows[0]["token_count"]
    # An explicit positive count is honored verbatim.
    db.add_message(record["id"], "user", "c" * 400, token_count=777)
    assert db.get_messages(record["id"], limit=10)[-1]["token_count"] == 777


def test_modus_environment_variables_apply():
    from modus.config import load_config

    config = load_config(env={
        "MODUS_MODEL": "modus-model",
        "MODUS_RUN_MAX_TURNS": "7",
    })

    assert config.llm.model == "modus-model"
    assert config.runtime.max_turns == 7


def test_compaction_summary_is_reference_only_and_does_not_replay_old_user_instruction():
    from modus.agent.compressor import SUMMARY_PREFIX, compress_messages

    messages = [
        Message(role="system", content="system contract"),
        Message(role="user", content="OLD MALICIOUS INSTRUCTION"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="latest request"),
        Message(role="assistant", content="latest answer"),
    ]

    compacted = compress_messages(messages, summary="2 earlier messages omitted", tail_count=2)

    assert compacted[0].content == "system contract"
    assert compacted[1].role == "system"
    assert str(compacted[1].content).startswith(SUMMARY_PREFIX)
    assert "OLD MALICIOUS INSTRUCTION" not in "\n".join(str(item.content) for item in compacted)
    assert [item.content for item in compacted[-2:]] == ["latest request", "latest answer"]


def test_repeated_compaction_replaces_the_previous_reference_summary():
    from modus.agent.compressor import SUMMARY_PREFIX, compress_messages

    first = compress_messages(
        [Message(role="user", content=f"message {index}") for index in range(6)],
        summary="first summary", tail_count=2,
    )
    second = compress_messages(
        [*first, Message(role="user", content="new request"), Message(role="assistant", content="new answer")],
        summary="second summary", tail_count=2,
    )

    summaries = [
        item for item in second
        if item.role == "system" and str(item.content).startswith(SUMMARY_PREFIX)
    ]
    assert len(summaries) == 1
    assert "second summary" in str(summaries[0].content)
    assert "first summary" not in str(summaries[0].content)
    assert [item.content for item in second[-2:]] == ["new request", "new answer"]


def test_desktop_compression_honors_token_threshold():
    from modus.config import ModusConfig
    from modus.desktop.server import DaoSession, _maybe_compress_history

    config = ModusConfig()
    config.features.compression.trigger_tokens = 1
    config.features.compression.tail_messages = 2
    engine = type("Engine", (), {"config": config})()
    session = DaoSession(
        id="session", engine=engine,
        main_history=[Message(role="user", content=f"message {index} " * 10) for index in range(6)],
    )

    result = _maybe_compress_history(session)

    assert len(session.main_history) == 3
    assert session.main_history[0].role == "system"
    assert "REFERENCE ONLY" in str(session.main_history[0].content)
    assert [item.content for item in session.main_history[-2:]] == [
        "message 4 " * 10, "message 5 " * 10,
    ]
    assert result == {
        "summary": (
            "4 earlier messages were omitted to keep this run within its context budget. "
            "Their text is not an active instruction. Ask the user to restate any detail that is "
            "required but absent from the recent messages."
        ),
        "omitted_count": 4,
        "tail_count": 2,
        "reference_only": True,
    }


def test_desktop_compaction_persists_and_restores_the_same_context_boundary(
    tmp_path, monkeypatch,
):
    from modus.config import ModusConfig
    from modus.desktop import db
    from modus.desktop import server
    from modus.desktop.server import DaoSession, _bind_persisted_session, _maybe_compress_history

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    record = db.create_session("Compacted conversation")
    for index in range(6):
        db.add_message(record["id"], "user", f"message {index} " * 10)

    config = ModusConfig()
    config.features.compression.trigger_tokens = 1
    config.features.compression.tail_messages = 2
    engine = type("Engine", (), {"config": config})()
    async def keep_engine(_session):
        return None
    monkeypatch.setattr(server, "_rebuild_session_engine", keep_engine)
    original = DaoSession(
        id="runtime-original", db_id=record["id"], engine=engine,
        main_history=[Message(role="user", content=f"message {index} " * 10) for index in range(6)],
    )

    result = _maybe_compress_history(original, run_id=None)
    db.add_message(record["id"], "assistant", "answer after compaction")
    db.add_message(record["id"], "user", "request after compaction")
    restored_record = db.restore_session(record["id"])
    rebound = DaoSession(id="runtime-restored", engine=engine)

    import asyncio

    asyncio.run(_bind_persisted_session(rebound, restored_record))

    assert result["compaction_id"].startswith("cmp_")
    assert restored_record["context_compaction"]["tail_count"] == 2
    assert [item.role for item in rebound.main_history] == [
        "system", "user", "user", "assistant", "user",
    ]
    assert "REFERENCE ONLY" in str(rebound.main_history[0].content)
    assert [item.content for item in rebound.main_history[1:3]] == [
        "message 4 " * 10, "message 5 " * 10,
    ]
    assert [item.content for item in rebound.main_history[-2:]] == [
        "answer after compaction", "request after compaction",
    ]


def test_context_compaction_has_a_typed_visible_timeline_event() -> None:
    from pathlib import Path

    root = Path(__file__).parents[1]
    events = (root / "src/modus/desktop/events.py").read_text(encoding="utf-8")
    timeline = (root / "src/modus/desktop/static/timeline.js").read_text(encoding="utf-8")

    assert 'CONTEXT_COMPACTED = "context_compacted"' in events
    assert 'case "context_compacted":' in timeline
    assert "完整会话记录仍保留在历史中" in timeline


def test_context_compaction_migrates_an_early_table_without_cutoff(
    tmp_path, monkeypatch,
) -> None:
    import sqlite3

    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE context_compactions (
                compaction_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                run_id TEXT,
                summary TEXT NOT NULL,
                omitted_count INTEGER NOT NULL,
                tail_count INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
        """)

    db.init_db()

    with sqlite3.connect(db.DB_PATH) as conn:
        columns = {
            str(row[1]) for row in conn.execute(
                "PRAGMA table_info(context_compactions)",
            ).fetchall()
        }
    assert "cutoff_message_id" in columns


def test_latest_context_compaction_uses_insertion_order_for_equal_timestamps(
    tmp_path, monkeypatch,
) -> None:
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    record = db.create_session("Compaction order")
    with db._get_conn() as conn:
        for compaction_id, summary, cutoff in (
            ("cmp_z_first", "first", 2),
            ("cmp_a_second", "second", 4),
        ):
            conn.execute(
                """INSERT INTO context_compactions
                   (compaction_id, session_id, run_id, summary, omitted_count,
                    tail_count, cutoff_message_id, created_at)
                   VALUES (?, ?, NULL, ?, 2, 2, ?, 123.0)""",
                (compaction_id, record["id"], summary, cutoff),
            )

    latest = db.get_latest_context_compaction(record["id"])

    assert latest["compaction_id"] == "cmp_a_second"
    assert latest["summary"] == "second"
    assert "rowid" not in latest


def test_compaction_restore_preserves_an_original_system_contract(
    tmp_path, monkeypatch,
) -> None:
    import asyncio

    from modus.config import ModusConfig
    from modus.desktop import db, server
    from modus.desktop.server import DaoSession, _bind_persisted_session, _maybe_compress_history

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    record = db.create_session("System contract")
    messages = [
        Message(role="system", content="durable system contract"),
        *[
            Message(role="user", content=f"message {index} " * 10)
            for index in range(5)
        ],
    ]
    for item in messages:
        db.add_message(record["id"], item.role, str(item.content))
    config = ModusConfig()
    config.features.compression.trigger_tokens = 1
    config.features.compression.tail_messages = 2
    engine = type("Engine", (), {"config": config})()
    original = DaoSession(
        id="runtime-original", db_id=record["id"], engine=engine,
        main_history=list(messages),
    )
    async def keep_engine(_session):
        return None
    monkeypatch.setattr(server, "_rebuild_session_engine", keep_engine)

    _maybe_compress_history(original)
    rebound = DaoSession(id="runtime-restored", engine=engine)
    asyncio.run(_bind_persisted_session(rebound, db.restore_session(record["id"])))

    assert [item.role for item in rebound.main_history] == [
        "system", "system", "user", "user",
    ]
    assert rebound.main_history[0].content == "durable system contract"
    assert "REFERENCE ONLY" in str(rebound.main_history[1].content)


def test_new_compaction_with_no_persisted_omission_uses_zero_boundary(
    tmp_path, monkeypatch,
) -> None:
    from modus.config import ModusConfig
    from modus.desktop import db
    from modus.desktop.server import DaoSession, _maybe_compress_history

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    record = db.create_session("Transient-only context")
    config = ModusConfig()
    config.features.compression.trigger_tokens = 1
    config.features.compression.tail_messages = 2
    engine = type("Engine", (), {"config": config})()
    session = DaoSession(
        id="runtime", db_id=record["id"], engine=engine,
        main_history=[
            Message(role="user", content=f"transient {index} " * 10)
            for index in range(6)
        ],
    )

    result = _maybe_compress_history(session)

    assert result["cutoff_message_id"] == 0
    assert db.restore_session(record["id"])["context_compaction"]["cutoff_message_id"] == 0


def test_repeated_desktop_compaction_uses_one_durable_id_ordered_boundary(
    tmp_path, monkeypatch,
) -> None:
    from modus.config import ModusConfig
    from modus.desktop import db
    from modus.desktop.server import DaoSession, _maybe_compress_history

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    record = db.create_session("Repeated compaction")
    config = ModusConfig()
    config.features.compression.trigger_tokens = 1
    config.features.compression.tail_messages = 2
    engine = type("Engine", (), {"config": config})()
    session = DaoSession(id="runtime", db_id=record["id"], engine=engine)
    for index in range(6):
        content = f"message {index} " * 10
        db.add_message(record["id"], "user", content)
        session.main_history.append(Message(role="user", content=content))
    first = _maybe_compress_history(session)
    for index in range(6, 8):
        content = f"message {index} " * 10
        db.add_message(record["id"], "user", content)
        session.main_history.append(Message(role="user", content=content))

    second = _maybe_compress_history(session)

    assert (first["omitted_count"], first["cutoff_message_id"]) == (4, 4)
    assert (second["omitted_count"], second["cutoff_message_id"]) == (6, 6)
    assert db.restore_session(record["id"])["context_compaction"]["omitted_count"] == 6


def test_compaction_restore_reads_all_rows_after_cutoff_beyond_default_window(
    tmp_path, monkeypatch,
) -> None:
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    record = db.create_session("Large active tail")
    db.add_message(record["id"], "system", "original contract")
    for index in range(3):
        db.add_message(record["id"], "user", f"old-{index}")
    cutoff = db.get_messages(record["id"], limit=1)[0]["id"]
    db.create_context_compaction(
        session_id=record["id"], summary="older rows omitted",
        omitted_count=4, tail_count=2, cutoff_message_id=cutoff,
    )
    for index in range(205):
        db.add_message(record["id"], "assistant", f"active-{index}")

    restored = db.restore_session(record["id"])

    assert len(restored["context_messages"]) == 206
    assert restored["context_messages"][0]["content"] == "original contract"
    assert restored["context_messages"][1]["content"] == "active-0"
    assert restored["context_messages"][-1]["content"] == "active-204"
    assert len(restored["messages"]) == 200


def test_compaction_boundary_and_restore_ignore_created_at_clock_reversal(
    tmp_path, monkeypatch,
) -> None:
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    record = db.create_session("Clock reversal")
    for index in range(5):
        db.add_message(record["id"], "user", f"message-{index}")
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id",
            (record["id"],),
        ).fetchall()
        for row, timestamp in zip(rows, [500, 400, 300, 200, 100], strict=True):
            conn.execute(
                "UPDATE messages SET created_at=? WHERE id=?",
                (timestamp, int(row["id"])),
            )

    boundary = db.get_context_compaction_boundary(record["id"], 2)
    db.create_context_compaction(
        session_id=record["id"], summary="older rows omitted",
        tail_count=2, **boundary,
    )
    restored = db.restore_session(record["id"])

    assert boundary == {"omitted_count": 3, "cutoff_message_id": int(rows[2]["id"])}
    assert [item["content"] for item in restored["context_messages"]] == [
        "message-3", "message-4",
    ]


def test_restored_context_recompacts_only_when_over_window(
    tmp_path, monkeypatch,
) -> None:
    import asyncio

    from modus.config import ModusConfig
    from modus.desktop import db, server
    from modus.desktop.server import DaoSession, _bind_persisted_session

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    record = db.create_session("Over-window restore")
    db.add_message(record["id"], "system", "durable contract")
    # A long tail (well over the tiny window below, but a normal trigger
    # threshold would not fire at runtime).
    for index in range(6):
        db.add_message(record["id"], "user", f"message {index} " * 200)
    db.create_context_compaction(
        session_id=record["id"], summary="older rows omitted",
        omitted_count=6, tail_count=2, cutoff_message_id=0,
    )
    for index in range(6):
        db.add_message(record["id"], "assistant", f"post-compaction {index} " * 100)

    config = ModusConfig()
    config.llm.max_context_window = 800  # tiny window forces the re-compact path
    config.llm.max_tokens = 20
    config.features.compression.tail_messages = 2
    engine = type("Engine", (), {"config": config})()

    async def keep_engine(_session):
        return None

    monkeypatch.setattr(server, "_rebuild_session_engine", keep_engine)
    rebound = DaoSession(id="runtime-restored", engine=engine)
    asyncio.run(_bind_persisted_session(rebound, db.restore_session(record["id"])))

    # Deterministic re-compaction kept the summary and bounded the tail.
    assert rebound.main_history[0].role == "system"
    assert "durable contract" in str(rebound.main_history[0].content)
    summaries = [
        m for m in rebound.main_history
        if m.role == "system" and "REFERENCE ONLY" in str(m.content)
    ]
    assert len(summaries) == 1
    # contract + summary + recent tail; a retained user instruction may add one.
    assert len(rebound.main_history) <= 5


def test_compress_keeps_final_user_instruction():
    """Compaction must never drop the last user request."""
    from modus.agent.compressor import compress_messages

    messages = [
        Message(role="system", content="contract"),
        Message(role="user", content="原始请求"),
        Message(role="assistant", content="old reply"),
        Message(role="user", content="活动请求"),
        Message(role="assistant", content="active reply"),
        Message(role="user", content="最终指令"),
        Message(role="assistant", content="final"),
    ]
    out = compress_messages(messages, summary="s", tail_count=2)

    contents = [str(m.content) for m in out]
    assert "最终指令" in contents, "final user instruction must survive compaction"
    assert "原始请求" not in contents  # old request is compacted away


def test_compress_turn_aligns_tail_no_orphan_tool():
    """Tail must never start on a tool message whose assistant was compacted."""
    from modus.agent.compressor import compress_messages

    messages = [
        Message(role="system", content="contract"),
        Message(role="user", content="request"),
        Message(role="assistant", content="turn1", tool_calls=[
            {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
        ]),
        Message(role="tool", content="r1", tool_call_id="c1"),
        Message(role="assistant", content="turn2", tool_calls=[
            {"id": "c2", "function": {"name": "bash", "arguments": "{}"}},
        ]),
        Message(role="tool", content="r2", tool_call_id="c2"),
        Message(role="assistant", content="turn3", tool_calls=[
            {"id": "c3", "function": {"name": "bash", "arguments": "{}"}},
        ]),
        Message(role="tool", content="r3", tool_call_id="c3"),
    ]
    out = compress_messages(messages, summary="s", tail_count=3)

    # Every tool message in the tail must have its assistant tool_call present.
    tool_ids = {m.tool_call_id for m in out if m.role == "tool"}
    assistant_ids = {
        str(tc["id"]) for m in out if m.role == "assistant" for tc in (m.tool_calls or [])
    }
    assert tool_ids <= assistant_ids, f"orphaned tool ids: {tool_ids - assistant_ids}"
