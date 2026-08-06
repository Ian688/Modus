"""User message card contract: centered, no avatar, two-line preview +
expand/edit/copy, and attachment cards (project/folder/file/image/url)."""

from pathlib import Path

from _bundle import css_bundle, js_bundle

PAGE = Path(__file__).resolve().parent.parent / "src/modus/desktop/static"


def test_user_card_html_function_exists() -> None:
    page = js_bundle()
    assert "function userCardHtml(payload)" in page
    assert "msg-centered" in page
    assert "user-text-preview" in page
    assert "user-text-edit" in page
    assert "data-user-edit-send" in page
    assert "data-user-copy" in page
    assert "card._userAttachments" in page


def test_user_card_attachment_kinds_rendered() -> None:
    page = js_bundle()
    assert "attach-card" in page
    assert 'data-kind="image"' in page
    assert 'data-kind="url"' in page
    assert "attach-name" in page
    assert "attach-open" in page
    assert "looksLikeDirectory" in page


def test_user_card_wiring_delegated() -> None:
    page = js_bundle()
    assert "function wireUserCardInteractions(root)" in page
    assert "function sendUserEditedMessage(text," in page
    assert "wireUserCardInteractions(card)" in page
    assert "card.dataset.expanded" in page
    assert 'editor.value = (preview.textContent || "").trim()' in page


def test_user_card_css_lives_in_page_style() -> None:
    html = css_bundle()
    assert ".msg.msg-centered{" in html
    assert ".user-card{" in html
    assert "linear-gradient(180deg,#f7f7f8 0%,#ececef 100%)" in html
    assert ".user-text-preview{" in html
    assert "-webkit-line-clamp:2" in html
    assert ".user-attach{" in html


def test_edit_resend_reuses_run_pipeline() -> None:
    page = js_bundle()
    assert "transmitPendingRunSubmission()" in page
    assert "nextTransientRequestId(\"run-message\")" in page
