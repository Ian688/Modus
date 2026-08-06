import pytest


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_execute_subtask_forwards_each_stream_delta_and_returns_full_text(monkeypatch):
    from modus.desktop import peri

    class FakeClient:
        async def chat(self, messages, tools, system_prompt):
            yield {"type": "text_delta", "text": "子任务"}
            yield {"type": "text_delta", "text": "输出"}

    monkeypatch.setattr(peri, "create_llm_client", lambda cfg: FakeClient())
    chunks: list[str] = []

    async def capture(chunk: str) -> None:
        chunks.append(chunk)

    result = await peri.execute_subtask(
        {"description": "analyze", "context": "ctx", "success_criteria": "complete"},
        {"provider": "test", "model": "worker", "api_key": "key"},
        "original task",
        stream_callback=capture,
    )

    assert chunks == ["子任务", "输出"]
    assert result == "子任务输出"


@pytest.mark.asyncio
async def test_peri_worker_uses_one_stable_streaming_event_then_completes(monkeypatch):
    from modus.desktop import server
    from modus.desktop.events import RunEventEmitter

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "host", "model_id": "host", "name": "Host", "provider": "test", "model": "host", "api_key": "key"},
            "worker_1": {"id": "worker", "model_id": "worker", "name": "Worker", "provider": "test", "model": "worker", "api_key": "key"},
        },
    })

    async def fake_decompose(*args, **kwargs):
        return [{"name": "Analyze", "description": "analyze", "context": "ctx", "success_criteria": "complete"}]

    async def fake_execute(task, model, user_message, timeout=120.0, *, stream_callback=None, **_kwargs):
        await stream_callback("第一段")
        await stream_callback("第二段")
        return "第一段第二段"

    async def fake_review(*args, **kwargs):
        return True, []

    async def fake_merge(*args, **kwargs):
        return "Host answer"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", fake_decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", fake_execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", fake_review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", fake_merge)

    emitter = RunEventEmitter(run_id="run_telestream", mode="peri", send_json=websocket.send_json)
    await server._run_peri_stream(
        websocket, server.DaoSession(id="session", db_id="db"), "task", emitter=emitter,
    )

    events = [item["event"] for item in websocket.sent if item["type"] == "agent_event"]
    worker_events = [event for event in events if event["type"] == "subagent_response"]
    assert len(worker_events) == 4
    assert {event["event_id"] for event in worker_events}.__len__() == 1
    assert [event["status"] for event in worker_events] == ["streaming", "streaming", "streaming", "completed"]
    assert [event["payload"]["markdown"] for event in worker_events] == ["", "第一段", "第一段第二段", "第一段第二段"]
    assert [event["sequence"] for event in worker_events] == [5, 5, 5, 5]


def test_execute_subtask_stream_callback_is_optional_for_backward_compatibility():
    from inspect import signature
    from modus.desktop.peri import execute_subtask

    parameter = signature(execute_subtask).parameters["stream_callback"]
    assert parameter.default is None
    assert parameter.kind.name == "KEYWORD_ONLY"
