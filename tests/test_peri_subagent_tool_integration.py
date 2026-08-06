import pytest


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_peri_routes_child_tool_events_to_typed_lower_timeline(monkeypatch):
    from modus.desktop import server

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "primary", "model_id": "primary", "name": "Host", "provider": "test", "model": "host", "api_key": "key"},
            "worker_1": {"id": "sub", "model_id": "sub", "name": "Inspector", "provider": "test", "model": "sub", "api_key": "key"},
        },
    })

    async def decompose(*_args, **_kwargs):
        return [{"name": "Inspect", "description": "inspect", "context": "repo", "success_criteria": "cite file"}]

    async def execute(task, model, message, **kwargs):
        assert __import__("pathlib").Path(kwargs["cwd"]).is_absolute()
        await kwargs["event_callback"]({
            "type": "subagent_tool_call", "name": "read_file", "input": {"path": "README.md"},
        })
        await kwargs["event_callback"]({
            "type": "subagent_tool_result", "name": "read_file", "result": "project overview", "is_error": False,
        })
        return "The README identifies the project."

    async def review(*_args, **_kwargs):
        return True, []

    async def merge(*_args, **_kwargs):
        return "Host answer"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "Read README")

    events = [packet["event"] for packet in websocket.sent if packet["type"] == "agent_event"]
    lower = [event for event in events if event["channel_id"] == "host_models"]
    tool_events = [event for event in lower if event["type"].startswith("subagent_tool_")]

    assert [event["type"] for event in tool_events] == ["subagent_tool_call", "subagent_tool_result"]
    assert all(event["actor"] == {"kind": "subagent", "id": "sub", "label": "Inspector"} for event in tool_events)
    assert tool_events[0]["payload"] == {
        "task_id": tool_events[0]["task_id"],
        "name": "read_file", "input": {"path": "README.md"},
    }
    assert tool_events[0]["task_id"] == tool_events[1]["task_id"]
    assert tool_events[1]["payload"] == {
        "task_id": tool_events[1]["task_id"],
        "name": "read_file", "result": "project overview", "is_error": False,
    }


def test_frontend_presents_subagent_tool_events_with_progressive_disclosure():
    from pathlib import Path

    from _bundle import js_bundle

    page = js_bundle()
    start = page.index('case "subagent_tool_call":')
    end = page.index('case "subagent_response":', start)
    tool_cases = page[start:end]

    # Both call and result cases render as html cards built by the shared toolRowHtml helper.
    assert 'case "subagent_tool_call":' in tool_cases
    assert 'case "subagent_tool_result":' in tool_cases
    assert 'html:true' in tool_cases
    assert 'toolRowHtml(' in tool_cases

    # The helper keeps the tool title and result summary visible. Only request
    # parameters and oversized raw logs use progressive disclosure.
    helper_start = page.index("function toolRowHtml")
    helper_end = page.index("const toolTimers", helper_start)
    helper = page[helper_start:helper_end]
    assert 'class="timeline-tool-head"' in helper
    assert 'toolResultViewHtml' in helper
    assert 'class="timeline-tool-input"' in helper
    assert 'class="tool-result-output-details"' in page
    assert 'host_models' in page
