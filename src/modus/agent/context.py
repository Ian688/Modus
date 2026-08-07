"""Context assembly seam: how session context is built for the model.

Runners previously glued memory + history + skill into messages ad hoc.  This
module defines a single ``ContextProvider`` interface so a future AGI can
request structured, retrieved or multimodal context through one defined path
instead of re-implementing string concatenation per runner.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from modus.desktop.memory import get_memory_context
from modus.types import Message


class ContextProvider(Protocol):
    """Builds the model-facing context for one session/run."""

    def memory_text(self, session_id: str | None, *, query: str | None = None) -> str:
        """Return bounded background memory for injection.

        ``query`` optionally scores semantic memory against the current request
        (project scope merged) instead of a flat dump.
        """
        ...

    def effective_history(
        self,
        session: Any,
        *,
        transient: Sequence[Message] | None = None,
        skill_message: Any = None,
        episodic_query: str | None = None,
        current_run_id: str | None = None,
    ) -> list[Message]:
        """Return the messages a runner will send to the model.

        Includes the assembled memory as a system message plus the session
        history and any transient/skill context.  The user's new message is
        added separately by the runner/loop.  ``episodic_query``, when given,
        recalls relevant prior-run conclusions as an extra bounded system block
        and scores semantic memory against the request.
        """
        ...


class SessionContextProvider:
    """Default provider: memory as system message + session history."""

    def memory_text(self, session_id: str | None, *, query: str | None = None) -> str:
        return get_memory_context(session_id, query=query) if session_id else ""

    def effective_history(
        self,
        session: Any,
        *,
        transient: Sequence[Message] | None = None,
        skill_message: Any = None,
        episodic_query: str | None = None,
        current_run_id: str | None = None,
    ) -> list[Message]:
        """Return the history messages a runner passes to the model.

        The user's new message is added separately by the runner/loop, so this
        includes memory (as a system message) plus prior session and transient
        context.  Memory is assembled here so every runner shares one path.
        """
        memory_context = self.memory_text(
            getattr(session, "db_id", None), query=episodic_query,
        )
        history = list(getattr(session, "main_history", []) or [])
        history.extend(transient or [])
        if skill_message is not None and getattr(skill_message, "content", ""):
            history.append(
                Message(
                    role="user",
                    content=f"[Skill 已附加] {skill_message.content}",
                )
            )
        if memory_context:
            history.append(Message(role="system", content=memory_context))
        if episodic_query and getattr(session, "db_id", None):
            engine = getattr(session, "engine", None)
            memory_cfg = getattr(getattr(engine, "config", None), "memory", None)
            retrieval_enabled = True if memory_cfg is None else bool(
                getattr(memory_cfg, "retrieval_enabled", True)
            )
            if retrieval_enabled:
                from modus.desktop.memory import episodic_recall_text

                recall = episodic_recall_text(
                    session.db_id, episodic_query, current_run_id=current_run_id,
                )
                if recall:
                    history.append(Message(role="system", content=recall))
        return history
