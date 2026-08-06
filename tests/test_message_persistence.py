import pytest

from modus.types import Message


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, packet):
        self.sent.append(packet)


class IncrementalEngine:
    def __init__(self, complete_history):
        self.complete_history = complete_history

    async def ask(self, message, history=None, **kwargs):
        yield {"type": "done", "messages": self.complete_history, "total_tokens": 2, "total_turns": 1, "stop_reason": "completed"}


@pytest.mark.asyncio
async def test_default_agent_persists_only_messages_added_by_current_turn(monkeypatch):
    from modus.desktop import server
    from modus.desktop import default_runner

    previous = [Message(role="user", content="old"), Message(role="assistant", content="old answer")]
    complete = previous + [
        Message(role="user", content="new"),
        Message(role="assistant", content="new answer"),
    ]
    persisted = []
    monkeypatch.setattr(default_runner, "add_message", lambda *args, **kwargs: persisted.append((args, kwargs)))
    monkeypatch.setattr(default_runner, "update_session", lambda *args, **kwargs: None)
    session = server.DaoSession(
        id="s", db_id="db", main_history=list(previous), engine=IncrementalEngine(complete),
        worldview="existing",
    )

    await server._stream_to_ws(FakeWebSocket(), session, "new")

    assert [(args[1], args[2]) for args, _ in persisted] == [
        ("user", "new"), ("assistant", "new answer"),
    ]


def test_tool_call_id_is_persisted_and_paired_on_restore():
    """A tool result must carry the id of the assistant tool_call it answers.

    Regression for the OpenAI-compatible HTTP 400 "assistant message with
    tool_calls must be followed by tool messages responding to each
    tool_call_id".  Rows predating the tool_call_id column are backfilled
    positionally at restore time; new rows are persisted with the id.
    """
    from modus.desktop import server

    calls = [
        {"id": "call_00_aaa", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        {"id": "call_01_bbb", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
    ]
    # Legacy restore: tool rows with no persisted id must be paired to the
    # assistant turn that produced them, in order.
    history = [
        Message(role="assistant", content="", tool_calls=calls),
        Message(role="tool", content="a", tool_call_id=None),
        Message(role="tool", content="b", tool_call_id=None),
    ]
    server._pair_tool_call_ids(history)
    assert history[1].tool_call_id == "call_00_aaa"
    assert history[2].tool_call_id == "call_01_bbb"
    # A row that already has an id is never clobbered.
    history = [
        Message(role="assistant", content="", tool_calls=[{"id": "call_00_aaa", "type": "function", "function": {}}]),
        Message(role="tool", content="a", tool_call_id="call_00_existing"),
    ]
    server._pair_tool_call_ids(history)
    assert history[1].tool_call_id == "call_00_existing"

