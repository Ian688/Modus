from pathlib import Path

from _bundle import js_bundle


PAGE = Path(__file__).parents[1] / "src/modus/desktop/static/index.html"


def test_legacy_chat_renderers_are_removed_after_timeline_parity() -> None:
    page = js_bundle()

    # Streaming code fences and tool cards now live in TimelineRenderer, so
    # there is no second renderer that can duplicate, reorder, or misplace DOM.
    for obsolete in (
        "function _feedStream(",
        "function _startCodeBlock(",
        "function _updateCodeBlock(",
        "function _finalizeCodeBlock(",
        "function _finalizeBlocks(",
        "function appendLowerEvent(",
        "function addUserMsg(",
        "let _msgEl =",
        "let _thinkingEl =",
        "activeTypedMode",
        "hasTypedRendererFor",
        "case \"text_delta\":",
        "case \"thinking_delta\":",
        "case \"moa_start\":",
        "case \"peri_sub_start\":",
        "case \"child_spawned\":",
        "case \"child_text_delta\":",
        "case \"contradiction_reported\":",
        "observeLegacy(",
        "legacyTasks",
        'case "moa_toggled":',
        'case "moa_configured":',
        "tc-card",
    ):
        assert obsolete not in page


def test_timeline_renderer_remains_the_single_chat_owner() -> None:
    page = js_bundle()
    assert "class TimelineRenderer" in page
    assert "renderTimelineMarkdown" in page
    assert "timelineRenderer.render(event)" in page
    assert "Legacy renderer removal is deferred" not in page
    assert "TimelineRenderer needs parity" not in page


def test_removed_legacy_change_review_styles_are_not_shipped() -> None:
    page = js_bundle()

    assert ".rp-review-card" not in page
    assert ".rp-review-file" not in page
