from pathlib import Path

from _bundle import css_bundle, js_bundle, page_html

ROOT = Path(__file__).parents[1]
PAGE = ROOT / "src/modus/desktop/static/index.html"
SERVER = ROOT / "src/modus/desktop/server.py"
MOA_RUNNER = ROOT / "src/modus/desktop/moa_runner.py"


def test_dual_mode_lower_area_is_a_vertical_typed_event_timeline() -> None:
    """Host/model communication stacks by event sequence in one lower container."""
    css = css_bundle()
    js = js_bundle()
    assert ".lower-chat-area {" in css
    assert "display:flex; flex-direction:column;" in css
    assert ".timeline-item { min-width:0; max-width:100%; overflow:hidden;" in css
    assert 'event.channel_id === "host_models"' in js
    assert 'return document.getElementById("chatAreaLower")' in js


def test_moa_reference_events_have_typed_lower_timeline_presentations() -> None:
    page = js_bundle()
    for event_name in (
        "host_dispatch",
        "reference_started",
        "reference_response",
        "host_aggregation",
    ):
        assert f'case "{event_name}":' in page
    assert "appendLowerEvent(" not in page
    assert "addSystemMsg(" not in page[page.index("class TimelineRenderer"):page.index("const eventStore")]


def test_peri_events_have_typed_lower_timeline_presentations() -> None:
    page = js_bundle()
    for event_name in (
        "subtask_assignment",
        "subagent_progress",
        "subagent_response",
        "host_review",
    ):
        assert f'case "{event_name}":' in page
    assert "appendLowerEvent(" not in page


def test_backend_emits_typed_moa_events_in_completion_order() -> None:
    moa = MOA_RUNNER.read_text()
    assert "EventType.REFERENCE_STARTED" in moa
    assert "EventType.REFERENCE_RESPONSE" in moa
    assert "EventType.HOST_AGGREGATION" in moa
    assert "asyncio.as_completed" in moa
    assert 'ChannelId.HOST_MODELS' in moa
