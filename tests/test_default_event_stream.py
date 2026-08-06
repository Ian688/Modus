import pytest

from modus.desktop.events import ChannelId, EventType


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


class FakeConfig:
    class Moa:
        enabled = False

    moa = Moa()


class FakeEngine:
    config = FakeConfig()

    async def ask(self, message: str, history=None, *, approval_callback=None, cancel_event=None, budget=None, session_id=None, run_id=None):
        yield {"type": "text_delta", "text": "我会创建文件。"}
        yield {
            "type": "tool_call",
            "name": "write_file",
            "input": {"path": "demo/index.html", "content": "<h1>PVZ</h1>"},
        }
        yield {
            "type": "tool_result",
            "name": "write_file",
            "result": "wrote demo/index.html",
            "is_error": False,
        }
        yield {
            "type": "done",
            "messages": [],
            "total_tokens": 42,
            "total_turns": 2,
        }


class FakeErrorEngine:
    config = FakeConfig()

    async def ask(self, message: str, history=None, *, approval_callback=None, cancel_event=None, budget=None, session_id=None, run_id=None):
        yield {"type": "thinking_delta", "text": "正在检查失败原因。"}
        yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 3}}
        yield {"type": "error", "error": "provider unavailable", "stop_reason": "engine_error"}


class DisconnectingWebSocket:
    """Accept startup packets, then simulate a browser closing mid-run."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send_json(self, _message: dict) -> None:
        from fastapi import WebSocketDisconnect

        self.attempts += 1
        if self.attempts >= 3:
            raise WebSocketDisconnect()


class DisconnectingEngine:
    config = FakeConfig()

    async def ask(self, message: str, history=None, *, approval_callback=None, cancel_event=None, budget=None, session_id=None, run_id=None):
        yield {"type": "text_delta", "text": "正在执行。"}


class PartialEofEngine:
    """A broken adapter that yields content, then omits its terminal packet."""

    config = FakeConfig()

    async def ask(self, message: str, history=None, *, approval_callback=None, cancel_event=None, budget=None, session_id=None, run_id=None):
        yield {"type": "text_delta", "text": "尚未完成的响应"}


class EmptyEofEngine:
    """A broken adapter that closes before yielding any provider packet."""

    config = FakeConfig()

    async def ask(self, message: str, history=None, *, approval_callback=None, cancel_event=None, budget=None, session_id=None, run_id=None):
        if False:  # pragma: no cover - keeps this function an async generator
            yield {}


@pytest.mark.asyncio
async def test_default_stream_emits_ordered_typed_events(monkeypatch):
    from modus.desktop import server

    websocket = FakeWebSocket()
    # This is a pure in-memory runner contract. Persisted Desktop paths use a
    # real session row and are covered separately below.
    session = server.DaoSession(id="session", engine=FakeEngine())
    monkeypatch.setattr(server, "add_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server, "_maybe_compress_history", lambda session, **_kwargs: None,
    )

    await server._stream_to_ws(websocket, session, "create a game")

    assert session.active_controller is None
    typed = [message["event"] for message in websocket.sent if message["type"] == "agent_event"]
    assert typed
    assert [event["sequence"] for event in typed] == list(range(1, len(typed) + 1))
    assert {event["run_id"] for event in typed}.__len__() == 1
    assert {event["channel_id"] for event in typed} == {ChannelId.USER_HOST.value, ChannelId.HOST_MODELS.value}
    assert [event["type"] for event in typed] == [
        EventType.RUN_STARTED.value,
        EventType.USER_MESSAGE.value,
        EventType.HOST_RESPONSE.value,
        EventType.TOOL_CALL.value,
        EventType.TOOL_RESULT.value,
        EventType.RUN_COMPLETED.value,
    ]
    assert typed[3]["payload"]["input"] == {"path": "demo/index.html", "content": "<h1>PVZ</h1>"}

    control_types = [message["type"] for message in websocket.sent if message["type"] != "agent_event"]
    assert control_types == ["done"]
    assert not ({"step", "text_delta", "thinking_delta", "tool_call", "tool_result", "usage", "error", "cancelled"} & set(control_types))


@pytest.mark.asyncio
async def test_default_stream_reports_run_failure_once_through_typed_transcript(monkeypatch):
    from modus.desktop import server

    websocket = FakeWebSocket()
    session = server.DaoSession(id="session-error", engine=FakeErrorEngine())
    monkeypatch.setattr(server, "add_message", lambda *args, **kwargs: None)

    await server._stream_to_ws(websocket, session, "fail cleanly")

    typed = [message["event"] for message in websocket.sent if message["type"] == "agent_event"]
    assert [event["type"] for event in typed] == [
        EventType.RUN_STARTED.value,
        EventType.USER_MESSAGE.value,
        EventType.HOST_THINKING.value,
        EventType.RUN_ERROR.value,
    ]
    assert typed[-1]["payload"]["message"] == "provider unavailable"
    assert typed[-1]["payload"]["stop_reason"] == "engine_error"
    assert [message["type"] for message in websocket.sent if message["type"] != "agent_event"] == ["done"]


@pytest.mark.asyncio
async def test_default_disconnect_cancels_persisted_run_and_root_task_without_audit_hook(monkeypatch):
    """Transport cleanup cannot depend on the event audit reaching SQLite."""
    from modus.desktop import server
    from modus.desktop.db import create_session, get_run, get_run_events, get_run_task

    persisted = create_session("disconnect")
    # Exercise the explicit database fallback rather than the normal audit
    # side-effect that occurs before a WebSocket send.
    monkeypatch.setattr(server, "_audit_event_for", lambda _session: lambda _event: None)
    websocket = DisconnectingWebSocket()
    session = server.DaoSession(
        id="runtime-disconnect", db_id=persisted["id"], engine=DisconnectingEngine(),
    )

    emitter = await server._stream_to_ws(websocket, session, "continue work")

    run = get_run(emitter.run_id)
    root = get_run_task(f"task_{emitter.run_id}_root")
    assert run is not None and run["state"] == "cancelled"
    assert run["stop_reason"] == "cancelled"
    assert run["budget"]["stop_reason"] == "cancelled"
    assert root is not None and root["status"] == "cancelled"
    terminal_events = [
        event for event in get_run_events(emitter.run_id)
        if event["type"] == "run_error"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["status"] == "cancelled"
    assert terminal_events[0]["payload"]["code"] == "transport_disconnected"
    assert session.active_controller is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("engine", "expected_host_responses"),
    [(PartialEofEngine(), 1), (EmptyEofEngine(), 0)],
    ids=["partial-eof", "empty-eof"],
)
async def test_default_provider_eof_without_terminal_durably_fails_run_once(
    engine, expected_host_responses,
):
    from modus.desktop import server
    from modus.desktop.db import (
        create_session, get_messages, get_run, get_run_events, get_run_task,
    )

    persisted = create_session("provider EOF")
    websocket = FakeWebSocket()
    session = server.DaoSession(
        id="runtime-provider-eof", db_id=persisted["id"], engine=engine,
    )

    emitter = await server._stream_to_ws(websocket, session, "keep this prompt")

    run = get_run(emitter.run_id)
    root = get_run_task(f"task_{emitter.run_id}_root")
    events = get_run_events(emitter.run_id)
    terminal_events = [event for event in events if event["type"] == "run_error"]
    wire_terminal_events = [
        packet["event"] for packet in websocket.sent
        if packet["type"] == "agent_event"
        and packet["event"]["type"] == "run_error"
    ]

    assert run is not None and run["state"] == "failed"
    assert run["stop_reason"] == "engine_error"
    assert run["error"] == "模型响应流在返回终态前结束。"
    assert root is not None and root["status"] == "failed"
    assert len(terminal_events) == len(wire_terminal_events) == 1
    assert terminal_events[0]["payload"]["code"] == "provider_stream_ended"
    assert terminal_events[0]["payload"]["retryable"] is True
    assert sum(event["type"] == "host_response" for event in events) == expected_host_responses
    assert [message["content"] for message in get_messages(session.db_id)] == ["keep this prompt"]
    assert [
        packet["stop_reason"] for packet in websocket.sent if packet["type"] == "done"
    ] == ["engine_error"]
    assert session.active_controller is None
