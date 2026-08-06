"""Attached-context (project/file/url) plumbing for run submissions.

The context bar attaches items that ride the run_message payload as a
``context`` array.  The server cleans/limits it, turns each item into a
deliberate system prompt (Agent decides how to use it — no cwd switch, no
pre-loading), folds it into the retry fingerprint, and injects the prompts
into the default/MOA/Peri runners.
"""

import pytest

from modus.desktop import server
from modus.types import Message


def test_clean_attached_context_filters_invalid_entries() -> None:
    cleaned = server._clean_attached_context([
        {"kind": "project", "label": "A", "value": "/a"},
        {"kind": "file", "label": "B", "value": "b.py"},
        {"kind": "url", "label": "C", "value": "https://example.com"},
        {"kind": "image", "label": "shot.png", "value": "shot.png",
         "thumb": "data:image/png;base64,AAAA"},
        {"kind": "url", "label": "D", "value": "not-a-url"},   # dropped: no scheme
        {"kind": "bogus", "label": "E", "value": "/e"},        # dropped: bad kind
        {"kind": "project", "label": "A", "value": "/a"},      # dropped: duplicate
        "not-a-dict",                                          # dropped
    ])
    assert cleaned == [
        {"kind": "project", "label": "A", "value": "/a"},
        {"kind": "file", "label": "B", "value": "b.py"},
        {"kind": "url", "label": "C", "value": "https://example.com"},
        {"kind": "image", "label": "shot.png", "value": "shot.png",
         "thumb": "data:image/png;base64,AAAA"},
    ]


def test_clean_attached_context_rejects_unsafe_image_thumbnails() -> None:
    cleaned = server._clean_attached_context([
        {"kind": "image", "label": "safe", "value": "safe.png",
         "thumb": "data:image/webp;base64,AAAA"},
        {"kind": "image", "label": "unsafe", "value": "unsafe.svg",
         "thumb": "data:image/svg+xml,<svg/>"},
    ])
    assert cleaned[0]["thumb"] == "data:image/webp;base64,AAAA"
    assert "thumb" not in cleaned[1]


def test_clean_attached_context_carries_file_content() -> None:
    cleaned = server._clean_attached_context([
        {"kind": "file", "label": "main.py", "value": "/proj/main.py", "content": "def main(): pass"},
        {"kind": "project", "label": "P", "value": "/p", "content": "should-be-ignored"},
        {"kind": "file", "label": "huge", "value": "/h", "content": "x" * (server.MAX_ATTACHED_FILE_CHARS + 50)},
        {"kind": "file", "label": "empty", "value": "/e", "content": "   "},
    ])
    assert cleaned[0]["content"] == "def main(): pass"
    assert "content" not in cleaned[1]  # content only kept for files
    assert len(cleaned[2]["content"]) == server.MAX_ATTACHED_FILE_CHARS
    assert "content" not in cleaned[3]  # whitespace-only content dropped


def test_clean_attached_context_bounds_and_count() -> None:
    # Empty / non-list / oversized are rejected wholesale.
    assert server._clean_attached_context(None) == []
    assert server._clean_attached_context([]) == []
    assert server._clean_attached_context("x") == []
    many = [
        {"kind": "file", "label": f"f{i}", "value": f"/f{i}.py"}
        for i in range(server.MAX_ATTACHED_CONTEXT + 5)
    ]
    # Oversized context is rejected wholesale (defensive: don't guess which to drop).
    assert server._clean_attached_context(many) == []
    # Label/value are bounded.
    long_item = {"kind": "file", "label": "L" * 500, "value": "V" * 5000}
    cleaned = server._clean_attached_context([long_item])
    assert cleaned[0]["label"] == "L" * 200
    assert cleaned[0]["value"] == "V" * 2000


def test_attached_context_messages_builds_deliberate_system_prompts() -> None:
    msgs = server._attached_context_messages([
        {"kind": "project", "label": "Proj", "value": "/proj"},
        {"kind": "file", "label": "main.py", "value": "/proj/main.py"},
        {"kind": "url", "label": "docs", "value": "https://example.com"},
    ])
    assert len(msgs) == 3
    assert all(m.role == "system" for m in msgs)
    assert "[Attached context — project]" in msgs[0].content
    assert "read_file" in msgs[1].content and "not pre-loaded" in msgs[1].content
    assert "web_fetch" in msgs[2].content and "not pre-loaded" in msgs[2].content
    # No content is pre-loaded: the text must not include the file body or URL body.
    assert "/proj/main.py" in msgs[1].content  # path is advisory, not content


def test_attached_context_messages_inlines_client_file_content() -> None:
    msgs = server._attached_context_messages([
        {"kind": "file", "label": "main.py", "value": "/proj/main.py",
         "content": "def main():\n    return 42\n"},
    ])
    # The wrapper names the file and instructs the Agent; the body ships in a
    # separate user message so the model sees the real content.
    assert len(msgs) == 2
    assert msgs[0].role == "system"
    assert "inlined below" in msgs[0].content and "main.py" in msgs[0].content
    assert msgs[1].role == "user"
    assert msgs[1].content == "def main():\n    return 42\n"
    # A path-only attachment keeps the old advisory prompt, no body message.
    solo = server._attached_context_messages([
        {"kind": "file", "label": "x", "value": "x.py"},
    ])
    assert len(solo) == 1
    assert "not pre-loaded" in solo[0].content


def test_fingerprint_is_order_independent_but_context_sensitive() -> None:
    ctx_a = [
        {"kind": "project", "label": "A", "value": "/a"},
        {"kind": "file", "label": "B", "value": "b.py"},
    ]
    ctx_a_shuffled = [
        {"kind": "file", "label": "B", "value": "b.py"},
        {"kind": "project", "label": "A", "value": "/a"},
    ]
    base = dict(content="task", skill_id="", requested_db_id="db1")
    f0 = server._run_submission_fingerprint(**base)
    f1 = server._run_submission_fingerprint(**base, context=ctx_a)
    f2 = server._run_submission_fingerprint(**base, context=ctx_a_shuffled)
    f3 = server._run_submission_fingerprint(**base, context=[])
    assert f1 == f2          # reordering chips is the same submission
    assert f0 == f3          # empty context == no context
    assert f1 != f0          # different context → different fingerprint


def test_fingerprint_distinguishes_inlined_file_content() -> None:
    base = dict(content="task", skill_id="", requested_db_id="db1")
    ctx_no_content = [{"kind": "file", "label": "b", "value": "b.py"}]
    ctx_content = [{"kind": "file", "label": "b", "value": "b.py", "content": "x = 1"}]
    ctx_other = [{"kind": "file", "label": "b", "value": "b.py", "content": "x = 2"}]
    a = server._run_submission_fingerprint(**base, context=ctx_no_content)
    b = server._run_submission_fingerprint(**base, context=ctx_content)
    c = server._run_submission_fingerprint(**base, context=ctx_other)
    assert a != b            # inlining content changes the submission
    assert b != c            # different file content → distinct submission


def test_preallocated_submission_default_injects_context_messages(monkeypatch) -> None:
    captured: dict = {}

    async def fake_stream(ws, session, content, **kwargs):
        captured["transient_context"] = kwargs.get("transient_context")
        return None

    monkeypatch.setattr(server, "_stream_to_ws", fake_stream)
    fake_session = type("S", (), {"db_id": None})()

    async def run() -> None:
        await server._run_preallocated_submission(
            object(), fake_session, "task", "",
            mode="default", emitter=object(), controller=object(),
            context_messages=[Message(role="system", content="[Attached context — project] X")],
        )

    import asyncio
    asyncio.run(run())
    ctx = captured["transient_context"]
    assert ctx is not None
    assert any("[Attached context" in (getattr(m, "content", "") or "") for m in ctx)


def test_preallocated_submission_without_context_omits_kwarg(monkeypatch) -> None:
    """Empty context must not pass the kwarg (compat with exact-signature fakes)."""
    captured: dict = {}

    async def fake_stream(ws, session, content, **kwargs):
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr(server, "_stream_to_ws", fake_stream)
    fake_session = type("S", (), {"db_id": None})()

    async def run() -> None:
        await server._run_preallocated_submission(
            object(), fake_session, "task", "",
            mode="default", emitter=object(), controller=object(),
        )

    import asyncio
    asyncio.run(run())
    # _stream_to_ws still receives transient_context=None (skill path), but the
    # context_messages kwarg itself is never forced into runner signatures.
    assert "context_messages" not in captured["kwargs"]
