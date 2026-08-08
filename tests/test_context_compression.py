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


# ─── Wave2 C2: file-operation manifest + re-inject + blacklist ─────────────

def test_extract_file_operations_collects_and_dedups_paths():
    from modus.agent.compressor import extract_file_operations

    messages = [
        Message(role="assistant", content="", tool_calls=[
            {"id": "1", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
        ]),
        Message(role="assistant", content="", tool_calls=[
            {"id": "2", "function": {"name": "edit_file", "arguments": '{"path": "a.py"}'}},
            {"id": "3", "function": {"name": "write_file", "arguments": '{"path": "b.py"}'}},
        ]),
        # Repeated read of the same path is deduped; file_path alias normalized.
        Message(role="assistant", content="", tool_calls=[
            {"id": "4", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
            {"id": "5", "function": {"name": "read_file", "arguments": '{"file_path": "c.py"}'}},
        ]),
    ]

    ops = extract_file_operations(messages)

    assert ops == {"read": ["a.py", "c.py"], "modified": ["a.py", "b.py"]}


def test_extract_file_operations_handles_parsed_arguments_and_ignores_others():
    from modus.agent.compressor import extract_file_operations

    messages = [
        Message(role="assistant", content="", tool_calls=[
            # Pre-parsed dict arguments (no JSON string).
            {"id": "1", "function": {"name": "edit_file", "arguments": {"path": "x.py"}}},
            # Non-file tool is ignored.
            {"id": "2", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}},
            # Malformed JSON is ignored, not raised.
            {"id": "3", "function": {"name": "read_file", "arguments": "{oops"}},
        ]),
    ]

    ops = extract_file_operations(messages)

    assert ops == {"read": [], "modified": ["x.py"]}


def test_render_file_manifest():
    from modus.agent.compressor import render_file_manifest

    manifest = render_file_manifest({"read": ["a.py"], "modified": ["a.py", "b.py"]})

    assert "[FILES READ THIS TURN] a.py" in manifest
    assert "[FILES MODIFIED THIS TURN] a.py, b.py" in manifest


def test_compress_appends_file_manifest():
    from modus.agent.compressor import SUMMARY_PREFIX, compress_messages

    messages = [
        Message(role="system", content="contract"),
        Message(role="user", content="old request"),
        Message(role="assistant", content="turn1", tool_calls=[
            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "src/a.py"}'}},
        ]),
        Message(role="tool", content="contents", tool_call_id="c1"),
        Message(role="assistant", content="turn2", tool_calls=[
            {"id": "c2", "function": {"name": "edit_file", "arguments": '{"path": "src/a.py"}'}},
        ]),
        Message(role="tool", content="Edited src/a.py", tool_call_id="c2"),
        Message(role="user", content="latest request"),
    ]

    out = compress_messages(messages, summary="older turns condensed", tail_count=2)

    summary = [m for m in out if m.role == "system" and str(m.content).startswith(SUMMARY_PREFIX)][0]
    content = str(summary.content)
    assert "[FILES READ THIS TURN] src/a.py" in content
    assert "[FILES MODIFIED THIS TURN] src/a.py" in content
    # The old tool-call turns are gone from the active context.
    assert "turn1" not in "\n".join(str(m.content) for m in out)


def test_approval_messages_not_compacted():
    from modus.agent.compressor import SUMMARY_PREFIX, compress_messages

    messages = [
        Message(role="system", content="contract"),
        Message(role="user", content="request one"),
        Message(role="assistant", content="turn1", tool_calls=[
            {"id": "c1", "function": {"name": "write_file", "arguments": '{"path": "x.py"}'}},
        ]),
        # The approval decision is protected context and must survive.
        Message(role="tool", content='Tool "write_file" denied by approval policy.', tool_call_id="c1"),
        Message(role="assistant", content="turn2"),
        Message(role="user", content="request two"),
        Message(role="assistant", content="turn3"),
        Message(role="user", content="FINAL REQUEST"),
    ]

    out = compress_messages(messages, summary="s", tail_count=2)

    all_text = "\n".join(str(m.content) for m in out)
    assert "denied by approval policy" in all_text, "approval decision must survive compaction"
    assert "FINAL REQUEST" in all_text


def test_goal_and_task_messages_not_compacted():
    from modus.agent.compressor import compress_messages

    messages = [
        Message(role="system", content="contract"),
        Message(role="user", content="old request"),
        # Goal/task context must survive compaction.
        Message(role="user", content="总目标：重构缓存层"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="latest request"),
        Message(role="assistant", content="latest answer"),
    ]

    out = compress_messages(messages, summary="s", tail_count=2)

    all_text = "\n".join(str(m.content) for m in out)
    assert "总目标：重构缓存层" in all_text, "goal context must survive compaction"


def test_should_compress_skips_when_only_protected_content_is_large():
    from modus.agent.compressor import should_compress

    messages = [
        Message(role="user", content=f'Goal: {"a" * 400}'),
        Message(role="assistant", content="answer"),
    ]
    # Total size exceeds the threshold, but the only large message is protected.
    assert should_compress(messages, threshold=100) is False
    # Removing the goal marker makes the same size compressing again.
    plain = [Message(role="user", content="a" * 400), Message(role="assistant", content="answer")]
    assert should_compress(plain, threshold=100) is True


def test_compress_reinjects_protected_tool_pair_outside_tail():
    from modus.agent.compressor import SUMMARY_PREFIX, compress_messages

    messages = [
        Message(role="system", content="contract"),
        Message(role="user", content="request one"),
        Message(role="assistant", content="turn1", tool_calls=[
            {"id": "c1", "function": {"name": "write_file", "arguments": '{"path": "x.py"}'}},
        ]),
        Message(role="tool", content='Tool "write_file" requires approval, but no approval callback.', tool_call_id="c1"),
        Message(role="assistant", content="turn2"),
        Message(role="user", content="request two"),
        Message(role="assistant", content="turn3"),
        Message(role="user", content="FINAL REQUEST"),
    ]

    out = compress_messages(messages, summary="s", tail_count=2)

    # The protected tool result AND its owning assistant tool_call both survive.
    all_text = "\n".join(str(m.content) for m in out)
    assert "requires approval" in all_text
    assistant_msgs = [m for m in out if m.role == "assistant"]
    assert any("write_file" in str(tc) for m in assistant_msgs for tc in (m.tool_calls or []))


def test_summarizer_appends_file_operations_to_semantic_summary():
    from modus.agent.compressor import SUMMARY_PREFIX, compress_messages

    messages = [
        Message(role="system", content="contract"),
        Message(role="user", content="old request"),
        Message(role="assistant", content="turn1", tool_calls=[
            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "src/x.py"}'}},
        ]),
        Message(role="tool", content="read x.py", tool_call_id="c1"),
        Message(role="user", content="latest request"),
    ]
    # A semantic summary flows through the same manifest append.
    out = compress_messages(messages, summary="语义摘要已生成", tail_count=2)

    summary = [m for m in out if m.role == "system" and str(m.content).startswith(SUMMARY_PREFIX)][0]
    assert "语义摘要已生成" in str(summary.content)
    assert "[FILES READ THIS TURN] src/x.py" in str(summary.content)


def test_summarize_omitted_prompt_asks_for_file_list(monkeypatch):
    from modus.desktop import summarizer

    assert "Read:" in summarizer._SUMMARIZE_SYSTEM
    assert "Modified:" in summarizer._SUMMARIZE_SYSTEM


def test_summarizer_file_operations_text_extracts_manifest():
    from modus.desktop.summarizer import _file_operations_text

    text = _file_operations_text([
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "src/a.py"}'}},
            {"id": "c2", "function": {"name": "edit_file", "arguments": '{"path": "src/a.py"}'}},
        ]),
    ])

    assert "[FILES READ/MODIFIED THIS TURN]" in text
    assert "src/a.py" in text
    # No tool messages → no manifest.
    assert _file_operations_text([Message(role="user", content="hi")]) == ""


def test_run_file_operations_weight_paths_in_recall(tmp_path, monkeypatch):
    """Recent-run file operations are extracted and weighted in recall scoring."""
    from modus.desktop import db, memory

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    sid = db.create_session("mem-ops")["id"]
    # Run A read/edited src/cache.py (path matches the query).
    run_a = db.create_run("run-a", sid, "default")
    db.upsert_run_event(sid, {
        "event_id": "evt-a1", "run_id": run_a["run_id"], "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1, "timestamp": "now",
        "mode": "default", "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "tool_call", "status": "completed",
        "payload": {"tool_call_id": "c1", "name": "edit_file", "input": {"path": "src/cache.py"}},
        "revision": 0, "part_id": "p1",
    })
    db.update_run(run_a["run_id"], state="completed", stop_reason="completed")
    # Run B touched a different file.
    run_b = db.create_run("run-b", sid, "default")
    db.upsert_run_event(sid, {
        "event_id": "evt-b1", "run_id": run_b["run_id"], "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1, "timestamp": "now",
        "mode": "default", "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "tool_call", "status": "completed",
        "payload": {"tool_call_id": "c2", "name": "write_file", "input": {"path": "src/other.py"}},
        "revision": 0, "part_id": "p1",
    })
    db.update_run(run_b["run_id"], state="completed", stop_reason="completed")

    # A query naming the touched path surfaces the file-touching run first.
    hits = memory.search_run_history(sid, "继续 src/cache.py")
    assert hits and hits[0]["run_id"] == run_a["run_id"]
    # The path token is present in the file-ops weighting.
    assert memory._run_file_operations(db.get_run_events(run_a["run_id"])) == ["src/cache.py"]


def test_reinject_recent_files_bounded():
    """The file manifest is tiny and bounded regardless of how many turns had
    file operations."""
    from modus.agent.compressor import compress_messages, render_file_manifest

    messages = [
        Message(role="system", content="contract"),
        Message(role="user", content="old request"),
        *[
            Message(role="assistant", content=f"turn {i}", tool_calls=[
                {"id": f"c{i}", "function": {"name": "read_file", "arguments": f'{{"path": "f{i}.py"}}'}},
            ])
            for i in range(20)
        ],
        Message(role="user", content="latest request"),
    ]

    out = compress_messages(messages, summary="s", tail_count=2)

    summary = [m for m in out if m.role == "system" and "REFERENCE ONLY" in str(m.content)][0]
    manifest_text = str(summary.content)
    # Every unique read path is listed, deduped, comma-separated — bounded.
    assert "[FILES READ THIS TURN] f0.py" in manifest_text
    assert len(manifest_text) < 2000

