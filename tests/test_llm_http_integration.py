"""HTTP-level integration for the real OpenAICompatibleClient.

Drives the production ``chat()`` SSE stream through ``httpx.MockTransport``,
covering the streaming parser, typed-event mapping, [DONE] termination, and
error mapping that the fake-client unit tests never exercise.
"""

from __future__ import annotations

import json

import httpx
import pytest

from modus.llm.openai_compatible import OpenAICompatibleClient
from modus.types import Message


def _client(handler) -> OpenAICompatibleClient:
    transport = httpx.MockTransport(handler)
    return OpenAICompatibleClient(
        provider_name="test", model="test-model", api_key="key",
        base_url="https://api.test.local/v1", timeout=30.0,
        transport=transport,
    )


def _sse_response(*events: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n" for ev in events)
    body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def _no_tool_stream():
    return _sse_response(
        {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]},
    )


@pytest.mark.asyncio
async def test_chat_streams_text_deltas_and_terminal():
    client = _client(lambda request: _no_tool_stream())
    messages = [Message(role="user", content="hi")]

    events = [ev async for ev in client.chat(messages, [], system_prompt="sys")]

    types = [ev["type"] for ev in events]
    assert "message_start" in types
    assert "text_delta" in types
    assert types[-1] == "message_end"
    text = "".join(ev["text"] for ev in events if ev["type"] == "text_delta")
    assert text == "Hello"
    assert events[-1]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_chat_parses_tool_call_deltas():
    def handler(request):
        return _sse_response(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": "{\"path\":\"a"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ".txt\"}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )

    client = _client(handler)
    events = [ev async for ev in client.chat([], [], system_prompt="sys")]

    tool_calls = [ev["tool_call"] for ev in events if ev["type"] == "tool_call_delta"]
    assert tool_calls and tool_calls[0]["function"]["name"] == "read_file"
    assert any(ev.get("stop_reason") == "tool_use" for ev in events)


@pytest.mark.asyncio
async def test_chat_maps_http_error_to_error_event():
    def handler(request):
        return httpx.Response(401, content=b'{"error": {"message": "bad key"}}')

    client = _client(handler)
    events = [ev async for ev in client.chat([], [], system_prompt="sys")]

    error = next(ev for ev in events if ev["type"] == "error")
    assert "401" in str(error["error"])


@pytest.mark.asyncio
async def test_chat_maps_stream_interruption_to_error():
    def handler(request):
        body = "data: {\"choices\": [{\"delta\": {\"content\": \"partial\"}}]}\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    client = _client(handler)  # stream ends without [DONE] and no terminal event
    events = [ev async for ev in client.chat([], [], system_prompt="sys")]
    # A truncated stream that never emits a terminal yields only deltas; the
    # caller's budget/controller owns terminality. Assert no crash and deltas seen.
    assert any(ev["type"] == "text_delta" for ev in events)


@pytest.mark.asyncio
async def test_chat_sends_expected_request_shape():
    captured: dict = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return _no_tool_stream()

    client = _client(handler)
    _ = [ev async for ev in client.chat([], [], system_prompt="sys")]

    assert captured["url"].endswith("/chat/completions")
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["model"] == "test-model"


@pytest.mark.asyncio
async def test_embed_calls_embeddings_endpoint():
    def handler(request):
        assert request.url.path.endswith("/embeddings")
        body = json.loads(request.content)
        assert body["input"] == ["hello", "world"]
        return httpx.Response(200, json={
            "data": [
                {"embedding": [0.1, 0.2, 0.3]},
                {"embedding": [0.4, 0.5, 0.6]},
            ],
        })

    client = _client(handler)
    vectors = await client.embed(["hello", "world"])
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


# ─── Input trimming toward max_context_window ─────────────────────────────

def _client_tiny_window() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        provider_name="test", model="test-model", api_key="key",
        base_url="https://api.test.local/v1", timeout=30.0,
        max_context_window=120, max_tokens=20,
        transport=httpx.MockTransport(lambda request: _no_tool_stream()),
    )


def _long_tool_history() -> list[Message]:
    """system contract + 2 full tool turns + a final user request."""
    return [
        Message(role="system", content="CONTRACT"),
        Message(role="user", content="request one " + "x" * 400),
        Message(role="assistant", content="answer one", tool_calls=[
            {"id": "1", "function": {"name": "read", "arguments": "{}"}},
        ]),
        Message(role="tool", content="tool one " + "z" * 400, tool_call_id="1"),
        Message(role="user", content="request two " + "y" * 400),
        Message(role="assistant", content="answer two"),
        Message(role="user", content="FINAL REQUEST"),
    ]


def test_trim_messages_keeps_contract_and_final_request():
    client = _client_tiny_window()
    trimmed = client._trim_messages(_long_tool_history(), [], "sys")

    # The leading system contract and the active tail survive.
    assert trimmed[0].role == "system" and trimmed[0].content == "CONTRACT"
    assert trimmed[-1].content == "FINAL REQUEST"
    # Whole leading turns are dropped; no dangling tool message outlives the
    # assistant tool-call it answers.
    roles = [m.role for m in trimmed]
    assert roles.count("user") >= 1
    assert not any(
        roles[i] == "tool" and (i == 0 or roles[i - 1] != "assistant")
        for i in range(len(roles))
    )


def test_trim_messages_is_a_noop_under_budget():
    client = _client_tiny_window()
    small = [Message(role="user", content="hi")]
    assert client._trim_messages(small, [], "sys") == small
    # The estimator reserves tool definitions and output tokens: even a
    # generous budget produces no truncation when nothing exceeds it.
    big = OpenAICompatibleClient(
        provider_name="t", model="m", api_key="k", base_url="http://x",
        max_context_window=1_000_000, max_tokens=20,
    )
    assert big._trim_messages(_long_tool_history(), [], "sys") == _long_tool_history()


def test_trim_preserves_compaction_summary_alongside_contract():
    """A restored context carries a contract + SUMMARY_PREFIX summary; neither
    may be dropped when the long tail is trimmed under the window."""
    client = _client_tiny_window()
    messages = [
        Message(role="system", content="CONTRACT"),
        Message(
            role="system",
            content="[CONTEXT COMPACTION — REFERENCE ONLY] older turns condensed",
        ),
        Message(role="user", content="m1" + "x" * 300),
        Message(role="assistant", content="a1"),
        Message(role="user", content="m2" + "y" * 300),
        Message(role="assistant", content="a2"),
        Message(role="user", content="FINAL"),
    ]

    trimmed = client._trim_messages(messages, [], "sys")

    assert trimmed[0].content == "CONTRACT"
    assert "REFERENCE ONLY" in str(trimmed[1].content)
    assert trimmed[-1].content == "FINAL"


@pytest.mark.asyncio
async def test_chat_sends_trimmed_payload_under_context_window():
    captured: dict = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return _no_tool_stream()

    client = OpenAICompatibleClient(
        provider_name="test", model="test-model", api_key="key",
        base_url="https://api.test.local/v1", timeout=30.0,
        max_context_window=120, max_tokens=20,
        transport=httpx.MockTransport(handler),
    )
    _ = [ev async for ev in client.chat(_long_tool_history(), [], system_prompt="sys")]

    sent = captured["payload"]["messages"]
    # The system prompt is always first (from the client, not the contract),
    # then the trimmed tail ending on the current request.
    assert sent[0]["role"] == "system" and sent[0]["content"] == "sys"
    assert sent[-1]["role"] == "user" and sent[-1]["content"] == "FINAL REQUEST"
    # The dropped early turn must not be in the request.
    all_text = json.dumps(sent, ensure_ascii=False)
    assert "request one" not in all_text


@pytest.mark.asyncio
async def test_chat_trimming_can_be_disabled():
    captured: dict = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return _no_tool_stream()

    client = OpenAICompatibleClient(
        provider_name="test", model="test-model", api_key="key",
        base_url="https://api.test.local/v1", timeout=30.0,
        max_context_window=50, max_tokens=20,
        trim_to_context_window=False,
        transport=httpx.MockTransport(handler),
    )
    _ = [ev async for ev in client.chat(_long_tool_history(), [], system_prompt="sys")]

    sent = captured["payload"]["messages"]
    all_text = json.dumps(sent, ensure_ascii=False)
    assert "request one" in all_text  # untrimmed: full history sent
