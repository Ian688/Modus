from _bundle import css_bundle, js_bundle
from modus.agent.query import _tool_input
from modus.tools.executor import _tool_call_arguments


def _call(arguments: str) -> dict:
    return {"id": "call_1", "function": {"name": "write_file", "arguments": arguments}}


def test_truncated_write_file_arguments_are_not_rewritten_as_raw_payload() -> None:
    """Malformed streamed calls are rejected explicitly, never executed as {raw: ...}."""
    raw = '{"path":"game/index.html","content":"<html>'
    payload = _tool_call_arguments(_call(raw))
    preview = _tool_input(_call(raw))

    assert payload["_modus_argument_error"] == "invalid_json"
    assert payload["_modus_raw_arguments"] == raw
    assert preview == payload


def test_file_path_alias_is_normalized_for_write_file_calls() -> None:
    payload = _tool_call_arguments(_call('{"file_path":"game/index.html","content":"ok"}'))
    assert payload == {"path": "game/index.html", "content": "ok"}


def test_typed_lower_timeline_contains_long_content_boundaries() -> None:
    css = css_bundle()
    required_css = (
        ".lower-chat-area { min-width:0; min-height:0; overflow-x:hidden;",
        ".timeline-item { min-width:0; max-width:100%; overflow:hidden;",
        ".timeline-item pre, .timeline-item .code-block { max-width:100%; overflow-x:auto;",
    )
    for rule in required_css:
        assert rule in css


def test_workspace_and_lower_timeline_consume_typed_events_only() -> None:
    page = js_bundle()
    timeline = page[page.index("class TimelineRenderer"):page.index("const eventStore")]

    assert 'event.channel_id === "host_models"' in timeline
    # Tool events are consumed by the KANBAN board's column derivation, not the
    # workspace tracker (which is folded into the board).
    assert "columnForEvent(event)" in page
    assert "ModusKanban.setActiveColumn" in page
    assert "appendLowerEvent(" not in page
    # Tool events are dispatched by type to _renderToolPart (the shared
    # disclosure-row handler) — not rendered through _presentation cases.
    assert 'event.type === "tool_call"' in timeline
    assert 'this._renderToolPart(event, container)' in timeline
