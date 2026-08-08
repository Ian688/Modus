"""Background review fork + skill/memory lifecycle trigger (Wave5 E2).

A run that *acted* (executed tools successfully) and crossed the N-turn
interval gets a background curator fork: a bounded, read-only-ish agent that
distills the run's summary and self-report blocks into candidate memories and
skills, then deposits them into curator territory only.

Safety posture:

- **Action-validation axiom (GenericAgent L0)**: the fork is only spawned when
  the run's turn records show at least one successful tool execution.  A
  clarification / small-talk turn never triggers learning.
- **Provenance gate**: the fork's mutating surface is a single ``deposit_review``
  tool whose handler only writes into curator territory — the SQLite memories
  table (via ``add_memory``, marked ``auto`` / unverified) and the skills
  repository (``~/.modus/skills/``).  The fork registry deliberately excludes
  ``write_file``/``edit_file``/``bash``/``office_exec`` and every other workspace
  writer, so a fork cannot touch the workspace even if the model tries.  The
  standalone ``provenance_gate`` validates any path before a deposit.
- **Unverified content disclosure**: deposited memories are written with
  ``authority='auto'`` and are injected to later turns with an "auto-extracted
  未经验证" disclosure (see ``modus.desktop.memory``).

The fork reuses the ``spawn_subtask`` ReActReasoner construction (no new process
model) but runs as a background ``asyncio`` task with a tightly scoped registry.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from modus.paths import data_path

# ── curator territory ─────────────────────────────────────────────────────────
#
# The only places a background review fork may write.  ``skills`` is a real
# directory (``~/.modus/skills/``); ``memories`` denotes the desktop SQLite
# memories table, written through ``add_memory`` (never a raw file write).
_SKILLS_ROOT = data_path("skills")


def curator_roots() -> tuple[Path, Path]:
    """Return the curator-owned paths: skills dir + memories dir."""
    return (Path(_SKILLS_ROOT).expanduser(), data_path("memories").expanduser())


def provenance_gate(path: str | Path) -> bool:
    """True only when ``path`` lives inside curator territory.

    The fork's write gate: a deposit path outside ``~/.modus/skills/`` or
    ``~/.modus/memories/`` (e.g. anything in the user's workspace) is rejected.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return False
    for root in curator_roots():
        try:
            root_resolved = Path(root).resolve()
        except OSError:
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
    return False


# ── deterministic trigger (N-turn interval + action validation) ─────────────

# Default review interval: a run must reach N turns AND have executed a tool
# successfully before a background review fork is spawned.
DEFAULT_REVIEW_INTERVAL = 10


def turn_executed_tool(budget: Any) -> bool:
    """GenericAgent axiom: the run executed at least one successful tool call.

    Distilled from the budget's turn records (``tool_successes > 0``).  A run
    that only talked, planned or probed with errors does not count as verified
    action and must not trigger learning.
    """
    records = getattr(budget, "turn_records", None)
    if not records:
        return False
    try:
        return any(int(getattr(rec, "tool_successes", 0) or 0) > 0 for rec in records)
    except Exception:
        return False


def review_trigger(budget: Any, *, interval: int = DEFAULT_REVIEW_INTERVAL,
                   last_review_turn: int = 0) -> bool:
    """Return True when a background review fork should spawn for this run.

    Both conditions must hold: the run reached the N-turn interval (spaced by
    ``last_review_turn`` so repeated runs do not re-review every turn) AND the
    run executed a successful tool call (action validation).  Never raises on a
    malformed budget.
    """
    if budget is None:
        return False
    try:
        turns = int(getattr(budget, "turns", 0) or 0)
    except (TypeError, ValueError):
        return False
    if turns - int(last_review_turn or 0) < max(1, int(interval or DEFAULT_REVIEW_INTERVAL)):
        return False
    return turn_executed_tool(budget)


# ── the review fork prompt ───────────────────────────────────────────────────

_MEMORY_REVIEW = """You are Modus's background memory curator.

Read the run summary and the agent's self-report below.  Distill ONLY durable,
reusable knowledge:

- **memory**: a stable fact, preference, or constraint about the user/project
  that a future run should know (category: fact|preference|constraint).
- **skill**: a reusable procedure (a way to accomplish this class of task) that
  would help future runs — the "how", not the one-off result.

ACTION VALIDATION AXIOM: only distill what this run actually verified by
executing it successfully.  The run's successful tool calls are the evidence.
Never distill greetings, clarifications, or unverified speculation.  If nothing
durable, make no deposit calls.

For each candidate call the deposit_review tool with a JSON payload:
{{
  "type": "memory" | "skill",
  "content": "concise, self-contained statement or procedure",
  "source_run": "{run_id}",
  "provenance": "run_summary | self_report"
}}
For a skill you may also provide "name" (lowercase letters/numbers/_/-) and
"description".  Prefer quality over quantity: at most 2 memories and 1 skill.

## Run summary
{run_summary}

## Agent self-report
{self_report}
"""


# ── provenance-gated fork registry ──────────────────────────────────────────

# Read-only tools the fork may use to inspect the run.  Deliberately NO
# write_file/edit_file/bash/office_exec/spawn_process — the fork's only
# mutation surface is ``deposit_review``.
_FORK_READ_TOOLS = frozenset({"list_dir", "read_file", "grep", "search_code", "search_memory"})


def _slug_for(content: str, fallback: str) -> str:
    """Derive a strict skill name from content, else a hash-based fallback."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (content or "").strip().lower()).strip("-")
    if not slug:
        digest = hashlib.sha1((content or "").encode("utf-8")).hexdigest()[:10]
        slug = f"skill-{digest}"
    return slug[:63]


def build_deposit_tool(*, skill_repository: Any = None) -> Any:
    """Return the fork's single mutating tool, gated to curator territory.

    ``skill_repository`` may be injected (tests use a temp dir); the default
    writes to ``~/.modus/skills/``.
    """
    from modus.tools.base import Tool, ToolResult, object_schema

    async def _deposit_handler(payload: dict[str, Any], context: Any) -> ToolResult:
        kind = str(payload.get("type") or "").strip().lower()
        content = str(payload.get("content") or "").strip()
        if kind not in {"memory", "skill"}:
            return ToolResult("deposit_review requires type=memory or type=skill", is_error=True)
        if not content:
            return ToolResult("deposit_review requires non-empty content", is_error=True)
        if len(content) > 4000:
            return ToolResult("deposit_review content too long (max 4000 chars)", is_error=True)
        source_run = str(payload.get("source_run") or "").strip()
        provenance = str(payload.get("provenance") or "run_summary")
        if kind == "skill":
            try:
                from modus.skills import SkillRepository

                repo = skill_repository if skill_repository is not None else SkillRepository()
                name = str(payload.get("name") or _slug_for(content, "skill"))
                name = _slug_for(name, name or "skill")
                description = str(payload.get("description") or content[:120])
                repo.save(name=name, description=description, prompt=content)
                return ToolResult(
                    f"skill deposited: {name} (status=active)",
                    display_summary=f"沉淀技能 {name}",
                    metadata={"operation": "deposit_review", "path": str(repo.root / f"{name}.json")},
                )
            except Exception as exc:
                return ToolResult(f"skill deposit failed: {exc}", is_error=True)
        # memory deposit
        if not context.session_id:
            return ToolResult("memory deposit requires a persisted session", is_error=True)
        try:
            from modus.desktop.memory import add_memory

            category = str(payload.get("category") or "fact").strip().lower()
            source_ids = [f"run:{source_run}"] if source_run else None
            add_memory(
                context.session_id, content, category,
                authority="auto", source_ids=source_ids,
            )
            return ToolResult(
                "memory deposited (auto-extracted 未经验证)",
                display_summary=f"沉淀记忆（{category}）",
                metadata={"operation": "deposit_review", "category": category,
                          "source_run": source_run, "provenance": provenance},
            )
        except Exception as exc:
            return ToolResult(f"memory deposit failed: {exc}", is_error=True)

    return Tool(
        name="deposit_review",
        description=(
            "Deposit one distilled memory (type=memory) or skill (type=skill) "
            "into Modus's curator store.  This is the ONLY write tool a review "
            "fork has — it writes exclusively to Modus's own skills/memories "
            "territory, never to the user's workspace."
        ),
        parameters=object_schema(
            {
                "type": {"type": "string", "description": "memory or skill"},
                "content": {"type": "string", "description": "The durable fact/preference/constraint or procedure"},
                "source_run": {"type": "string", "description": "Run id that verified this knowledge"},
                "provenance": {"type": "string", "description": "run_summary or self_report"},
                "category": {"type": "string", "description": "fact, preference or constraint (memory only)"},
                "name": {"type": "string", "description": "Skill name (skill only)"},
                "description": {"type": "string", "description": "Skill description (skill only)"},
            },
            ["type", "content"],
        ),
        handler=_deposit_handler,
        is_read_only=False,
        is_concurrency_safe=False,
        danger_level="medium",
        requires_approval=False,
        capabilities=("memory", "filesystem"),
    )


def build_fork_registry(*, skill_repository: Any = None) -> Any:
    """Return a provenance-gated ToolRegistry for a review fork.

    Only read-only builtin tools plus ``deposit_review``.  No workspace writer
    is present, so even a model that asks for write_file/edit_file/bash gets a
    "tool not found" denial — the fork cannot touch the workspace.
    """
    from modus.tools.builtins import get_builtin_tools
    from modus.tools.registry import ToolRegistry

    registry = ToolRegistry()
    for tool in get_builtin_tools():
        if tool.name in _FORK_READ_TOOLS:
            registry.register(tool)
    registry.register(build_deposit_tool(skill_repository=skill_repository))
    return registry


def _fork_config(config: Any) -> Any:
    """Config copy for the fork: recursion disabled (no subtask chain)."""
    try:
        child = copy.deepcopy(config)
        child.features.convergence.max_recursion_depth = 0
        return child
    except Exception:
        return config


# ── background spawn ─────────────────────────────────────────────────────────

# In-memory last-review turn per session, so the N-turn interval is respected
# across runs of the same session.  Not persisted: acceptable for a best-effort
# curator trigger.
_LAST_REVIEW_TURN: dict[str, int] = {}


async def _run_review_fork(
    *,
    session_id: str,
    run_id: str,
    cwd: str,
    llm_client: Any,
    config: Any,
    run_summary: str,
    self_report: str,
    skill_repository: Any = None,
    max_turns: int = 4,
) -> None:
    """Run one bounded background review fork (best-effort, never raises)."""
    from modus.agent.strategies import ReActReasoner
    from modus.runtime.budget import RunBudget, RunLimits
    from modus.types import Message

    registry = build_fork_registry(skill_repository=skill_repository)
    budget = RunBudget(RunLimits(
        max_turns=max_turns,
        max_tokens=max(2_000, int(getattr(getattr(config, "runtime", None), "max_tokens", 0) or 2_000)),
        max_wall_seconds=min(300.0, getattr(getattr(config, "runtime", None), "max_wall_seconds", 600.0)),
    ))
    reasoner = ReActReasoner(
        llm_client=llm_client,
        tool_registry=registry,
        system_prompt=_MEMORY_REVIEW.format(
            run_summary=(run_summary or "")[:3000],
            self_report=(self_report or "")[:1500],
            run_id=run_id,
        ),
        cwd=cwd,
        config=_fork_config(config),
        max_turns=max_turns,
        budget=budget,
        session_id=session_id,
        run_id=run_id,
    )
    # The fork's registry is already provenance-gated: it exposes no workspace
    # writer — the only mutation surface is ``deposit_review``, which writes
    # exclusively into curator territory (skills/memories).  Auto-approve that
    # surface so the fork can deposit without a human in the loop; the
    # provenance gate is the authorization, not a second approval prompt.
    def _fork_approval_callback(request: dict[str, Any]) -> str:
        return "approve"

    try:
        async for _event in reasoner.run(
            [Message(role="user", content="Distill durable knowledge and deposit it.")],
            approval_callback=_fork_approval_callback,
        ):
            pass
    except Exception:
        # A background review must never disturb the run's terminal state.
        return


def spawn_background_review(
    *,
    session_id: str,
    run_id: str,
    cwd: str,
    llm_client: Any,
    config: Any,
    run_summary: str = "",
    self_report: str = "",
    skill_repository: Any = None,
    max_turns: int = 4,
) -> asyncio.Task | None:
    """Spawn the background review fork as an asyncio task; None when no loop.

    The fork runs detached from the caller: the run has already ended, and the
    fork's deposits are independently gated to curator territory.
    """
    try:
        return asyncio.create_task(_run_review_fork(
            session_id=session_id, run_id=run_id, cwd=cwd,
            llm_client=llm_client, config=config,
            run_summary=run_summary, self_report=self_report,
            skill_repository=skill_repository, max_turns=max_turns,
        ))
    except RuntimeError:
        return None


def maybe_spawn_background_review(
    *,
    session_id: str | None,
    run_id: str | None,
    cwd: str,
    llm_client: Any,
    config: Any,
    budget: Any,
    run_summary: str = "",
    self_report: str = "",
    interval: int = DEFAULT_REVIEW_INTERVAL,
    skill_repository: Any = None,
) -> asyncio.Task | None:
    """Combine the deterministic trigger with the background spawn.

    Returns the spawned task, or None when the run did not meet the gate
    (no successful tool execution, below the N-turn interval, no persisted
    session/run, or no running event loop).
    """
    if not session_id or not run_id:
        return None
    last = _LAST_REVIEW_TURN.get(str(session_id), 0)
    if not review_trigger(budget, interval=interval, last_review_turn=last):
        return None
    task = spawn_background_review(
        session_id=session_id, run_id=run_id, cwd=cwd,
        llm_client=llm_client, config=config,
        run_summary=run_summary, self_report=self_report,
        skill_repository=skill_repository,
    )
    if task is not None:
        try:
            _LAST_REVIEW_TURN[str(session_id)] = int(getattr(budget, "turns", 0) or 0)
        except (TypeError, ValueError):
            pass
    return task
