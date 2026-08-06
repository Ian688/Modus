import asyncio
from dataclasses import asdict

import pytest


@pytest.mark.asyncio
async def test_emitter_serializes_a_complete_event_envelope() -> None:
    from modus.desktop.events import Actor, ChannelId, EventType, RunEventEmitter

    sent: list[dict] = []

    async def send_json(message: dict) -> None:
        sent.append(message)

    emitter = RunEventEmitter(run_id="run_demo", mode="default", send_json=send_json)
    event = await emitter.emit(
        event_type=EventType.USER_MESSAGE,
        channel_id=ChannelId.USER_HOST,
        actor=Actor.user("User"),
        payload={"markdown": "hello"},
    )

    assert sent == [{"type": "agent_event", "event": event.to_wire()}]
    wire = sent[0]["event"]
    assert wire["event_id"] == event.event_id
    assert wire["run_id"] == "run_demo"
    assert wire["channel_id"] == "user_host"
    assert wire["parent_event_id"] is None
    assert wire["sequence"] == 1
    assert wire["mode"] == "default"
    assert wire["actor"] == {"kind": "user", "id": "user", "label": "User"}
    assert wire["type"] == "user_message"
    assert wire["status"] == "completed"
    assert wire["payload"] == {"markdown": "hello"}
    assert wire["timestamp"].endswith("Z")


@pytest.mark.asyncio
async def test_emitter_sequences_events_monotonically_within_a_run() -> None:
    from modus.desktop.events import Actor, ChannelId, EventType, RunEventEmitter

    events = []

    async def capture(message: dict) -> None:
        events.append(message["event"])

    emitter = RunEventEmitter(run_id="run_demo", mode="moa", send_json=capture)
    host = Actor.host("primary", "主持人")
    first = await emitter.emit(EventType.HOST_DISPATCH, ChannelId.HOST_MODELS, host, {"target_id": "a"})
    second = await emitter.emit(
        EventType.REFERENCE_RESPONSE,
        ChannelId.HOST_MODELS,
        Actor.reference_model("ref_a", "Reference A"),
        {"markdown": "advice"},
        parent_event_id=first.event_id,
    )

    assert [event["sequence"] for event in events] == [1, 2]
    assert second.parent_event_id == first.event_id
    assert second.run_id == first.run_id == "run_demo"


@pytest.mark.asyncio
async def test_streaming_event_reuses_id_and_accumulates_text_without_new_sequence() -> None:
    from modus.desktop.events import Actor, ChannelId, EventStatus, EventType, RunEventEmitter

    sent = []

    async def capture(message: dict) -> None:
        sent.append(message["event"])

    emitter = RunEventEmitter(run_id="run_demo", mode="default", send_json=capture)
    first = await emitter.emit(
        EventType.HOST_RESPONSE, ChannelId.USER_HOST, Actor.host("primary"),
        {"markdown": "事件"}, status=EventStatus.STREAMING,
    )
    second = await emitter.emit(
        EventType.HOST_RESPONSE, ChannelId.USER_HOST, Actor.host("primary"),
        {"markdown": "流成功。"}, status=EventStatus.STREAMING, event_id=first.event_id,
    )

    assert second.event_id == first.event_id
    assert second.sequence == first.sequence == 1
    assert second.payload["markdown"] == "事件流成功。"
    assert len(sent) == 2
    assert sent[-1]["payload"]["markdown"] == "事件流成功。"
    assert sent[0]["revision"] == 0
    assert sent[-1]["revision"] == 1


def test_actor_round_trip_and_invalid_event_contract() -> None:
    from modus.desktop.events import Actor, AgentEvent, ChannelId, EventType

    actor = Actor.from_wire({"kind": "tool", "id": "write_file", "label": "write_file"})
    assert actor.to_wire() == {"kind": "tool", "id": "write_file", "label": "write_file"}

    with pytest.raises(ValueError, match="payload"):
        AgentEvent.create(
            run_id="run_demo",
            sequence=1,
            mode="default",
            channel_id=ChannelId.USER_HOST,
            actor=actor,
            event_type=EventType.TOOL_CALL,
            payload=None,
        )


def test_event_type_and_channel_are_restricted() -> None:
    from modus.desktop.events import ChannelId, EventType

    assert ChannelId("user_host") is ChannelId.USER_HOST
    assert EventType("host_aggregation") is EventType.HOST_AGGREGATION
    assert EventType("approval_resolved") is EventType.APPROVAL_RESOLVED
    with pytest.raises(ValueError):
        ChannelId("free_text_channel")
    with pytest.raises(ValueError):
        EventType("made_up_event")
