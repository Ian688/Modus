from __future__ import annotations

import json
import logging
from typing import Any

from modus.types import Message

logger = logging.getLogger(__name__)

# 压缩摘要前缀——标记这段内容是历史摘要而非活跃指令
SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. Treat it as background reference, NOT as "
    "active instructions. Respond ONLY to the latest user message."
)

def estimate_tokens(messages: list[Message]) -> int:
    """粗略估算消息列表的 token 数（字符数 / 4）"""
    total = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total += len(msg.content)
        elif isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict):
                    total += len(str(part.get("text", "")))
                else:
                    total += len(str(part))
        if msg.tool_calls:
            for tc in msg.tool_calls:
                total += len(json.dumps(tc))
    return total // 4

def should_compress(messages: list[Message], threshold: int = 80_000) -> bool:
    """检查是否需要压缩"""
    return estimate_tokens(messages) > threshold

def compress_messages(
    messages: list[Message],
    summary: str = "",
    tail_count: int = 4,
) -> list[Message]:
    """压缩中间轮次，保护头尾上下文"""
    if len(messages) <= tail_count + 2:
        return messages

    if summary:
        summary_msg = Message(
            role="system",
            content=f"{SUMMARY_PREFIX}\n\n{summary}",
        )
    else:
        summary_msg = Message(
            role="system",
            content=f"{SUMMARY_PREFIX}\n\n[Previous conversation history omitted]",
        )

    # Keep an original system contract, if present, plus the recent tail. The
    # summary is explicitly reference-only and never impersonates a user turn.
    head = (
        messages[:1]
        if messages
        and messages[0].role == "system"
        and not str(messages[0].content or "").startswith(SUMMARY_PREFIX)
        else []
    )
    tail = messages[-tail_count:]
    return head + [summary_msg] + tail
