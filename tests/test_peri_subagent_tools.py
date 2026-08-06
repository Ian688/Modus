import pytest

from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.registry import ToolRegistry


class FakeToolClient:
    """Requests one read-only tool, then answers after its result returns."""

    async def chat(self, messages, tools, *, system_prompt):
        if not any(message.role == "tool" for message in messages):
            all_names = {tool["function"]["name"] for tool in tools}
            assert "read_file" in all_names
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "read-1",
                    "function": {"name": "read_file", "arguments": '{"path":"brief.txt"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        assert messages[-1].content == "source evidence"
        yield {"type": "text_delta", "text": "Verified from the assigned file."}
        yield {"type": "message_end", "stop_reason": "end_turn"}


@pytest.mark.asyncio
async def test_subagent_executes_only_allowlisted_read_tool_and_reports_typed_events(monkeypatch):
    from modus.desktop import peri

    async def read_file(payload, _context):
        assert payload == {"path": "brief.txt"}
        return ToolResult("source evidence")

    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file", description="read", handler=read_file,
        parameters=object_schema({"path": {"type": "string"}}, ["path"]),
        required_keys=["path"],
    ))
    # This must never be exposed to the child even when supplied by a caller.
    registry.register(Tool(
        name="write_file", description="write", handler=read_file,
        parameters=object_schema({"path": {"type": "string"}}),
    ))

    monkeypatch.setattr(peri, "create_llm_client", lambda _cfg: FakeToolClient())
    events: list[dict] = []

    output = await peri.execute_subtask(
        {"description": "inspect", "context": "local", "success_criteria": "cite evidence"},
        {"provider": "test", "model": "sub", "api_key": "key"},
        "inspect the brief",
        tool_registry=registry,
        event_callback=events.append,
    )

    assert output == "Verified from the assigned file."
    assert [event["type"] for event in events] == [
        "subagent_tool_call", "subagent_tool_result",
    ]
    assert events[0]["name"] == "read_file"
    assert events[0]["input"] == {"path": "brief.txt"}
    assert events[1] == {
        "type": "subagent_tool_result",
        "tool_call_id": "read-1",
        "name": "read_file",
        "result": "source evidence",
        "is_error": False,
    }


def test_subagent_registry_excludes_all_mutating_and_shell_tools():
    from modus.desktop.peri import build_subagent_tool_registry
    from modus.tools.builtins import get_builtin_tools

    registry = build_subagent_tool_registry(get_builtin_tools())

    assert set(registry.list_names()) == {
        "glob", "grep", "list_dir", "read_file",
        "load_skill", "search_code", "web_search", "web_fetch",
        "git_status", "git_diff_work",
    }
    assert not {"save_memory", "revert_turn", "edit", "write_file", "bash", "git_add", "git_commit", "spawn_subtask"} & set(registry.list_names())


def test_recursive_registry_adds_spawn_subtask_only_with_context():
    from modus.desktop.peri import build_subagent_tool_registry
    from modus.tools.builtins import get_builtin_tools

    # recursive=True without a spawn_context cannot build the tool: recursion stays off.
    bare = build_subagent_tool_registry(get_builtin_tools(), recursive=True)
    assert "spawn_subtask" not in bare.list_names()

    ctx = {
        "execute": lambda *a, **k: "child", "ref_config": {},
        "depth": 0, "max_recursion_depth": 2, "cwd": "/tmp",
        "writable": False, "cancel_event": None, "approval_callback": None,
        "event_callback": None, "max_turns": 5,
    }
    registry = build_subagent_tool_registry(get_builtin_tools(), recursive=True, spawn_context=ctx)
    assert "spawn_subtask" in registry.list_names()


def test_writable_subagent_registry_scopes_writes_but_never_shell_or_merge():
    from modus.desktop.peri import build_subagent_tool_registry
    from modus.tools.builtins import get_builtin_tools

    registry = build_subagent_tool_registry(get_builtin_tools(), writable=True)
    names = set(registry.list_names())

    # Writable mode adds scoped file/git writes…
    assert {"write_file", "edit_file", "git_add", "git_commit"} <= names
    # …but never shell, memory, revert, or worktree/merge mutation.
    assert not {"bash", "save_memory", "revert_turn", "git_worktree_remove", "git_merge_to_main", "git_worktree_add"} & names


@pytest.mark.asyncio
async def test_subagent_tool_loop_stops_at_its_turn_budget(monkeypatch):
    from modus.desktop import peri

    class RepeatingToolClient:
        async def chat(self, _messages, _tools, *, system_prompt):
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0, "id": "loop", "function": {
                        "name": "read_file", "arguments": '{"path":"brief.txt"}',
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}

    async def read_file(_payload, _context):
        return ToolResult("evidence")

    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file", description="read", handler=read_file,
        parameters=object_schema({"path": {"type": "string"}}, ["path"]),
    ))
    monkeypatch.setattr(peri, "create_llm_client", lambda _cfg: RepeatingToolClient())

    with pytest.raises(peri.PeriModelError, match="exceeded 2 tool turns"):
        await peri.execute_subtask(
            {"description": "inspect"}, {"provider": "test", "model": "sub", "api_key": "key"},
            "inspect", tool_registry=registry, max_turns=2,
        )
