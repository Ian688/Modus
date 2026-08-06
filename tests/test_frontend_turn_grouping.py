"""Turn grouping: one user turn (activity + reply + footer) is wrapped in a
semantic `.turn` reading group without adding another visible card surface.

The live path anchors on `run_id` (the EventStore already indexes by run_id);
the reload path has no run_id and falls back to the currently-open turn. All
existing bubble classes (.msg.user/.msg.assistant/.run-completion) are kept —
the wrapper only adds grouping.
"""

from _bundle import css_bundle, js_bundle


def test_turn_grouping_helpers_exist() -> None:
    page = js_bundle()
    assert 'turn.className = "turn"' in page
    assert "_turnAnchor(event)" in page
    assert "_openTurn(container, event)" in page
    assert "dataset.turnId" in page
    assert "dataset.absorbable" in page
    assert "dataset.result" in page


def test_turn_grouping_css_lives_in_page_style() -> None:
    page = css_bundle()
    assert ".turn{" in page
    assert ".turn-bd{" in page
    assert ".msg-bar{" in page
    assert '.turn-bd>.run-completion{' in page
    assert ".turn-bd>.msg{" in page
    # The reading column constraint on wide screens.
    assert "--read-col:" in page
    assert 'align-items:center' in page


def test_turn_group_is_visually_flat_and_activity_is_single_disclosure() -> None:
    css = css_bundle()
    assert ".chat-area>.turn{" in css
    assert "box-shadow:none" in css
    assert ".run-activity{" in css
    assert ".run-activity-items-bounded{" in css


def test_conversation_v1_has_stable_role_anchors() -> None:
    page = js_bundle()
    css = css_bundle()
    assert 'node.dataset.messageRole = "user"' in page
    assert 'node.dataset.messageRole = "assistant"' in page
    assert 'class="message-label">你的请求' not in page
    assert 'class="message-label">Modus' in page
    assert "Conversation V1: request → activity → answer → outcome" in css


def test_message_action_bar_wired_in_copy_handlers() -> None:
    page = js_bundle()
    assert 'data-msg-copy' in page
    assert 'data-msg-resend' in page
    assert 'msg-bar' in page
    # Resend refills the composer; it must not auto-send (avoid touching the
    # sendMessage submission chain).
    assert 'input.value = text' in page
    assert 'input.focus()' in page


def test_reload_cleanup_removes_turn_nodes() -> None:
    page = js_bundle()
    # The pinned cleanup selector string stays intact; the .turn sweep is a
    # separate line added after it.
    assert 'ca.querySelectorAll(".msg, .timeline-item, .run-completion, .empty-state, .collab-msg, .timeline-expand-earlier").forEach(e => e.remove());' in page
    assert 'ca.querySelectorAll(".turn").forEach(e => e.remove());' in page
