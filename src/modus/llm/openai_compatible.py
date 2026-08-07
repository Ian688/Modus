from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from modus.types import Message

@dataclass(slots=True)
class OpenAICompatibleClient:
    provider_name: str
    model: str
    api_key: str
    base_url: str
    max_tokens: int = 8192
    temperature: float = 0.7
    timeout: float = 120.0
    max_context_window: int = 128_000
    prompt_cache: bool = False
    supports_images: bool = False
    supports_tools: bool = True
    reasoning_effort: str | None = None
    transport: httpx.AsyncBaseTransport | None = None
    # Last-resort input guard: when enabled, chat() trims the message list
    # toward ``max_context_window`` before sending, so an over-budget run never
    # blows the provider's hard window.  The system prompt, tool definitions
    # and the requested output budget are reserved first.
    trim_to_context_window: bool = True

    @property
    def model_name(self) -> str:
        return self.model

    # chat() 方法——这是整个 195 行文件的心脏
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.api_key:
            yield {"type": "error", "error": RuntimeError("API key not configured")}
            return

        if self.trim_to_context_window:
            messages = self._trim_messages(messages, tools, system_prompt)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._format_messages(messages, system_prompt),
            "stream": True,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools and self.supports_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.reasoning_effort:
            # This is deliberately capability-gated by the model repository.
            # OpenAI-compatible providers that expose reasoning effort accept
            # this field; other models never receive a guessed parameter.
            payload["reasoning_effort"] = self.reasoning_effort

        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "user-agent": "Modus/0.1.0",
        }
        url = self.base_url.rstrip("/") + "/chat/completions"

        yield {"type": "message_start", "model": self.model}
        try:
            async with (
                httpx.AsyncClient(timeout=self.timeout, http2=False, transport=self.transport) as client,
                client.stream("POST", url, headers=headers, json=payload) as response,
            ):
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    body = await response.aread()
                    err_text = body.decode("utf-8", errors="replace")[:300]
                    yield {"type": "error", "error": f"API {response.status_code}: {err_text}"}
                    return
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
                    yield {"type": "error", "error": f"Connection failed: {exc}"}
                    return
                try:
                    async for event in _iter_sse(response):
                        if event == "[DONE]":
                            break
                        try:
                            chunk = json.loads(event)
                        except json.JSONDecodeError:
                            continue
                        async for parsed in self._parse_chunk(chunk):
                            yield parsed
                except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError) as exc:
                    yield {"type": "error", "error": f"Stream interrupted: {exc}"}
                    return
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            yield {"type": "error", "error": f"Connection setup failed: {exc}"}
            return

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings for texts via the provider's ``/embeddings`` endpoint.

        Works for OpenAI-compatible providers that expose embeddings (OpenAI,
        Ollama, LM Studio, Jina, ...).  DeepSeek has no embeddings endpoint, so
        callers must catch failure and fall back to lexical matching.
        """
        if not texts:
            return []
        url = self.base_url.rstrip("/") + "/embeddings"
        payload = {"model": self.model, "input": texts}
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "user-agent": "Modus/0.1.0",
        }
        async with httpx.AsyncClient(timeout=self.timeout, http2=False, transport=self.transport) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        return [item["embedding"] for item in data.get("data", [])]

    def _format_messages(self, messages: list[Message], system_prompt: str) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for message in messages:
            if message.role == "tool":
                formatted.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": str(message.content),
                    }
                )
            elif message.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
                if message.tool_calls:
                    item["tool_calls"] = message.tool_calls
                formatted.append(item)
            else:
                formatted.append(
                    {"role": message.role, "content": self._format_content(message.content)}
                )
        return formatted

    def _format_content(self, content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        if self.supports_images:
            cleaned = []
            for part in content:
                item = {key: value for key, value in part.items() if key != "metadata"}
                cleaned.append(item)
            return cleaned
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                metadata = part.get("metadata") or {}
                source = metadata.get("source", "remote image")
                width = metadata.get("width", "?")
                height = metadata.get("height", "?")
                text_parts.append(f"[Image omitted: {source}, {width}x{height}]")
        return "\n".join(text_parts)

    def _trim_messages(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> list[Message]:
        """Trim toward ``max_context_window``, keeping the active tail.

        Budget is ``max_context_window`` minus the tool definitions, the
        requested output tokens and the system prompt.  When over budget, whole
        leading turns are dropped oldest-first so that ``tool`` messages never
        outlive the assistant tool-call they answer.  Leading ``system``
        messages (the contract and any context-compaction summary) and the
        final user turn (plus anything after it) are never dropped.
        """
        if not messages or self.max_context_window <= 0:
            return messages
        available = (
            self.max_context_window
            - _estimate_json_tokens(tools)
            - max(0, self.max_tokens)
            - _estimate_text_tokens(system_prompt)
        )
        if available <= 0:
            return messages
        # A leading system contract and any compaction summary are context,
        # not instruction history: keep every leading system message.
        head = 0
        while head < len(messages) and messages[head].role == "system":
            head += 1
        head_msgs = messages[:head]
        body = messages[head:]
        if sum(_estimate_message_tokens(m) for m in messages) <= available:
            return messages
        # Everything from the last user turn onward is the active request;
        # never trim into it.
        last_user = max(
            (i for i, m in enumerate(body) if m.role == "user"),
            default=-1,
        )
        active = body[last_user:] if last_user >= 0 else body
        active_tokens = sum(_estimate_message_tokens(m) for m in active)
        if active_tokens >= available:
            return messages  # nothing trimmable without dropping the request
        dropable = body[: last_user if last_user >= 0 else 0]
        # Drop whole user->assistant/tool turns from the front of the
        # dropable prefix until the body fits.  A turn is one user message and
        # everything up to (not including) the next user message.
        while dropable and active_tokens + sum(
            _estimate_message_tokens(m) for m in dropable
        ) > available:
            first_user = next((i for i, m in enumerate(dropable) if m.role == "user"), None)
            if first_user is None:
                dropable = dropable[1:]  # dangling assistant/tool prefix
                continue
            next_user = next(
                (i for i in range(first_user + 1, len(dropable))
                 if dropable[i].role == "user"),
                None,
            )
            if next_user is None:
                dropable = []  # only one turn left in the prefix; drop it all
            else:
                dropable = dropable[next_user:]
        return head_msgs + dropable + active


        if isinstance(content, str):
            return content
        if self.supports_images:
            cleaned = []
            for part in content:
                item = {key: value for key, value in part.items() if key != "metadata"}
                cleaned.append(item)
            return cleaned
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                metadata = part.get("metadata") or {}
                source = metadata.get("source", "remote image")
                width = metadata.get("width", "?")
                height = metadata.get("height", "?")
                text_parts.append(f"[Image omitted: {source}, {width}x{height}]")
        return "\n".join(text_parts)
    
    # 解析流式 chunk
    async def _parse_chunk(self, chunk: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        choices = chunk.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}

        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            yield {"type": "thinking_delta", "thinking": reasoning}

        content = delta.get("content")
        if isinstance(content, str) and content:
            yield {"type": "text_delta", "text": content}

        tool_calls = delta.get("tool_calls") or []
        for tool_call in tool_calls:
            yield {"type": "tool_call_delta", "tool_call": tool_call}

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            yield {"type": "message_end", "stop_reason": _map_finish_reason(str(finish_reason))}

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            yield {
                "type": "usage",
                "usage": {
                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                    "output_tokens": int(usage.get("completion_tokens") or 0),
                },
            }

# 解析流式chunk，所必要的映射函数：
def _map_finish_reason(reason: str) -> str:
    if reason in {"tool_calls", "tool_use"}:
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    if reason == "content_filter":
        return "stop_sequence"
    return "end_turn"

async def _iter_sse(response: httpx.Response) -> AsyncIterator[str]:
    """解析 SSE (Server-Sent Events) 流，逐条 yield data 内容"""
    buffer = ""
    async for text in response.aiter_text():
        buffer += text
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            data_lines = []
            for line in event.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if data_lines:
                yield "\n".join(data_lines)
    if buffer.strip():
        data_lines = []
        for line in buffer.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            yield "\n".join(data_lines)


def _estimate_text_tokens(text: str) -> int:
    """Rough token estimate shared with the agent compressor: chars / 4."""
    return (len(text) or 0) // 4


def _estimate_json_tokens(value: Any) -> int:
    """Token estimate for serialized JSON (tool definitions etc.)."""
    if not value:
        return 0
    return len(json.dumps(value, ensure_ascii=False)) // 4


def _estimate_message_tokens(message: Message) -> int:
    """Token estimate for one Message (content + tool_calls).

    Delegates to the shared ``modus.agent.compressor.estimate_tokens`` so the
    request-time trim and the end-of-run compaction agree on one estimator.
    """
    from modus.agent.compressor import estimate_tokens

    return estimate_tokens([message])
