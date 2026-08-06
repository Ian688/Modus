"""Real-browser regression cases against the isolated test-mode server.

These drive the actual Desktop UI with Playwright + system Chrome, so they
catch frontend regressions (broken handlers, missing DOM, WS contract drift)
that backend-only and source-assertion tests cannot.
"""

import pytest


def _send_run(page, text: str) -> None:
    page.fill("#input", text)
    page.click("#sendBtn")


def _wait_for_selector(page, selector: str, timeout: int = 10_000) -> None:
    page.wait_for_selector(selector, timeout=timeout)


def test_page_loads_with_usable_composer(app):
    """The composer unlocks after seeding a default model."""
    select = app.locator("#composerSelect")
    assert select.is_enabled()
    label = select.inner_text()
    assert label.strip() != "选择模型"  # a model is selected
    assert app.locator("#input").is_enabled()


def test_console_has_no_app_js_errors(app):
    """No application JavaScript errors during load and interaction."""
    app.locator("#composerSelect").click()
    app.keyboard.press("Escape")
    app.locator("#input").click()
    app.locator("#input").press("Escape")
    app.wait_for_timeout(500)
    app_errors = [e for e in app.errors if "extension" not in e.lower()]
    assert app_errors == [], f"console errors: {app_errors}"


def test_user_message_appears_in_timeline(app):
    """Typing a message renders a user bubble and unlocks the run control."""
    app.locator("#input").fill("hello browser")
    app.locator("#input").press("Enter")
    # The approval fixture turns every run into an approval request; the user
    # bubble appears before the card, so assert on it directly.
    _wait_for_selector(app, "#chatArea .msg.user", timeout=10_000)
    user_bubble = app.locator("#chatArea .msg.user").first.inner_text()
    assert "hello browser" in user_bubble
    assert app.locator("#runControl").is_visible()


def test_composer_model_menu_lists_seeded_model(app):
    """The composer menu lists the seeded model and enhanced modes."""
    app.locator("#composerSelect").click()
    _wait_for_selector(app, "#composerMenu:not([hidden])")
    assert app.locator("#composerMenu [data-model-id]").count() >= 1
    modes = app.locator("#composerMenu [data-mode]").all_inner_texts()
    assert any("MOA" in m for m in modes)
    assert any("Peri" in m for m in modes)


def test_approval_deny_writes_nothing(app, server_proc):
    """Denying the approval card leaves the controlled workspace untouched."""
    workspace = server_proc["workspace"]
    assert not (workspace / "approval-proof.txt").exists()

    _send_run(app, "deny me")
    _wait_for_selector(app, "[data-approval-id]")
    app.locator('[data-approval-decision="deny"]').first.click()
    # A denied approval does not produce a run-completion card; the card flips
    # to the denied state and the workspace stays clean.
    _wait_for_selector(app, '.approval-card[data-state="denied"]', timeout=12_000)

    assert not (workspace / "approval-proof.txt").exists()


def test_approval_allow_writes_controlled_file(app, server_proc):
    """Allowing the approval card writes only inside the controlled workspace."""
    workspace = server_proc["workspace"]
    assert not (workspace / "approval-proof.txt").exists()

    _send_run(app, "allow me")
    _wait_for_selector(app, "[data-approval-id]")
    app.locator('[data-approval-decision="approve"]').first.click()
    _wait_for_selector(app, ".run-completion", timeout=12_000)

    assert (workspace / "approval-proof.txt").exists()
    assert "allow me" in (workspace / "approval-proof.txt").read_text()


def test_skill_attachment_chip_and_send_carry_skill_id(app, seed_skill_on_page):
    """Attaching a skill via @ shows a chip; sending consumes it.

    The outbound skill_id payload is asserted at the unit level
    (test_skill_attachment.py); this browser case proves the real UI chain:
    @ picker → chip appears → chip shows the skill name.
    """
    seed_skill_on_page(name="review-code", prompt="只输出可验证的审查结论。")
    app.locator("#input").fill("@")
    _wait_for_selector(app, "#skillAtMenu:not([hidden])")
    _wait_for_selector(app, '[data-skill-at="review-code"]')
    app.locator('[data-skill-at="review-code"]').click()

    # The chip appears with the skill name attached.
    _wait_for_selector(app, "#skillChip:not([hidden])")
    chip_text = app.locator("#skillChipLabel").inner_text()
    assert "review-code" in chip_text

    # Removing the chip is the inverse path: pendingSkillId clears and the chip
    # hides, proving the attachment state machine round-trips in the browser.
    app.locator("#skillChipRemove").click()
    assert app.locator("#skillChip").is_hidden()


def test_memory_settings_panel_adds_and_lists_memory(app):
    """The Settings → 记忆 panel adds a memory via the live WebSocket."""
    app.evaluate("() => { openSettings('memory'); }")
    app.wait_for_selector("#memoryFact:visible", timeout=10_000)
    app.locator("#memoryFact").fill("E2E 记忆：项目使用 pytest")
    app.locator("#memoryCategory").select_option("fact")
    app.locator("#memoryAddBtn").click()
    # The list re-renders with the persisted memory after the memory_added ack.
    app.wait_for_function(
        "() => (document.getElementById('memoryList')?.innerText || '').includes('E2E 记忆')",
        timeout=10_000,
    )
    app.locator("#memoryClearBtn").click()


def test_sandbox_readiness_button_visible_in_peri_settings(app):
    """The Peri settings expose the git-readiness check for writable workers."""
    app.evaluate("() => { openSettings('peri'); }")
    app.wait_for_selector("#periReadinessBtn", timeout=10_000)
    assert app.locator("#periReadinessBtn").is_visible()
    app.locator("#periReadinessBtn").click()
    # The readiness panel renders a result (ready or blocked) without JS errors.
    app.wait_for_function(
        "() => (document.getElementById('periReadiness')?.textContent || '').trim().length > 0",
        timeout=10_000,
    )