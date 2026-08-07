from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from modus.config import ModusConfig
from modus.llm.base import LlmClient
from modus.tools.registry import ToolRegistry
from modus.types import Message, QueryResult

class Agent:
    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        system_prompt: str,
        cwd: str,
        config: ModusConfig,
        max_turns: int = 20,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.config = config
        self.max_turns = max_turns
        self.history: list[Message] = []

    async def run(
        self,
        message: str,
        *,
        approval_callback: Callable[[dict[str, Any]], Awaitable[str] | str] | None = None,
        cancel_event: asyncio.Event | None = None,
        budget: Any = None,
        session_id: str | None = None,
        run_id: str | None = None,
        reasoner_factory: Callable[..., Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        from modus.agent.strategies import PlanExecuteReasoner, ReActReasoner

        if reasoner_factory is None:
            # ``config.prompt.agent_mode`` selects the default strategy.  The
            # "react" mode is the classic loop; "plan" adds plan-then-execute
            # decomposition.  Explicit reasoner_factory overrides both.
            mode = str(getattr(getattr(self.config, "prompt", None), "agent_mode", "react"))
            reasoner_factory = PlanExecuteReasoner if mode == "plan" else ReActReasoner
        factory = reasoner_factory
        reasoner = factory(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            system_prompt=self.system_prompt,
            cwd=self.cwd,
            config=self.config,
            max_turns=self.max_turns,
            budget=budget,
            session_id=session_id,
            run_id=run_id,
        )
        async for event in reasoner.run(
            [*(self.history or []), Message(role="user", content=message)],
            approval_callback=approval_callback, cancel_event=cancel_event,
        ):
            if event.get("type") == "done":
                self.history = list(event.get("messages") or [])
            yield event

    async def run_complete(self, message: str) -> QueryResult:
        text = ""
        tokens = 0
        turns = 0
        content_parts: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        async for event in self.run(message):
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "content_delta":
                # Future AGI multimodal output: collect structured content parts.
                part = event.get("content")
                if isinstance(part, dict):
                    content_parts.append(part)
            elif event.get("type") == "artifact":
                artifacts.append(event.get("payload") or {})
            elif event.get("type") == "done":
                tokens = int(event.get("total_tokens") or 0)
                turns = int(event.get("total_turns") or 0)
        content: str | list[dict[str, Any]] | None = (
            content_parts if content_parts else None
        )
        return QueryResult(
            text=text, total_tokens=tokens, turns=turns,
            content=content, artifacts=artifacts,
        )

    def clear_history(self) -> None:
        self.history = []
