"""Semantic ```summary / ```insight blocks: tone-colored closing summaries and
Agent-authored insight cards.

The Agent closes a finished task with a fenced ```summary block (fence-line
tone: success|warn|error|info, one `- ` bullet per key point) and flags a
recommendation with a fenced ```insight block (single-line takeaway). The
frontend renders both as pure-presentation cards (no interactivity); the
summary card's tone drives the status color/icon, the insight card is purple
accented and labelled as the Agent's own view.
"""

from pathlib import Path

from _bundle import css_bundle, js_bundle
from modus.config import ModusConfig
from modus.prompt import PromptAssembler


def test_summary_and_insight_blocks_render_cards() -> None:
    page = js_bundle()
    # markdown.js intercepts ```summary / ```insight fenced blocks.
    assert 'lang === "summary"' in page
    assert 'lang === "insight"' in page
    assert "function summaryCardHtml" in page
    assert "function insightCardHtml" in page
    assert 'data-tone="' in page
    assert "sem-pt-ic" in page
    # Both cards are pure presentation: the semantic-card builders must not
    # emit the interactive choice/approval markup.
    for fn in ("function summaryCardHtml", "function insightCardHtml"):
        start = page.index(fn)
        end = page.index("\nfunction ", start + 1) if "\nfunction " in page[start + 1:] else len(page)
        body = page[start:end]
        assert "data-choice-card" not in body
        assert "choice-btn" not in body
    # Localized labels for the success summary and the insight card.
    assert "Agent 见解" in page
    assert "完成总结" in page


def test_semantic_card_css_lives_in_page_style() -> None:
    page = css_bundle()
    assert ".sem-card {" in page
    assert ".sem-hd {" in page
    assert ".sem-pts li {" in page
    assert ".sem-insight {" in page
    assert '.sem-summary[data-tone="warn"] {' in page


def test_semantic_prompt_guidance_in_assembler() -> None:
    prompt = PromptAssembler(
        config=ModusConfig(),
        cwd=".",
        tool_names=["read_file", "write_file"],
        model="test-model",
        provider="test-provider",
    ).build()
    assert "```summary" in prompt
    assert "```insight" in prompt
    assert "success, warn, error, or info" in prompt


def test_semantic_prompt_guidance_in_peri_merge_only() -> None:
    from modus.desktop import peri

    assert "```summary" in peri.HOST_MERGE_PROMPT
    assert "```insight" in peri.HOST_MERGE_PROMPT
    # HOST_ROLE is reused by JSON-only decompose/quality prompts and SUB_ROLE is
    # an intermediate worker prompt — neither should carry the guidance.
    assert "```summary" not in peri.HOST_ROLE
    assert "```summary" not in peri.SUB_ROLE


def test_semantic_guidance_not_in_moa_intermediates() -> None:
    from modus.agent import moa

    # Reference advisors and the aggregator produce intermediate guidance, not
    # the user-facing answer, so they must not be steered to emit semantic blocks.
    assert "```summary" not in moa.REFERENCE_ROLE
    assert "```summary" not in moa.AGGREGATOR_PROMPT


def test_streaming_semantic_block_falls_back_to_plain_text() -> None:
    page = js_bundle()
    # A half-closed ```summary/```insight block (odd fence count) must not render
    # a card before the closing fence arrives; the fence line may still read
    # "summary success" so the guard splits on the first word.
    assert 'if (semWord === "summary" || semWord === "insight") return renderMd(source)' in page


def test_structured_steps_and_plan_blocks_render_cards() -> None:
    from pathlib import Path

    PAGE = Path(__file__).resolve().parent.parent / "src/modus/desktop/static"
    src = (PAGE / "markdown.js").read_text(encoding="utf-8")
    assert 'lang === "steps"' in src
    assert 'lang === "plan"' in src
    assert "function stepsCardHtml(code)" in src
    assert "function planCardHtml(code)" in src
    assert "sem-steps-list" in src
    assert "sem-plan-phase" in src


def test_structured_prompt_guidance_in_assembler() -> None:
    from modus.config import ModusConfig
    from modus.prompt import PromptAssembler

    prompt = PromptAssembler(
        config=ModusConfig(), cwd=".", tool_names=["bash"], model="test", provider="test",
    ).build()
    assert "```plan" in prompt
    assert "```steps" in prompt
