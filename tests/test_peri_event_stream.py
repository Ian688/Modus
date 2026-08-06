import pytest


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


def _one_worker_models() -> dict:
    return {
        "peri_roles": {
            "host": {
                "id": "host", "model_id": "host", "name": "Host",
                "provider": "test", "model": "host", "api_key": "key",
            },
            "worker_1": {
                "id": "worker", "model_id": "worker", "name": "Worker",
                "provider": "test", "model": "worker", "api_key": "key",
            },
        },
    }


def _assert_failed_without_consensus(websocket: FakeWebSocket) -> None:
    events = [item["event"] for item in websocket.sent if item["type"] == "agent_event"]
    types = [event["type"] for event in events]
    assert "host_response" not in types
    assert "run_completed" not in types
    assert types[-1] == "run_error"
    terminals = [item for item in websocket.sent if item["type"] == "done"]
    assert len(terminals) == 1
    assert terminals[0]["stop_reason"] == "failed"
    assert terminals[0]["budget"]["stop_reason"] == "failed"


@pytest.mark.asyncio
async def test_peri_emits_upper_and_lower_channels_in_one_run(monkeypatch):
    from modus.desktop import server

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "primary", "model_id": "primary", "name": "Host", "provider": "test", "model": "host"},
            "worker_1": {"id": "sub-a", "model_id": "sub-a", "name": "Research", "provider": "test", "model": "sub-a"},
            "worker_2": {"id": "sub-b", "model_id": "sub-b", "name": "Builder", "provider": "test", "model": "sub-b"},
        },
    })

    async def fake_decompose(message, provider, model, count, **kwargs):
        return [
            {"name": "Research", "description": "research", "context": message, "success_criteria": "facts"},
            {"name": "Build", "description": "build", "context": message, "success_criteria": "plan"},
        ]

    async def fake_execute(task, model, user_message, timeout=120.0, *, stream_callback=None, **_kwargs):
        return f"{model['name']} output"

    async def fake_review(tasks, outputs, provider, model, user_message, **kwargs):
        return True, [], [8.0]

    async def fake_merge(tasks, outputs, provider, model, user_message, **kwargs):
        return "Host final answer"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", fake_decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", fake_execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", fake_review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", fake_merge)

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "Build a game")

    events = [item["event"] for item in websocket.sent if item["type"] == "agent_event"]
    assert events
    assert len({event["run_id"] for event in events}) == 1
    assert {event["channel_id"] for event in events} == {"user_host", "host_models"}
    lower = [event for event in events if event["channel_id"] == "host_models"]
    lower_types = [event["type"] for event in lower]
    assert lower_types.count("subtask_assignment") == 2
    assert lower_types.count("subagent_progress") == 2
    assert lower_types.count("subagent_response") == 4
    assert lower_types[-1] == "host_review"
    assignments = [event for event in lower if event["type"] == "subtask_assignment"]
    assert all(event["payload"].get("task_id") for event in assignments)
    assert all(event["payload"].get("target_id") for event in assignments)
    assert len({event["payload"]["task_id"] for event in assignments}) == 2
    last_assignment = max(i for i, kind in enumerate(lower_types) if kind == "subtask_assignment")
    first_worker_terminal = min(
        i for i, event in enumerate(lower)
        if event["type"] == "subagent_response" and event["status"] in {"completed", "failed"}
    )
    assert last_assignment < first_worker_terminal
    for worker_id in {"sub-a", "sub-b"}:
        worker_events = [
            event for event in lower
            if event["type"] == "subagent_response" and event["actor"]["id"] == worker_id
        ]
        assert [event["status"] for event in worker_events] == ["streaming", "completed"]
        assert all(event["payload"].get("task_id") for event in worker_events)
    upper_types = [event["type"] for event in events if event["channel_id"] == "user_host"]
    assert upper_types == ["run_started", "user_message", "host_response", "run_completed"]
    transport_types = [item["type"] for item in websocket.sent if item["type"] != "agent_event"]
    assert "done" in transport_types
    assert not ({"step", "peri_sub_start", "peri_sub_done", "peri_output"} & set(transport_types))


@pytest.mark.asyncio
async def test_peri_workers_enter_execution_concurrently(monkeypatch):
    import asyncio
    from modus.desktop import server

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "host", "model_id": "host", "name": "Host", "provider": "test", "model": "host"},
            "worker_1": {"id": "a", "model_id": "a", "name": "A", "provider": "test", "model": "a"},
            "worker_2": {"id": "b", "model_id": "b", "name": "B", "provider": "test", "model": "b"},
        },
    })
    both_started = asyncio.Event()
    started: set[str] = set()

    async def decompose(*_args, **_kwargs):
        return [
            {"name": "A", "description": "a", "success_criteria": "a"},
            {"name": "B", "description": "b", "success_criteria": "b"},
        ]

    async def execute(_task, model, *_args, **_kwargs):
        started.add(model["id"])
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        return model["id"] + " output"

    async def review(*_args, **_kwargs):
        return True, [], [8.0]

    async def merge(*_args, **_kwargs):
        return "merged"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    await server._run_peri_stream(
        websocket, server.DaoSession(id="s", db_id="db"), "parallel task",
    )

    assert started == {"a", "b"}


def test_peri_handler_uses_typed_emitter():
    source = (__import__("pathlib").Path(__file__).parents[1] / "src/modus/desktop/peri_runner.py").read_text()
    start = source.index("async def _run_peri_body")
    implementation = source[start:]
    assert "RunEventEmitter" in implementation
    assert "EventType.SUBTASK_ASSIGNMENT" in implementation
    assert "EventType.SUBAGENT_RESPONSE" in implementation
    assert "EventType.HOST_REVIEW" in implementation
    assert "EventType.HOST_RESPONSE" in implementation
    assert "ChannelId.HOST_MODELS" in implementation
    assert "ChannelId.USER_HOST" in implementation


@pytest.mark.asyncio
async def test_peri_missing_workers_has_only_failed_terminal(monkeypatch):
    from modus.desktop import server

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "host", "model_id": "host", "provider": "test", "model": "host"},
        },
    })
    websocket = FakeWebSocket()

    await server._run_peri_stream(
        websocket, server.DaoSession(id="s", db_id="db"), "task",
    )

    events = [item["event"] for item in websocket.sent if item["type"] == "agent_event"]
    assert [event["type"] for event in events] == ["run_started", "user_message", "run_error"]
    assert "run_completed" not in {event["type"] for event in events}
    assert [item for item in websocket.sent if item["type"] == "done"][-1]["stop_reason"] == "failed"


@pytest.mark.asyncio
async def test_peri_unexpected_failure_emits_one_typed_error_and_one_terminal_control(monkeypatch):
    from modus.desktop import server

    def fail_loading_models():
        raise RuntimeError("repository unavailable")

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: fail_loading_models())
    websocket = FakeWebSocket()

    await server._run_peri_stream(
        websocket, server.DaoSession(id="s", db_id="db"), "task",
    )

    events = [item["event"] for item in websocket.sent if item["type"] == "agent_event"]
    assert [event["type"] for event in events] == ["run_started", "user_message", "run_error"]
    assert events[-1]["payload"]["code"] == "peri_failed"
    terminals = [item for item in websocket.sent if item["type"] == "done"]
    assert len(terminals) == 1
    assert terminals[0]["stop_reason"] == "failed"


@pytest.mark.asyncio
async def test_peri_worker_model_failure_cannot_become_consensus(monkeypatch):
    from modus.desktop import peri, server

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: _one_worker_models())

    async def decompose(*_args, **_kwargs):
        return [{"name": "Inspect", "description": "inspect", "success_criteria": "facts"}]

    async def fail_worker(*_args, **_kwargs):
        raise peri.PeriModelError("worker provider unavailable")

    monkeypatch.setattr(peri, "decompose_task", decompose)
    monkeypatch.setattr(peri, "execute_subtask", fail_worker)
    websocket = FakeWebSocket()

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "task")

    _assert_failed_without_consensus(websocket)


@pytest.mark.asyncio
async def test_peri_invalid_host_review_cannot_become_consensus(monkeypatch):
    from modus.desktop import peri, server

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: _one_worker_models())

    async def decompose(*_args, **_kwargs):
        return [{"name": "Inspect", "description": "inspect", "success_criteria": "facts"}]

    async def execute(*_args, **_kwargs):
        return "worker evidence"

    async def invalid_review(*_args, **_kwargs):
        raise peri.PeriModelError("Host review returned invalid JSON")

    monkeypatch.setattr(peri, "decompose_task", decompose)
    monkeypatch.setattr(peri, "execute_subtask", execute)
    monkeypatch.setattr(peri, "review_subtask_outputs", invalid_review)
    websocket = FakeWebSocket()

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "task")

    _assert_failed_without_consensus(websocket)


@pytest.mark.asyncio
async def test_peri_merge_failure_cannot_fall_back_to_concatenation(monkeypatch):
    from modus.desktop import peri, server

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: _one_worker_models())

    async def decompose(*_args, **_kwargs):
        return [{"name": "Inspect", "description": "inspect", "success_criteria": "facts"}]

    async def execute(*_args, **_kwargs):
        return "worker evidence"

    async def review(*_args, **_kwargs):
        return True, [], [8.0]

    async def fail_merge(*_args, **_kwargs):
        raise peri.PeriModelError("host merge failed")

    monkeypatch.setattr(peri, "decompose_task", decompose)
    monkeypatch.setattr(peri, "execute_subtask", execute)
    monkeypatch.setattr(peri, "review_subtask_outputs", review)
    monkeypatch.setattr(peri, "merge_outputs", fail_merge)
    websocket = FakeWebSocket()

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "task")

    _assert_failed_without_consensus(websocket)


@pytest.mark.asyncio
async def test_peri_revision_fails_after_max_rounds_without_convergence(monkeypatch):
    from modus.desktop import server

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: _one_worker_models())

    async def decompose(*_args, **_kwargs):
        return [{"name": "Inspect", "description": "inspect", "success_criteria": "facts"}]

    calls = 0

    async def execute(*_args, **_kwargs):
        # A different answer every round keeps semantic overlap low, so the
        # loop cannot claim convergence while the Host keeps rejecting.
        return f"revised evidence round {calls}"

    async def review(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return False, ["add stronger evidence"], [3.0]

    async def merge(*_args, **_kwargs):
        raise AssertionError("merge must not run after failed review")

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)
    websocket = FakeWebSocket()

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "task")

    # Initial review plus at least one revision round; the loop must stop
    # (stall or round cap) and fail rather than accept a rejected consensus.
    assert calls >= 2
    _assert_failed_without_consensus(websocket)


@pytest.mark.asyncio
async def test_peri_send_failure_reaps_sibling_before_failed_terminal(monkeypatch):
    """Terminal failure is emitted only after every worker has stopped."""
    import asyncio

    from modus.desktop import server
    from modus.desktop.events import RunEventEmitter

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "host", "name": "Host", "provider": "test", "model": "host"},
            "worker_1": {"id": "fail", "name": "Fail", "provider": "test", "model": "fail"},
            "worker_2": {"id": "slow", "name": "Slow", "provider": "test", "model": "slow"},
        },
    })

    async def decompose(*_args, **_kwargs):
        return [
            {"name": "Fail", "description": "fail", "success_criteria": "never"},
            {"name": "Slow", "description": "slow", "success_criteria": "never"},
        ]

    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    release_sibling = asyncio.Event()
    terminal_observations: list[bool] = []
    provider_calls: list[str] = []

    async def execute(_task, model, *_args, stream_callback=None, **_kwargs):
        provider_calls.append(model["id"])
        if model["id"] == "slow":
            sibling_started.set()
            try:
                await release_sibling.wait()
                await stream_callback("late worker event")
                return "late worker artifact"
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise
        await sibling_started.wait()
        await stream_callback("break transport")
        return "unreachable"

    async def unexpected_stage(*_args, **_kwargs):
        raise AssertionError("Host stages must not run after event delivery fails")

    websocket = FakeWebSocket()

    async def audit_event(event: dict) -> bool:
        if event["type"] == "run_error":
            terminal_observations.append(sibling_cancelled.is_set())
        return True

    class FailingEmitter(RunEventEmitter):
        async def emit(self, event_type, channel_id, actor, payload, **kwargs):
            if (
                str(event_type) == "subagent_response"
                and str(kwargs.get("status")) == "streaming"
                and payload.get("markdown") == "break transport"
            ):
                raise RuntimeError("socket closed")
            return await super().emit(event_type, channel_id, actor, payload, **kwargs)

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", unexpected_stage)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", unexpected_stage)
    emitter = FailingEmitter(
        run_id="run_peri_reap", mode="peri", send_json=websocket.send_json,
        audit_event=audit_event,
    )

    await server._run_peri_stream(
        websocket, server.DaoSession(id="runtime", db_id=""), "task", emitter=emitter,
        manage_controller=True,
    )

    assert provider_calls == ["fail", "slow"]
    assert sibling_cancelled.is_set()
    assert terminal_observations == [True]
    assert [
        item["event"]["type"] for item in websocket.sent
        if item["type"] == "agent_event" and item["event"]["type"] in {"run_error", "run_completed"}
    ] == ["run_error"]
    before_release = [
        item["event"] for item in websocket.sent
        if item["type"] == "agent_event"
        and item["event"]["type"] in {
            "subagent_response", "subagent_tool_call", "subagent_tool_result", "artifact",
        }
        and item["event"]["actor"]["id"] == "slow"
    ]
    release_sibling.set()
    await asyncio.sleep(0.01)
    after_release = [
        item["event"] for item in websocket.sent
        if item["type"] == "agent_event"
        and item["event"]["type"] in {
            "subagent_response", "subagent_tool_call", "subagent_tool_result", "artifact",
        }
        and item["event"]["actor"]["id"] == "slow"
    ]
    assert after_release == before_release, [
        (event["type"], event["status"], event["actor"]["id"], event["payload"])
        for event in after_release[len(before_release):]
    ]
    assert not any(
        "late worker" in str(item)
        for item in websocket.sent
    )


@pytest.mark.asyncio
async def test_full_peri_transport_failure_persists_one_atomic_cancel_terminal(monkeypatch):
    from fastapi import WebSocketDisconnect

    from modus.config import ModusConfig
    from modus.desktop import db, server

    class Engine:
        config = ModusConfig()

    class DisconnectingWebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)
            if packet["type"] == "agent_event" and packet["event"]["type"] == "user_message":
                raise WebSocketDisconnect()

    persisted = db.create_session("Peri disconnect")
    session = server.DaoSession(
        id="runtime-peri-disconnect", db_id=persisted["id"], engine=Engine(),
    )
    websocket = DisconnectingWebSocket()

    await server._run_peri_session(websocket, session, "task")

    run_id = session.active_run_id
    run = db.get_run(run_id)
    root = db.get_run_task(f"task_{run_id}_root")
    terminal = [
        event for event in db.get_run_events(run_id)
        if event["type"] in {"run_completed", "run_error"}
    ]
    assert run is not None and run["state"] == "cancelled"
    assert run["stop_reason"] == "cancelled"
    assert root is not None and root["status"] == "cancelled"
    assert len(terminal) == 1
    assert terminal[0]["type"] == "run_error"
    assert terminal[0]["status"] == "cancelled"
    assert terminal[0]["payload"]["code"] == "transport_disconnected"
