from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from modus.types import Message

class LlmClient(Protocol):
    """所有 LLM 客户端必须遵循的接口协议"""
    model_name: str
    provider_name: str
    max_context_window: int
    # Prompt-cache engineering (Wave2 C1): when ``enable_prompt_cache`` is set,
    # the client applies cache_control breakpoints to the formatted messages —
    # the static system block plus the first/last ``cache_breakpoints`` user
    # messages (default 3).  ``cache_breakpoints`` is ignored when caching is
    # disabled.
    enable_prompt_cache: bool = False
    cache_breakpoints: int = 3

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> AsyncIterator[dict[str, Any]]: ...


    