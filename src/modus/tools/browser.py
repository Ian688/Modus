"""Browser operation tools: playwright-driven headless Chrome for the agent.

Gives the Agent a persistent, scriptable browser (Phase A1 of the personal-PC
blueprint).  The human side previews the same URL in the desktop iframe
(``/api/preview``); the CSS selector is the cross-surface contract — the human
picks an element and annotates it, the Agent reproduces it with
``browser_click(selector)`` / ``browser_extract(selector)`` to debug.

Design notes:

- **Single shared BrowserContext**: ``navigate → click → screenshot`` needs state
  continuity across tool calls, so one module-level holder + ``asyncio.Lock``
  is reused instead of launching a browser per call.  The playwright process is
  in-process asyncio state and dies with the Modus server; it is NOT registered
  in the ``process_tools`` disk registry (that is for OS subprocesses the agent
  must survive across, e.g. dev servers).
- **Lazy import + fallback**: playwright is a dev dependency.  If it is missing
  or ``channel="chrome"`` fails, fall back to bundled chromium, and fail with a
  clear message only when nothing is available.
- **browser_eval is the only high-risk tool**: arbitrary JS execution, so it is
  ``danger_level="high" + requires_approval=True``.  A best-effort blocklist
  rejects exfiltration APIs (fetch/WebSocket/XMLHttpRequest/sendBeacon) as a
  first line; approval is the authoritative gate.
- **Screenshots go through the artifact pipeline** (``_persist_tool_result``),
  never as base64 in ``content`` — the model gets a bounded payload, the UI
  renders the artifact.
- **Every tool is ``is_concurrency_safe=False``** so the executor serializes
  calls against the shared page (the executor's read-concurrency path would
  otherwise race it).
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from modus.tools.base import ToolContext, ToolResult

# A blocklist of exfiltration APIs for browser_eval, applied as a first line.
# The approval gate is authoritative; this is belt-and-suspenders for the case
# where a grant set or hitl mode lets the call through unapproved.
_EVAL_BLOCKLIST = ("fetch(", "XMLHttpRequest", "WebSocket(", "sendBeacon(", "navigator.sendBeacon")
_EVAL_MAX_CHARS = 4096

_browser_lock = asyncio.Lock()
_holder: dict[str, Any] | None = None  # {"pw","browser","context","page"}


async def _ensure_browser() -> Any:
    """Return the shared Page, launching headless Chrome on first use."""
    global _holder
    async with _browser_lock:
        if _holder is not None:
            return _holder["page"]
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright 未安装：请运行 `pip install -e '.[dev]'` 或 `uv sync --group dev`"
            ) from exc
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            # System Chrome missing; fall back to playwright's bundled chromium.
            browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        _holder = {"pw": pw, "browser": browser, "context": context, "page": page}
        return page


async def _close_browser() -> None:
    global _holder
    async with _browser_lock:
        if _holder is None:
            return
        try:
            await _holder["browser"].close()
        except Exception:
            pass
        try:
            await _holder["pw"].stop()
        except Exception:
            pass
        _holder = None


def _bound_eval_source(js: str) -> None:
    """Reject exfiltration-shaped eval sources before they reach the page."""
    for token in _EVAL_BLOCKLIST:
        if token in js:
            raise ValueError(f"browser_eval rejected: {token} is blocked")


async def browser_navigate(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Navigate the shared browser to a URL and wait for the page to load."""
    url = str(payload.get("url") or "").strip()
    if not url:
        return ToolResult("browser_navigate requires a url", is_error=True)
    page = await _ensure_browser()
    try:
        await page.goto(url, wait_until="load", timeout=30000)
        title = await page.title()
    except Exception as exc:
        return ToolResult(f"browser_navigate failed: {exc}", is_error=True)
    return ToolResult(
        f"Navigated to {url} · {title}",
        metadata={
            "operation": "browser_navigate", "url": url, "title": title,
            # The desktop preview iframe reads this to open the same page.
            "preview_url": url,
        },
    )


async def browser_state(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Return the current URL, title, visible links, and input placeholders."""
    page = await _ensure_browser()
    try:
        url = page.url
        title = await page.title()
        links = await page.eval_on_selector_all(
            "a[href]", "els => els.slice(0,20).map(e => ({text:(e.innerText||e.textContent||'').trim().slice(0,60), href:e.href}))",
        )
        inputs = await page.eval_on_selector_all(
            "input, textarea, select",
            "els => els.slice(0,20).map(e => ({tag:e.tagName.toLowerCase(), type:e.type||'', placeholder:e.placeholder||'', name:e.name||''}))",
        )
    except Exception as exc:
        return ToolResult(f"browser_state failed: {exc}", is_error=True)
    lines = [f"URL: {url}", f"Title: {title}", "Links:", *[f"  {l['text']} → {l['href']}" for l in links], "Inputs:", *[f"  <{i['tag']}> {i.get('placeholder') or i.get('name') or i.get('type')}" for i in inputs]]
    return ToolResult(
        "\n".join(lines),
        metadata={"operation": "browser_state", "url": url, "link_count": len(links), "input_count": len(inputs)},
    )


async def browser_extract(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Extract text or attribute from all elements matching a CSS selector."""
    selector = str(payload.get("selector") or "").strip()
    if not selector:
        return ToolResult("browser_extract requires a selector", is_error=True)
    attr = str(payload.get("attr") or "").strip() or None
    limit = max(1, min(int(payload.get("limit") or 20), 100))
    page = await _ensure_browser()
    try:
        if attr:
            values = await page.eval_on_selector_all(
                selector, "els => els.map(e => e.getAttribute(arguments[0]))", attr,
            )
            rows = [v for v in values if v is not None][:limit]
            body = "\n".join(rows) if rows else "(no matches)"
        else:
            texts = await page.eval_on_selector_all(
                selector, "els => els.map(e => (e.innerText||e.textContent||'').trim())",
            )
            rows = [t for t in texts if t][:limit]
            body = "\n".join(rows) if rows else "(no matches)"
    except Exception as exc:
        return ToolResult(f"browser_extract failed: {exc}", is_error=True)
    return ToolResult(body, metadata={"operation": "browser_extract", "selector": selector, "match_count": len(rows) if 'rows' in locals() else 0})


async def browser_screenshot(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Capture a viewport screenshot and persist it as an artifact."""
    page = await _ensure_browser()
    try:
        png = await page.screenshot(full_page=False)
    except Exception as exc:
        return ToolResult(f"browser_screenshot failed: {exc}", is_error=True)
    b64 = base64.b64encode(png).decode("ascii")
    artifact = None
    if context.session_id and context.run_id:
        from modus.tools.builtins import _persist_tool_result

        artifact = _persist_tool_result(
            context, kind="screenshot", title=f"browser · {page.url}",
            content=f"data:image/png;base64,{b64}",
        )
    disclosure = {"local_bytes_read": len(png), "model_bytes_sent": 0, "raw_content_sent": False}
    if artifact is None:
        return ToolResult(
            f"Screenshot captured ({len(png)} bytes) — requires a persisted session to display",
            metadata={"operation": "browser_screenshot", "url": page.url},
            disclosure=disclosure,
        )
    return ToolResult(
        "Screenshot captured",
        metadata={"operation": "browser_screenshot", "url": page.url, "artifact_id": artifact.get("artifact_id")},
        artifacts=[artifact],
        model_payload=f"Screenshot captured: {page.url} ({len(png)} bytes)",
        disclosure=disclosure,
    )


async def browser_click(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Click the first element matching a CSS selector."""
    selector = str(payload.get("selector") or "").strip()
    if not selector:
        return ToolResult("browser_click requires a selector", is_error=True)
    page = await _ensure_browser()
    try:
        await page.click(selector, timeout=5000)
    except Exception as exc:
        return ToolResult(f"browser_click failed: {exc}", is_error=True)
    return ToolResult(f"Clicked {selector}", metadata={"operation": "browser_click", "selector": selector})


async def browser_type(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Type text into the first element matching a CSS selector."""
    selector = str(payload.get("selector") or "").strip()
    text = str(payload.get("text") or "")
    if not selector:
        return ToolResult("browser_type requires a selector", is_error=True)
    page = await _ensure_browser()
    try:
        await page.fill(selector, text, timeout=5000)
    except Exception as exc:
        return ToolResult(f"browser_type failed: {exc}", is_error=True)
    return ToolResult(f"Typed into {selector}", metadata={"operation": "browser_type", "selector": selector})


async def browser_eval(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Evaluate a JS expression in the page. High-risk: requires approval."""
    js = str(payload.get("js") or "").strip()
    if not js:
        return ToolResult("browser_eval requires a js expression", is_error=True)
    if len(js) > _EVAL_MAX_CHARS:
        return ToolResult(f"browser_eval js exceeds {_EVAL_MAX_CHARS} chars", is_error=True)
    try:
        _bound_eval_source(js)
    except ValueError as exc:
        return ToolResult(str(exc), is_error=True)
    page = await _ensure_browser()
    try:
        result = await page.evaluate(js)
    except Exception as exc:
        return ToolResult(f"browser_eval failed: {exc}", is_error=True)
    text = str(result)[:4000] if result is not None else "(null)"
    return ToolResult(text, metadata={"operation": "browser_eval", "url": page.url})


async def browser_close(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Close the shared browser and release its resources."""
    await _close_browser()
    return ToolResult("Browser closed", metadata={"operation": "browser_close"})
