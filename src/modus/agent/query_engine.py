from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from modus.agent.agent import Agent
from modus.config import ModusConfig
from modus.llm.base import LlmClient
from modus.prompt import PromptAssembler
from modus.tools.registry import ToolRegistry
from modus.types import Message, QueryResult

class QueryEngine:
    """QueryEngine 组装 PromptAssembler + Agent"""

    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        config: ModusConfig,
        cwd: str,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config
        self.cwd = cwd
        self.system_prompt = PromptAssembler(
            config=config,
            cwd=cwd,
            tool_names=tool_registry.list_names(),
            model=llm_client.model_name,
            provider=llm_client.provider_name,
        ).build()

    async def ask(
        self,
        message: str,
        history: list[Message] | None = None,
        *,
        approval_callback: Callable[[dict[str, Any]], Awaitable[str] | str] | None = None,
        cancel_event: asyncio.Event | None = None,
        budget: Any = None,
        session_id: str | None = None,
        run_id: str | None = None,
        reasoner_factory: Callable[..., Any] | None = None,
    ):
        agent = Agent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            system_prompt=self.system_prompt,
            cwd=self.cwd,
            config=self.config,
        )
        agent.history = list(history or [])
        async for event in agent.run(
            message, approval_callback=approval_callback, cancel_event=cancel_event, budget=budget,
            session_id=session_id, run_id=run_id, reasoner_factory=reasoner_factory,
        ):
            yield event

    async def ask_complete_async(self, message: str, history: list[Message] | None = None, *, approval_callback=None) -> QueryResult:
        text = ""
        tokens = 0
        turns = 0
        async for event in self.ask(message, history, approval_callback=approval_callback):
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "done":
                tokens = int(event.get("total_tokens") or 0)
                turns = int(event.get("total_turns") or 0)
        return QueryResult(text=text, total_tokens=tokens, turns=turns)

    def ask_complete(self, message: str, history: list[Message] | None = None) -> QueryResult:
        return asyncio.run(self.ask_complete_async(message, history))
