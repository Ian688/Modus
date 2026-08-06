from fastapi.testclient import TestClient


class FakeEngine:
    def __init__(self, **_kwargs) -> None:
        pass

    async def ask(self, message, history=None, *, approval_callback=None, cancel_event=None, budget=None, session_id=None, run_id=None):
        yield {"type": "text_delta", "text": f"Echo: {message}"}
        yield {"type": "done", "messages": [], "total_tokens": 3, "total_turns": 1}


def test_websocket_default_agent_streams_typed_events_and_done(monkeypatch):
    from modus.desktop import server

    async def fake_registry(**_kwargs):
        return object()

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            ready = socket.receive_json()
            assert ready["type"] == "session_ready"

            socket.send_json({"type": "run_message", "content": "smoke"})
            received = []
            # Typed events, control messages, and worldview updates have
            # independent counts. Stop on the terminal control contract
            # instead of guessing a packet count.
            for _ in range(20):
                item = socket.receive_json()
                received.append(item)
                if item["type"] == "done":
                    break
            else:
                raise AssertionError(f"did not receive done; got {received!r}")

    typed = [item["event"] for item in received if item["type"] == "agent_event"]
    assert [event["type"] for event in typed] == ["run_started", "user_message", "host_response", "run_completed"]
    assert len({event["run_id"] for event in typed}) == 1
    assert any(item["type"] == "done" for item in received)


def test_websocket_interrupt_closes_receiver_after_fail_closed_cancel(monkeypatch):
    from modus.desktop import server

    observed = []
    original_cancel = server.DaoSession.cancel_stream

    def capture_cancel(session):
        observed.append(session.id)
        original_cancel(session)

    async def fake_registry(**_kwargs):
        return object()

    monkeypatch.setattr(server.DaoSession, "cancel_stream", capture_cancel)
    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "interrupt"})

    assert observed


def _receive_until(socket, terminal_type: str) -> list[dict]:
    received = []
    for _ in range(40):
        item = socket.receive_json()
        received.append(item)
        if item["type"] == "error":
            raise AssertionError(f"WebSocket failed before {terminal_type}: {item!r}")
        if item["type"] == terminal_type:
            return received
    raise AssertionError(f"did not receive {terminal_type}; got {received!r}")


def test_websocket_moa_keeps_host_and_reference_events_in_one_run(monkeypatch, tmp_path):
    from modus.desktop import server

    # Collaboration runs require a workspace at admission; the default test
    # session picks up this root so the MOA gate passes.
    monkeypatch.setattr(server, "_desktop_default_workspace_root", str(tmp_path))

    async def fake_registry(**_kwargs):
        return object()

    async def fake_reference(ref, _messages, _prompt, temperature=0.7, timeout=25.0, *, stream_callback=None, owner=None):
        if stream_callback:
            await stream_callback(f"Advice from {ref['name']}")
        return f"Advice from {ref['name']}"

    async def fake_host(host_config, messages, system_prompt, guidance, temperature=0.7, timeout=60.0, *, stream_callback=None, owner=None):
        if stream_callback:
            await stream_callback("Host synthesis result")
        return "Host synthesis result"

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "moa_roles": {
            "host": {"id": "host", "model_id": "host", "name": "Host", "provider": "test", "model": "h", "api_key": "key"},
            "reference_1": {"id": "architect", "model_id": "architect", "name": "Architect", "provider": "test", "model": "ref", "api_key": "key"},
        },
    })
    monkeypatch.setattr(server, "_session_mode_snapshot", lambda mode: {
        "host": {"model_id": "host"},
        "reference_1": {"model_id": "architect"},
    } if mode == "moa" else {})
    monkeypatch.setattr("modus.agent.moa.call_reference", fake_reference)
    monkeypatch.setattr("modus.agent.moa.call_aggregator", fake_host)
    monkeypatch.setattr("modus.agent.moa.call_host", fake_host)

    # Mock model_repository to provide the default host at socket startup.
    class FakeRepo:
        def runtime_model(self, model_id=None):
            return {"id": "host", "name": "Host", "provider": "test", "model": "h", "api_key": "key"}

        def public_snapshot(self):
            return {
                "models": [{"id": "host", "name": "Host", "provider": "test", "model": "h"}],
                "selection": {"default_model_id": "host", "moa_roles": {}},
            }
    monkeypatch.setattr(server, "model_repository", FakeRepo())

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "session_set_mode", "mode": "moa"})
            assert socket.receive_json()["type"] == "mode_updated"
            socket.send_json({"type": "run_message", "content": "smoke MOA"})
            received = _receive_until(socket, "done")

    typed = [item["event"] for item in received if item["type"] == "agent_event"]
    assert {event["run_id"] for event in typed}.__len__() == 1
    assert {"host_dispatch", "reference_started", "reference_response", "host_aggregation", "host_response", "run_completed"} <= {
        event["type"] for event in typed
    }


def test_websocket_peri_emits_worker_and_host_events(monkeypatch, tmp_path):
    from modus.desktop import server

    monkeypatch.setattr(server, "_desktop_default_workspace_root", str(tmp_path))

    async def fake_registry(**_kwargs):
        return object()

    async def fake_decompose(message, provider, model, count, **_kwargs):
        return [{"name": "Research", "description": message, "context": message, "success_criteria": "facts"}]

    async def fake_execute(_task, _model, _message, timeout=120.0, *, stream_callback=None, **_kwargs):
        if stream_callback:
            await stream_callback("Worker evidence")
        return "Worker output"

    async def fake_review(*_args, **_kwargs):
        return True, [], [8.0]

    async def fake_merge(*_args, **_kwargs):
        return "Host final"

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "primary", "model_id": "primary", "name": "Host", "provider": "test", "model": "host"},
            "worker_1": {"id": "worker", "model_id": "worker", "name": "Worker", "provider": "test", "model": "worker"},
        },
    })
    monkeypatch.setattr(server, "_session_mode_snapshot", lambda mode: {
        "host": {"model_id": "primary"},
        "worker_1": {"model_id": "worker"},
    } if mode == "peri" else {})
    monkeypatch.setattr("modus.desktop.peri.decompose_task", fake_decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", fake_execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", fake_review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", fake_merge)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "session_set_mode", "mode": "peri"})
            assert socket.receive_json()["type"] == "mode_updated"
            socket.send_json({"type": "run_message", "content": "smoke Peri"})
            received = _receive_until(socket, "done")

    typed = [item["event"] for item in received if item["type"] == "agent_event"]
    assert {"subtask_assignment", "subagent_response", "host_review", "host_response", "run_completed"} <= {
        event["type"] for event in typed
    }
    assert len({event["run_id"] for event in typed}) == 1
