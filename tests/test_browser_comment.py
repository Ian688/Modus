"""Browser element annotation backend (Phase A2): browser_comment handler.

The frontend picks elements in the preview iframe and posts a browser_comment;
the handler validates the payload (bounded items, loopback-only URL) then
forwards it through the normal run_message pipeline so the annotations reach the
LLM as the next user turn.
"""

from __future__ import annotations

import json

import pytest

from modus.desktop import db
from modus.desktop.server import _handle_browser_comment, _require_loopback_preview


def _session() -> str:
    return db.create_session("browser-comment")["id"]


def _payload(**overrides) -> dict:
    base = {
        "type": "browser_comment",
        "url": "http://localhost:3000/",
        "items": [{"selector": "aside#sidebar", "tag": "aside", "text": "菜单", "annotation": "太窄了"}],
    }
    base.update(overrides)
    return base


def test_require_loopback_preview_accepts_localhost():
    assert _require_loopback_preview("http://localhost:3000/") == "http://localhost:3000/"
    assert _require_loopback_preview("http://127.0.0.1:8000/") == "http://127.0.0.1:8000/"


def test_require_loopback_preview_rejects_remote():
    for url in ("http://example.com/", "https://evil.com/x", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            _require_loopback_preview(url)


@pytest.mark.asyncio
async def test_browser_comment_rejects_empty_items():
    sent = []

    class FakeWS:
        async def send_json(self, payload):
            sent.append(payload)

    session = type("S", (), {"owner_id": ""})()
    await _handle_browser_comment(FakeWS(), session, _payload(items=[]))
    assert sent and sent[0]["code"] == "invalid_browser_comment"


@pytest.mark.asyncio
async def test_browser_comment_rejects_too_many_items():
    sent = []
    items = [{"selector": f"x{i}"} for i in range(21)]

    class FakeWS:
        async def send_json(self, payload):
            sent.append(payload)

    session = type("S", (), {"owner_id": ""})()
    await _handle_browser_comment(FakeWS(), session, _payload(items=items))
    assert sent and sent[0]["code"] == "invalid_browser_comment"


@pytest.mark.asyncio
async def test_browser_comment_rejects_remote_url():
    sent = []

    class FakeWS:
        async def send_json(self, payload):
            sent.append(payload)

    session = type("S", (), {"owner_id": ""})()
    await _handle_browser_comment(FakeWS(), session, _payload(url="http://evil.com/"))
    assert sent and sent[0]["code"] == "invalid_browser_comment"


def test_browser_comment_content_formatting():
    """The formatted comment text carries selectors, text, and annotations."""
    items = [
        {"selector": "aside#sidebar", "tag": "aside", "text": "菜单", "annotation": "太窄了"},
        {"selector": "button#submit", "tag": "button", "text": "提交", "annotation": ""},
    ]
    from modus.desktop.server import _handle_browser_comment

    # Build the same content the handler would forward by extracting the logic.
    # The handler delegates to _handle_explicit_run_message, so we verify the
    # formatting by reading what a run_message would receive via a stub.
    forwarded = {}

    async def _fake_run_message(ws, session, message):
        forwarded["content"] = message["content"]

    import modus.desktop.server as server_mod

    old = server_mod._handle_explicit_run_message
    server_mod._handle_explicit_run_message = _fake_run_message
    try:
        import asyncio

        asyncio.run(server_mod._handle_browser_comment(
            type("WS", (), {"send_json": lambda self, p: None})(),
            type("S", (), {"owner_id": ""})(),
            _payload(items=items),
        ))
    finally:
        server_mod._handle_explicit_run_message = old

    content = forwarded.get("content", "")
    assert "[浏览器元素点评]" in content
    assert "页面: http://localhost:3000/" in content
    assert "aside#sidebar" in content
    assert "太窄了" in content
    assert "button#submit" in content


def test_browser_comment_carries_image_attachments():
    """Element screenshots flow through to the run_message attachments."""
    import asyncio
    import modus.desktop.server as server_mod

    forwarded = {}

    async def _fake_run_message(ws, session, message):
        forwarded.update(message)

    old = server_mod._handle_explicit_run_message
    server_mod._handle_explicit_run_message = _fake_run_message
    try:
        asyncio.run(server_mod._handle_browser_comment(
            type("WS", (), {"send_json": lambda self, p: None})(),
            type("S", (), {"owner_id": ""})(),
            _payload(items=[
                {"selector": "aside#sidebar", "text": "菜单", "annotation": "太窄",
                 "image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="},
                {"selector": "button#x", "text": "x", "annotation": "",
                 "image": "not-a-data-uri"},  # rejected: not whitelisted
            ]),
        ))
    finally:
        server_mod._handle_explicit_run_message = old

    attachments = forwarded.get("attachments", [])
    assert len(attachments) == 1
    assert attachments[0]["kind"] == "image"
    assert attachments[0]["content"].startswith("data:image/png;base64,")
