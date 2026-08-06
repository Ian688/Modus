from pathlib import Path

import asyncio
import pytest
from fastapi.testclient import TestClient

from _bundle import js_bundle


class EchoEngine:
    def __init__(self, **_kwargs) -> None:
        pass

    async def ask(self, message, history=None, **_kwargs):
        from modus.types import Message

        yield {"type": "text_delta", "text": f"Echo: {message}"}
        yield {
            "type": "done",
            "messages": [
                *(history or []), Message(role="user", content=message),
                Message(role="assistant", content=f"Echo: {message}"),
            ],
            "total_tokens": 3,
            "total_turns": 1, "stop_reason": "completed",
        }


def _patch_engine(monkeypatch, server) -> None:
    async def fake_registry(**_kwargs):
        return object()

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", EchoEngine)


def _receive_until(socket, terminal_type: str, limit: int = 30) -> list[dict]:
    packets = []
    for _ in range(limit):
        packet = socket.receive_json()
        packets.append(packet)
        if packet["type"] == terminal_type:
            return packets
    raise AssertionError(f"did not receive {terminal_type}; got {packets!r}")


def test_connection_is_transient_until_first_stateful_action(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import get_messages, get_session, list_sessions

    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            ready = socket.receive_json()
            assert ready["type"] == "session_ready"
            assert ready["runtime_session_id"] == ready["session_id"]
            assert ready["db_id"] == ""
            assert ready["persisted"] is False
            assert list_sessions() == []

            socket.send_json({"type": "run_message", "content": "persist me"})
            packets = _receive_until(socket, "done")

    persisted = next(packet for packet in packets if packet["type"] == "session_persisted")
    db_id = persisted["db_id"]
    assert db_id
    assert persisted["runtime_session_id"] == ready["runtime_session_id"]
    assert persisted["persisted"] is True
    assert persisted["session"]["id"] == db_id
    assert get_session(db_id) is not None
    assert [row["content"] for row in get_messages(db_id)] == ["persist me", "Echo: persist me"]
    assert [item["id"] for item in list_sessions()] == [db_id]


def test_transient_mode_is_persisted_without_a_fake_database_id(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import get_session, list_sessions

    _patch_engine(monkeypatch, server)
    monkeypatch.setattr(server, "_session_mode_snapshot", lambda mode: {
        "host": {"model_id": "host-model", "temperature": 0.2},
        "reference_1": {"model_id": "reference-model", "temperature": 0.7},
    } if mode == "moa" else {})

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            ready = socket.receive_json()
            assert ready["db_id"] == ""
            socket.send_json({"type": "session_set_mode", "db_id": "", "mode": "moa"})
            updated = socket.receive_json()

    assert updated["type"] == "mode_updated"
    assert updated["db_id"] == ""
    assert updated["mode"] == "moa"
    assert updated["model_id"] == "host-model"
    assert list_sessions() == []

    session = server.DaoSession(
        id="persist-after-close", mode=updated["mode"],
        model_id=updated["model_id"],
        mode_config={
            "host": {"model_id": "host-model", "temperature": 0.2},
            "reference_1": {"model_id": "reference-model", "temperature": 0.7},
        },
    )
    record = server.manager.persist_first(session)
    assert record is not None
    assert get_session(record["id"])["mode"] == "moa"
    assert get_session(record["id"])["model_id"] == "host-model"


def test_resume_session_uses_transcript_ops_for_known_cursor(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import create_run, create_session, update_run, upsert_run_event

    _patch_engine(monkeypatch, server)
    session = create_session("incremental")
    create_run("run-incremental", session["id"], "default")
    for sequence, event_id in ((1, "evt-one"), (2, "evt-two")):
        upsert_run_event(session["id"], {
            "event_id": event_id, "run_id": "run-incremental", "channel_id": "user_host",
            "parent_event_id": None, "sequence": sequence, "timestamp": "now",
            "mode": "default", "actor": {"kind": "host", "id": "primary", "label": "主持人"},
            "type": "host_response", "status": "completed",
            "payload": {"markdown": event_id}, "revision": 0, "part_id": "part-" + event_id,
        })
    update_run("run-incremental", state="completed", stop_reason="completed")

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({
                "type": "resume_session", "db_id": session["id"],
                "cursors": {"run-incremental": 1},
            })
            packets = _receive_until(socket, "session_history_end")

    ops = [packet for packet in packets if packet["type"] == "transcript_ops"]
    assert len(ops) == 1
    assert ops[0]["since_sequence"] == 1
    assert [event["event_id"] for event in ops[0]["events"]] == ["evt-one", "evt-two"]
    assert not [packet for packet in packets if packet["type"] == "transcript_reset"]
    snapshot = next(packet for packet in packets if packet["type"] == "workbench_snapshot")
    assert snapshot["data"]["session_id"] == session["id"]
    assert snapshot["data"]["runs"][0]["run_id"] == "run-incremental"
    assert packets.index(snapshot) < len(packets) - 1


def test_missing_resume_target_returns_correlated_recovery_error(monkeypatch):
    from modus.desktop import server

    _patch_engine(monkeypatch, server)
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            ready = socket.receive_json()
            socket.send_json({
                "type": "resume_session", "db_id": "deleted-session",
                "request_id": "resume-missing",
            })
            error = socket.receive_json()

    assert error["type"] == "error"
    assert error["code"] == "session_not_found"
    assert error["operation"] == "resume_session"
    assert error["request_id"] == "resume-missing"
    assert error["requested_db_id"] == "deleted-session"
    assert error["db_id"] == ""
    assert error["runtime_session_id"] == ready["runtime_session_id"]
    assert error["message"] == "未找到要恢复的会话。"


@pytest.mark.asyncio
async def test_resume_busy_error_echoes_identity_for_bounded_frontend_retry(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import create_session

    _patch_engine(monkeypatch, server)
    record = create_session("busy resume")
    owner = server.DaoSession(id="resume-owner", db_id=record["id"])
    release = asyncio.Event()
    server.manager._sessions[owner.id] = owner
    try:
        assert server.start_session_run(owner, release.wait()) is True
        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as socket:
                ready = socket.receive_json()
                socket.send_json({
                    "type": "resume_session", "db_id": record["id"],
                    "request_id": "resume-busy",
                })
                error = socket.receive_json()

        assert error["type"] == "error"
        assert error["code"] == "session_busy"
        assert error["operation"] == "resume_session"
        assert error["request_id"] == "resume-busy"
        assert error["requested_db_id"] == record["id"]
        assert error["db_id"] == ""
        assert error["runtime_session_id"] == ready["runtime_session_id"]
        assert error["run_owned_by_connection"] is False
    finally:
        release.set()
        if owner.active_run_task is not None:
            await owner.active_run_task
        server.manager._sessions.pop(owner.id, None)


def test_resume_protocol_carries_run_config_snapshot(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import create_run, create_session

    _patch_engine(monkeypatch, server)
    session = create_session("snapshot replay")
    create_run(
        "run-with-snapshot", session["id"], "default",
        config_snapshot={
            "schema": "modus.run-config.v1", "mode": "default",
            "host_model_id": "model-a", "reasoning_effort": "high",
        },
    )

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": session["id"]})
            packets = _receive_until(socket, "session_history_end")

    history_start = next(packet for packet in packets if packet["type"] == "session_history_start")
    transcript = next(packet for packet in packets if packet["type"] == "transcript_reset")
    for packet in (history_start, transcript):
        snapshot = packet["runs"][0]["config_snapshot"]
        assert snapshot["schema"] == "modus.run-config.v1"
        assert snapshot["host_model_id"] == "model-a"


def test_resume_restores_workbench_after_transcript_reset(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import (
        create_run, create_run_task, create_session, update_run,
        update_run_task,
    )

    _patch_engine(monkeypatch, server)
    session = create_session("restored workbench")
    create_run("run-restored", session["id"], "default")
    create_run_task(
        task_id="task-restored", run_id="run-restored",
        session_id=session["id"], ordinal=-1, task_kind="root",
        title="已完成任务",
    )
    update_run_task("task-restored", status="completed")
    update_run("run-restored", state="completed", stop_reason="completed")

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": session["id"]})
            packets = _receive_until(socket, "session_history_end")

    packet_types = [packet["type"] for packet in packets]
    assert packet_types.index("transcript_reset") < packet_types.index("workbench_snapshot")
    assert packet_types[-1] == "session_history_end"
    snapshot = next(
        packet["data"] for packet in packets
        if packet["type"] == "workbench_snapshot"
    )
    assert snapshot["session_id"] == session["id"]
    assert [(run["run_id"], run["state"]) for run in snapshot["runs"]] == [
        ("run-restored", "completed"),
    ]
    assert snapshot["runs"][0]["tasks"][0]["title"] == "已完成任务"
    assert snapshot["runs"][0]["semantic"]["outcome"]["status"] == "succeeded"


def test_replayed_terminal_event_carries_authoritative_workbench_projection():
    from modus.desktop.server import _transcript_event

    terminal = {
        "event_id": "evt-terminal", "run_id": "run-terminal",
        "sequence": 7, "revision": 0, "type": "run_completed",
        "payload": {"stop_reason": "completed"},
    }
    run_projection = {
        "run_id": "run-terminal",
        "semantic": {
            "schema": "modus.semantic-run.v1",
            "outcome": {"status": "succeeded", "recovery_count": 1},
        },
    }

    replayed = _transcript_event(terminal, workbench=run_projection)

    assert replayed["workbench"] is run_projection
    assert replayed["workbench"]["semantic"]["outcome"] == {
        "status": "succeeded", "recovery_count": 1,
    }
    nonterminal = _transcript_event(
        {**terminal, "event_id": "evt-tool", "type": "tool_result"},
        workbench=run_projection,
    )
    assert "workbench" not in nonterminal


def test_legacy_replay_reports_message_count_to_prevent_false_blank_state(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import add_message, create_session

    _patch_engine(monkeypatch, server)
    session = create_session("legacy")
    add_message(session["id"], "user", "legacy prompt")
    add_message(session["id"], "assistant", "legacy response")

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": session["id"]})
            packets = _receive_until(socket, "session_history_end")

    terminal = packets[-1]
    assert terminal["event_count"] == 0
    assert terminal["message_count"] == 2
    start = next(packet for packet in packets if packet["type"] == "session_history_start")
    assert start["legacy_messages"] == [
        {"role": "user", "content": "legacy prompt"},
        {"role": "assistant", "content": "legacy response"},
    ]


def test_mixed_legacy_and_typed_history_keeps_only_the_legacy_prefix(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import (
        add_message, create_run, create_session, update_run, upsert_run_event,
    )

    _patch_engine(monkeypatch, server)
    session = create_session("migrated")
    add_message(session["id"], "user", "old question")
    add_message(session["id"], "assistant", "old answer")
    create_run("run-typed", session["id"], "default")
    for sequence, event_type, role, markdown in (
        (1, "user_message", "user", "new question"),
        (2, "host_response", "host", "new answer"),
    ):
        upsert_run_event(session["id"], {
            "event_id": f"evt-mixed-{sequence}", "run_id": "run-typed",
            "channel_id": "user_host", "parent_event_id": None,
            "sequence": sequence, "timestamp": "now", "mode": "default",
            "actor": {"kind": role, "id": role, "label": role},
            "type": event_type, "status": "completed",
            "payload": {"markdown": markdown},
        })
    # Modern context rows must not be rendered beside their typed equivalents.
    add_message(session["id"], "user", "new question")
    add_message(session["id"], "assistant", "new answer")
    update_run("run-typed", state="completed", stop_reason="completed")

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": session["id"]})
            packets = _receive_until(socket, "session_history_end")

    start = next(packet for packet in packets if packet["type"] == "session_history_start")
    assert start["legacy_messages"] == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    transcript = next(packet for packet in packets if packet["type"] == "transcript_reset")
    assert [event["payload"]["markdown"] for event in transcript["events"]] == [
        "new question", "new answer",
    ]


def test_resume_protocol_uses_server_owned_mode_and_distinct_identities():
    root = Path(__file__).parents[1]
    server = (root / "src/modus/desktop/server.py").read_text()
    page = js_bundle()

    assert '"type": "session_restored"' in server
    assert '"type": "session_persisted"' in server
    assert '"runtime_session_id": session.id' in server
    assert '"db_id": session.db_id' in server
    assert 'case "session_restored":' in page
    assert 'case "session_persisted":' in page
    assert 'currentDbId=msg.db_id || "";' in page
    assert "get_session_run_history" in server
    assert '"type": "transcript_reset"' in server
    assert '"type": "transcript_ops"' in server
    assert '"type": "session_history_start"' in server
    assert '"type": "session_history_end"' in server
    assert '"type": "transcript_reset"' in server
    assert '"type": "transcript_ops"' in server
    assert 'get_run_events_since' in server
    assert 'cursors' in page
    assert 'transcriptCursors' in page
    assert 'renderedSessionId' in page
    assert 'msg.session_id !== renderedSessionId' in page
    assert 'msg.operation === "resume_session"' in page
    assert 'localStorage.removeItem("modus_last_db_id")' in page
    assert "delete transcriptCursorsBySession[requestedDbId]" in page
    assert 'loadSessionMessages("", [], "")' in page
    assert "get_legacy_messages(session_id)" in server
    assert '"legacy_messages": [' in server
    history_start = page[page.index('case "session_history_start":'):page.index('case "session_history_end":')]
    assert "setPendingLegacyMessages(msg.session_id, msg.legacy_messages);" in history_start
    history_end = page[page.index('case "session_history_end":'):page.index('case "session_switched":')]
    assert "renderLegacyMessagePrefix(msg.session_id);" in history_end
    prefix = page[
        page.index("function renderLegacyMessagePrefix"):
        page.index("function applyTranscriptEvent")
    ]
    assert '.msg[data-legacy-prefix="true"]' in prefix
    assert 'node.dataset.legacyPrefix = "true";' in prefix
    assert "ca.insertBefore(fragment, firstTypedNode || null);" in prefix
    loader = page[page.index("function loadSessionMessages"):page.index("// Batch delete helper")]
    assert 'pendingLegacySessionId !== String(sessionId || "")' in loader
    # A global UI preference must never mutate a restored conversation after
    # reconnect. The persisted session is the sole mode authority.
    connect_body = page.split("function modusConnectSocket()", 1)[1].split("function handleMsg", 1)[0]
    assert "modus_current_mode" not in connect_body
