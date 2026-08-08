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

# 文件操作清单（Wave2 C2）：从 tool_calls 里识别读过/改过哪些文件。
# 只读工具（read_file）与改写工具（write_file/edit_file/patch）分开归类。
_READ_TOOLS = frozenset({"read_file"})
_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "patch"})
_PATH_ALIASES = ("path", "file_path", "file")

# 压缩黑名单标记：审批相关消息（requires_approval 的决策/结果）与 goal/task 关键
# 消息不可被压缩截断——审批决策一旦丢失会破坏 approve-then-execute 的语义，
# goal/task 是模型正在执行的目标，截断会丢失正在进行的任务上下文。
_APPROVAL_RESULT_MARKERS = (
    "requires approval",
    "approval denied",
    "denied by approval",
    "approval policy",
    "Tool skipped by user approval",
    "approval-modified",
    "approval expired",
    "approval failed closed",
    "approval modify",
    "approval cancelled",
    "approval callback",
)
_GOAL_TASK_MARKERS = (
    "[GOAL]",
    "Goal:",
    "总目标",
    "[TASK]",
    "当前任务 [",
    "## Task",
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


def extract_file_operations(messages: list[Message]) -> dict[str, list[str]]:
    """从 tool_calls 收集本轮读过/改过的文件，去重并按时间序。

    Returns ``{"read": [...], "modified": [...]}`` — each list holds the unique
    file paths extracted from ``read_file``/``write_file``/``edit_file``/``patch``
    tool-call arguments, in first-use order.  Handles both the streamed
    OpenAI-compatible shape (``function.arguments`` JSON string) and a
    pre-parsed dict.  Path aliases (``file_path``/``file``) are normalized to
    ``path`` exactly like the executor does, so every consumer sees the same
    contract.
    """
    read: list[str] = []
    modified: list[str] = []
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or call.get("name") or "")
            arguments = function.get("arguments", call.get("arguments", {}))
            if isinstance(arguments, str):
                try:
                    payload = json.loads(arguments or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
            else:
                payload = arguments
            if not isinstance(payload, dict):
                continue
            path = str(payload.get("path") or "") or ""
            if not path:
                for alias in _PATH_ALIASES[1:]:
                    if payload.get(alias):
                        path = str(payload[alias])
                        break
            if not path:
                continue
            target = modified if name in _MUTATING_TOOLS else (read if name in _READ_TOOLS else None)
            if target is not None and path not in target:
                target.append(path)
    return {"read": read, "modified": modified}


def render_file_manifest(operations: dict[str, list[str]]) -> str:
    """Render the file-operation manifest as the compacted-context block.

    Emits ``[FILES READ THIS TURN]`` / ``[FILES MODIFIED THIS TURN]`` sections
    so the model can recover which files it read or edited mid-run, even after
    the turns that contained those tool calls were compacted away.
    """
    lines: list[str] = []
    if operations.get("read"):
        lines.append("[FILES READ THIS TURN] " + ", ".join(operations["read"]))
    if operations.get("modified"):
        lines.append("[FILES MODIFIED THIS TURN] " + ", ".join(operations["modified"]))
    return "\n".join(lines)


def is_protected_message(message: Message) -> bool:
    """True when a message must never be compacted away.

    Protected messages are the approval-decided tool results (approve-then-
    execute: the decision is the record of what was authorized) and goal/task
    context messages (the model's active objective).  ``should_compress`` skips
    these before choosing a candidate window, and ``compress_messages`` re-adds
    any protected message that would otherwise fall outside the retained tail.
    """
    content = ""
    if isinstance(message.content, str):
        content = message.content
    elif isinstance(message.content, list):
        parts = []
        for part in message.content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part))
        content = "\n".join(parts)
    lowered = content.lower()
    if any(marker in lowered for marker in _APPROVAL_RESULT_MARKERS):
        return True
    return any(marker in content for marker in _GOAL_TASK_MARKERS)


def _protected_message_roles(messages: list[Message]) -> set[int]:
    """Indices of messages that must survive compaction."""
    return {i for i, message in enumerate(messages) if is_protected_message(message)}

def should_compress(messages: list[Message], threshold: int = 80_000) -> bool:
    """检查是否需要压缩。

    Protected messages (approval decisions, goal/task context) are excluded
    from the budget check — a run whose only large content is protected must
    not be compacted, because compaction could never safely drop it.
    """
    if estimate_tokens(messages) <= threshold:
        return False
    protected = _protected_message_roles(messages)
    candidates = [m for i, m in enumerate(messages) if i not in protected]
    return estimate_tokens(candidates) > threshold

def compression_tail_count(config: Any) -> int:
    """从引擎配置读取压缩保留尾部条数（默认 8，至少 2）。"""
    return max(2, int(getattr(getattr(getattr(config, "features", None), "compression", None), "tail_messages", 8)))

def compress_messages(
    messages: list[Message],
    summary: str = "",
    tail_count: int = 4,
) -> list[Message]:
    """压缩中间轮次，保护头尾上下文。

    Keeps an original system contract (if any), a reference-only summary, the
    final user instruction, and a turn-aligned recent tail.  ``tail`` never
    starts mid-tool-turn: if it would begin on a ``tool`` message whose owning
    assistant was compacted, it backs up to that assistant so every retained
    tool message has its assistant tool_call in the retained context.

    Wave2 C2 re-inject: the summary is followed by the file-operation manifest
    (``[FILES READ THIS TURN]`` / ``[FILES MODIFIED THIS TURN]``) extracted from
    the compacted tool calls, so the model can recover which files it read or
    edited this run.  Protected messages (approval decisions and goal/task
    context) are never compacted: any protected message that would fall outside
    the retained tail is spliced back in right after the summary.
    """
    if len(messages) <= tail_count + 2:
        return messages

    if summary:
        summary_text = f"{SUMMARY_PREFIX}\n\n{summary}"
    else:
        summary_text = f"{SUMMARY_PREFIX}\n\n[Previous conversation history omitted]"

    manifest = render_file_manifest(extract_file_operations(messages))
    if manifest and "[FILES READ THIS TURN]" not in summary_text:
        # A semantic summary may already carry the file list (the summarizer is
        # asked to include it); avoid a duplicate block when it did.
        summary_text += f"\n\n{manifest}"
    summary_msg = Message(role="system", content=summary_text)

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

    # Turn-align the tail: back up so the tail starts at an assistant-with-
    # tool_calls (or any non-tool) message, never in the middle of a
    # compacted tool turn.  Every retained tool message then has its owning
    # assistant tool_call present.
    start = 0
    for index, message in enumerate(tail):
        if message.role != "tool":
            start = index
            break
    if start > 0:
        tail = tail[start:]

    # Never drop the final user instruction: if the last user message falls
    # outside the tail, splice it back in right before the tail.
    last_user_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            last_user_index = index
            break
    if last_user_index >= 0:
        # The user message is present in the tail already when its index is at
        # or after ``len(messages) - len(tail)``.
        tail_start_index = len(messages) - len(tail)
        if last_user_index < tail_start_index:
            kept_user = messages[last_user_index]
            if kept_user not in tail:
                tail = [kept_user, *tail]

    # Re-inject protected messages (approval decisions, goal/task context) that
    # the tail rule would otherwise drop.  They are appended after the summary
    # so their position stays deterministic and they remain reference context.
    tail_start_index = len(messages) - len(tail)
    protected = [
        messages[i] for i in _protected_message_roles(messages)
        if i < tail_start_index
    ]
    # Turn-align each re-injected protected assistant/tool pair: a protected
    # tool result must keep its owning assistant tool_call, and vice-versa.
    protected = _align_protected(messages, protected)
    # Never duplicate a message the tail already keeps.
    protected = [m for m in protected if m not in tail]

    return head + [summary_msg] + protected + tail


def _align_protected(messages: list[Message], protected: list[Message]) -> list[Message]:
    """Pair re-injected protected tool results with their assistant tool_calls.

    A protected tool result whose assistant tool_call was compacted is
    useless (the call it answers is gone); a protected assistant tool_call
    whose result was dropped loses its decision outcome.  For each protected
    message, walk the original list for its counterpart and keep both.
    """
    if not protected:
        return protected
    original_ids = {id(message) for message in protected}
    result = list(protected)
    for message in protected:
        if message.role == "tool" and message.tool_call_id:
            for other in messages:
                if (
                    other.role == "assistant"
                    and id(other) not in original_ids
                    and any(str(tc.get("id")) == message.tool_call_id for tc in (other.tool_calls or []))
                ):
                    if id(other) not in {id(m) for m in result}:
                        result.append(other)
                    break
        elif message.role == "assistant" and message.tool_calls:
            for other in messages:
                if other.role == "tool" and other.tool_call_id:
                    if any(str(tc.get("id")) == other.tool_call_id for tc in message.tool_calls):
                        if id(other) not in {id(m) for m in result}:
                            result.append(other)
                        break
    return result
