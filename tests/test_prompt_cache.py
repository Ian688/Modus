"""Prompt-cache breakpoint engineering (Wave2 C1).

Covers ``split_system_blocks`` / ``apply_cache_to_messages`` and the client /
assembler integration: static block cached, dynamic block not, first + last
user messages breakpointed, and trimming preserving the cached system head.
"""

from __future__ import annotations

from modus.config import ModusConfig
from modus.llm.cache import (
    DEFAULT_BREAKPOINTS,
    DYNAMIC_BOUNDARY,
    STATIC_BOUNDARY,
    apply_cache_to_messages,
    split_system_blocks,
    strip_system_markers,
)
from modus.llm.openai_compatible import OpenAICompatibleClient
from modus.prompt import PromptAssembler
from modus.types import Message


# ─── split_system_blocks ───────────────────────────────────────────────────

def test_split_system_blocks_boundary_markers():
    static = "You are Modus. Use tools to inspect files."
    dynamic = "Current time: 2026-08-08T12:00:00"
    prompt = f"{static}\n{STATIC_BOUNDARY}\n{DYNAMIC_BOUNDARY}\n{dynamic}"

    split_static, split_dynamic = split_system_blocks(prompt)

    assert split_static == static
    assert split_dynamic == dynamic
    assert "Current time" not in split_static
    assert "You are Modus" not in split_dynamic


def test_split_system_blocks_no_marker_is_fully_static():
    split_static, split_dynamic = split_system_blocks("plain prompt")
    assert split_static == "plain prompt"
    assert split_dynamic == ""


def test_split_system_blocks_ignores_marker_words_in_dynamic():
    # A dynamic block that mentions the marker word must not be split further.
    static = "role block"
    dynamic = "Working directory: /tmp/__MODUS_STATIC__/x"
    prompt = f"{static}\n{STATIC_BOUNDARY}\n{DYNAMIC_BOUNDARY}\n{dynamic}"

    split_static, split_dynamic = split_system_blocks(prompt)

    assert split_static == static
    assert split_dynamic == dynamic


# ─── apply_cache_to_messages ───────────────────────────────────────────────

def test_apply_cache_3_breakpoints():
    messages = [
        {"role": "system", "content": "static"},
        {"role": "system", "content": "dynamic"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
    ]

    result = apply_cache_to_messages(messages, breakpoints=3)

    # The static system message (first system) is cached; the dynamic one is not.
    assert result[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in result[1]
    # First user + last two users are breakpointed; the second user is not.
    assert result[2]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in result[4]
    assert result[6]["cache_control"] == {"type": "ephemeral"}
    assert result[8]["cache_control"] == {"type": "ephemeral"}
    # Input is not mutated.
    assert "cache_control" not in messages[0]


def test_apply_cache_zero_breakpoints_only_static():
    messages = [
        {"role": "system", "content": "static"},
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
    ]

    result = apply_cache_to_messages(messages, breakpoints=0)

    assert result[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in result[1]
    assert "cache_control" not in result[2]


def test_apply_cache_default_breakpoints():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
    ]
    result = apply_cache_to_messages(messages)
    assert DEFAULT_BREAKPOINTS == 3
    assert "cache_control" in result[1] and "cache_control" in result[2]


def test_apply_cache_no_system_message():
    result = apply_cache_to_messages([{"role": "user", "content": "hi"}])
    assert result[0]["cache_control"] == {"type": "ephemeral"}


# ─── strip_system_markers ──────────────────────────────────────────────────

def test_strip_system_markers_removes_boundary_lines():
    prompt = f"static\n{STATIC_BOUNDARY}\n{DYNAMIC_BOUNDARY}\ndynamic"
    assert STATIC_BOUNDARY not in strip_system_markers(prompt)
    assert DYNAMIC_BOUNDARY not in strip_system_markers(prompt)
    assert "static" in strip_system_markers(prompt)
    assert "dynamic" in strip_system_markers(prompt)


# ─── client / assembler integration ────────────────────────────────────────

def _assembled_prompt(cwd: str = "/tmp/work") -> str:
    return PromptAssembler(
        config=ModusConfig(), cwd=cwd, tool_names=["read_file", "write_file"],
        model="test-model", provider="test-provider",
    ).build()


def test_assembler_emits_static_then_dynamic_blocks():
    prompt = _assembled_prompt()
    static, dynamic = split_system_blocks(prompt)
    # Role/capability/tool declarations are static and stable.
    assert "You are Modus" in static
    assert "Available tools:" in static
    assert "Guidelines:" in static
    assert "```choice" in static
    assert "```summary" in static
    # Per-turn env facts are dynamic.
    assert "Current time:" in dynamic
    assert "Working directory:" in dynamic


def test_dynamic_change_keeps_static_cache():
    prompt_a = PromptAssembler(
        config=ModusConfig(), cwd="/tmp/work", tool_names=["read_file"],
        model="m", provider="p",
    ).build()
    prompt_b = PromptAssembler(
        config=ModusConfig(), cwd="/tmp/elsewhere", tool_names=["read_file"],
        model="m", provider="p",
    ).build()

    static_a, _ = split_system_blocks(prompt_a)
    static_b, _ = split_system_blocks(prompt_b)

    # Only the dynamic block changed; the static block (cache prefix) is intact.
    assert static_a == static_b


def test_format_messages_splits_system_when_cache_enabled():
    client = OpenAICompatibleClient(
        provider_name="t", model="m", api_key="k", base_url="http://x",
        enable_prompt_cache=True,
    )
    prompt = _assembled_prompt()
    static, dynamic = split_system_blocks(prompt)

    formatted = client._format_messages(
        [Message(role="user", content="u1"), Message(role="user", content="u2")],
        prompt,
    )

    assert formatted[0]["role"] == "system" and formatted[0]["content"] == static
    assert formatted[1]["role"] == "system" and formatted[1]["content"] == dynamic
    # Static block cached; dynamic not; first + last user breakpointed.
    assert formatted[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in formatted[1]
    assert "cache_control" in formatted[2]
    assert "cache_control" in formatted[3]


def test_format_messages_strips_markers_when_cache_disabled():
    client = OpenAICompatibleClient(
        provider_name="t", model="m", api_key="k", base_url="http://x",
        enable_prompt_cache=False,
    )
    prompt = _assembled_prompt()

    formatted = client._format_messages([Message(role="user", content="u1")], prompt)

    assert formatted[0]["role"] == "system"
    assert STATIC_BOUNDARY not in formatted[0]["content"]
    assert DYNAMIC_BOUNDARY not in formatted[0]["content"]
    assert "cache_control" not in formatted[0]
    assert "You are Modus" in formatted[0]["content"]


def test_trim_preserves_static_and_cache():
    client = OpenAICompatibleClient(
        provider_name="t", model="m", api_key="k", base_url="http://x",
        max_context_window=150, max_tokens=20, enable_prompt_cache=True,
    )
    prompt = _assembled_prompt()
    static, _ = split_system_blocks(prompt)
    messages = [
        Message(role="system", content="CONTRACT"),
        Message(role="user", content="request one " + "x" * 400),
        Message(role="assistant", content="answer one", tool_calls=[
            {"id": "1", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
        ]),
        Message(role="tool", content="tool one " + "z" * 400, tool_call_id="1"),
        Message(role="user", content="request two " + "y" * 400),
        Message(role="assistant", content="answer two"),
        Message(role="user", content="FINAL REQUEST"),
    ]

    trimmed = client._trim_messages(messages, [], prompt)

    # The system contract head survives trimming.
    assert trimmed[0].role == "system" and trimmed[0].content == "CONTRACT"
    assert trimmed[-1].content == "FINAL REQUEST"
    # Formatting after trimming still caches the static block and breakpoints
    # the first + last user.
    formatted = client._format_messages(trimmed, prompt)
    assert formatted[0]["role"] == "system"
    assert formatted[0]["content"] == static
    assert formatted[0]["cache_control"] == {"type": "ephemeral"}
    users = [f for f in formatted if f["role"] == "user"]
    assert users[0].get("cache_control") == {"type": "ephemeral"}
    assert users[-1].get("cache_control") == {"type": "ephemeral"}


def test_create_llm_client_deepseek_cache_enabled():
    from modus.config import LlmConfig
    from modus.llm import create_llm_client

    cfg = LlmConfig(provider="deepseek", model="deepseek-v4-flash", api_key="k")
    client = create_llm_client(cfg)

    assert client.enable_prompt_cache is True
    assert client.prompt_cache is True
    assert client.cache_breakpoints == 3


def test_create_llm_client_openai_cache_disabled():
    from modus.config import LlmConfig
    from modus.llm import create_llm_client

    cfg = LlmConfig(provider="openai", model="gpt-5", api_key="k")
    client = create_llm_client(cfg)

    assert client.enable_prompt_cache is False
    assert client.prompt_cache is False
