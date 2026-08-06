"""Timeline sequence insertion + reply segmentation contract.

Phase 1 of the message-rendering rework: tool/approval rows render *between*
reply paragraphs in true stream order instead of being appended after the
reply. This is implemented by an ordered-insertion helper (_insertBySeq) and a
per-run segmentation map (_replySegs) that lets later reply deltas open a fresh
segment after a sealed tool row.
"""

from pathlib import Path

from _bundle import js_bundle

PAGE = Path(__file__).resolve().parent.parent / "src/modus/desktop/static"


def test_sequence_insertion_helper_exists() -> None:
    page = js_bundle()
    assert "_insertBySeq(target, node, event)" in page
    assert "dataset.anchorSeq" in page
    assert "Number(child.dataset.anchorSeq" in page
    assert "target.appendChild(node)" in page


def test_reply_segmentation_state_exists() -> None:
    page = js_bundle()
    assert "this._replySegs = new Map()" in page
    assert "this._runCursor = new Map()" in page
    assert "_openReplySegment(target, event)" in page
    assert "_sealReplySegments(runId)" in page
    assert "this._insertBySeq(activity.body, node, source)" in page
    assert "renderTimelineMarkdown(seg.text" in page


def test_reply_state_markers_wire_into_turn() -> None:
    page = js_bundle()
    assert "_setReplyState(event.run_id, \"streaming\")" in page
    assert "turnNode.dataset.replyState = \"done\"" in page
    assert "_setReplyState(runId, state)" in page


def test_reply_state_css_lives_in_page_style() -> None:
    from _bundle import css_bundle

    html = css_bundle()
    assert '.turn[data-reply-state="streaming"]' in html
    assert '.turn[data-reply-state="done"]' in html
    assert ".think-block.thinking-preview .thinking-scroll{" in html
    assert "max-height:2.4em" in html
    assert ".approval-card[data-state=\"completed\"]" in html
    assert ".execution-receipt>summary" in html


def test_done_state_uses_the_answer_and_semantic_completion_without_a_duplicate_summary() -> None:
    page = js_bundle()
    assert "_maybeAppendReplySummary(turn, runId)" not in page
    assert "回复归纳" not in page
    assert 'turnNode.querySelector(".turn-bd").appendChild(footer)' in page
