"""Interactive ```choice cards: Agent offers clickable options when unsure.

The Agent emits a fenced ```choice block (one option per line) when the user's
intent is ambiguous. The frontend renders it as a clickable card; clicking an
option sends a run_message and collapses the card to a "已选择" summary.
"""

from pathlib import Path

from _bundle import css_bundle, js_bundle
from modus.config import ModusConfig
from modus.prompt import PromptAssembler


def test_choice_block_renders_clickable_cards() -> None:
    page = js_bundle()
    # markdown.js intercepts ```choice fenced blocks and emits interactive cards.
    assert 'lang === "choice"' in page
    assert "function choiceCardHtml(lines, chosen)" in page
    assert 'data-choice-card' in page
    assert 'class="choice-btn"' in page
    assert "class=\"choice-chosen\"" in page
    # websocket.js submits the clicked option as a run_message.
    assert "function submitChoice" in page
    assert "card.dataset.state === \"chosen\"" in page
    # timeline.js wires the button click through addCopyHandlers.
    assert "[data-choice-card] .choice-btn" in page


def test_choice_card_css_lives_in_page_style() -> None:
    page = css_bundle()
    assert ".choice-card {" in page
    assert ".choice-btn" in page
    assert ".choice-hint" in page
    assert ".choice-chosen" in page


def test_choice_prompt_guidance_in_assembler() -> None:
    prompt = PromptAssembler(
        config=ModusConfig(),
        cwd=".",
        tool_names=["read_file", "write_file"],
        model="test-model",
        provider="test-provider",
    ).build()
    assert "```choice" in prompt
    assert "do not guess" in prompt.lower()


def test_choice_prompt_guidance_in_peri_merge_only() -> None:
    from modus.desktop import peri

    assert "```choice" in peri.HOST_MERGE_PROMPT
    # HOST_ROLE is reused by JSON-only decompose/quality prompts and SUB_ROLE is
    # an intermediate worker prompt — neither should carry the choice guidance.
    assert "```choice" not in peri.HOST_ROLE
    assert "```choice" not in peri.SUB_ROLE


def test_moa_reference_and_aggregator_do_not_carry_choice() -> None:
    from modus.agent import moa

    # Reference advisors and the aggregator produce intermediate guidance, not
    # the user-facing answer, so they must not be steered to emit choice cards.
    assert "```choice" not in moa.REFERENCE_ROLE
    assert "```choice" not in moa.AGGREGATOR_PROMPT


def test_streaming_choice_block_falls_back_to_plain_text() -> None:
    page = js_bundle()
    # A half-closed ```choice block (odd fence count) must not render an
    # interactive card before the closing fence arrives.
    assert 'if (lang === "choice") return renderMd(source)' in page
