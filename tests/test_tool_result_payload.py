"""Contract tests for the canonical ToolResult -> event serialization."""
from modus.tools.base import ToolResult
from modus.tools.payload import artifact_ids_from_result, bounded_for_model, tool_result_event


def test_plain_result_stays_a_two_key_event():
    """A result without optional fields must serialize to exactly result+is_error."""
    result = ToolResult(content="source evidence")
    assert tool_result_event(result) == {"result": "source evidence", "is_error": False}


def test_display_summary_and_metadata_are_conditional():
    result = ToolResult(
        content="body",
        display_summary="写入 src/app.py",
        metadata={"operation": "write", "path": "src/app.py"},
    )
    event = tool_result_event(result)
    assert event["result"] == "body"
    assert event["is_error"] is False
    assert event["display_summary"] == "写入 src/app.py"
    assert event["metadata"]["operation"] == "write"
    # No artifacts/disclosure on a result that has none.
    assert "artifacts" not in event
    assert "disclosure" not in event


def test_model_payload_replaces_content_for_model_and_event():
    result = ToolResult(
        content="legacy visible text",
        model_payload="bounded summary for the model",
        raw_result="full raw output that must never be serialized",
        artifacts=[{"artifact_id": "art_1"}],
        disclosure={"local_bytes_read": 100, "model_bytes_sent": 20},
        logs=["diagnostic line"],
    )
    event = tool_result_event(result)
    # The model (and frontend) read the bounded payload, not the raw text.
    assert event["result"] == "bounded summary for the model"
    assert event["artifacts"] == [{"artifact_id": "art_1"}]
    assert event["disclosure"] == {"local_bytes_read": 100, "model_bytes_sent": 20}
    # Raw and logs never cross the event boundary.
    assert "raw_result" not in event
    assert "logs" not in event


def test_model_text_falls_back_to_content():
    assert ToolResult(content="plain").model_text() == "plain"
    assert ToolResult(content="c", model_payload="m").model_text() == "m"
    assert ToolResult(content="c", model_payload="").model_text() == "c"


def test_extra_fields_are_merged_without_overwriting_core():
    result = ToolResult(content="x")
    event = tool_result_event(result, extra={"tool_call_id": "call_1"})
    assert event["tool_call_id"] == "call_1"
    assert event["result"] == "x"
    # None extras are dropped.
    event = tool_result_event(result, extra={"name": None})
    assert "name" not in event


def test_bounded_for_model_counts_and_bounds_only_when_oversized():
    short = "small"
    text, disclosure = bounded_for_model(short, limit=100)
    assert text == short
    assert disclosure == {"model_bytes_sent": len(short)}
    assert "chars_omitted" not in disclosure

    large = "x" * 500
    text, disclosure = bounded_for_model(large, limit=200)
    assert len(text) < 500
    assert text.startswith("x" * 100)  # head preserved
    assert text.endswith("x" * 60)  # tail preserved
    assert "... 300 characters omitted ..." in text
    assert disclosure["chars_omitted"] == 300
    assert disclosure["truncated"] is True


def test_artifact_ids_from_result_collects_top_level_ids():
    event = {"artifacts": [{"artifact_id": "art_1"}, {"artifact_id": "art_2"}, {"no_id": True}]}
    assert artifact_ids_from_result(event) == ("art_1", "art_2")
    assert artifact_ids_from_result({}) == ()
