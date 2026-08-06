"""Small transport-neutral command router for Desktop control messages.

Feature services can now be tested without constructing the  WebSocket receive
loop.  Existing commands can migrate here incrementally while the wire protocol
remains backwards compatible.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


CommandHandler = Callable[[Any, Any, dict[str, Any]], Awaitable[None]]


class DesktopCommandRouter:
    def __init__(self) -> None:
        self._handlers: dict[str, CommandHandler] = {}

    def register(self, command: str, handler: CommandHandler) -> None:
        name = str(command or "").strip()
        if not name:
            raise ValueError("command name is required")
        if name in self._handlers:
            raise ValueError(f"command already registered: {name}")
        self._handlers[name] = handler

    async def dispatch(self, websocket: Any, session: Any, message: dict[str, Any]) -> bool:
        handler = self._handlers.get(str(message.get("type") or ""))
        if handler is None:
            return False
        await handler(websocket, session, message)
        return True

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

