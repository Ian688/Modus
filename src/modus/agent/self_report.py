"""Best-effort parsing of the model's fenced self-report blocks.

``PromptAssembler`` asks the model to wrap its plan, executed steps, closing
summary, and insights in fenced ``` blocks.  Nothing currently reads them back,
so the agent's own framing of a run is thrown away.  This module extracts those
blocks deterministically (no LLM, no side effects) so a runner can persist them
into run working memory and surface them to the KANBAN projection.

Block parsing is best-effort by design: a model that omits or malforms a fence
simply yields nothing.  Block content is model output — never trusted as
instructions, always treated as reference.
"""

from __future__ import annotations

import re
from typing import Any

# ```summary ... ```, ```plan ... ```, ```steps ... ```, ```insight ... ```
# with the status word on the fence line (e.g. ```summary success).
# The status line is a single optional word; the body is everything until the
# closing fence.  ``(?s)`` makes ``.`` match newlines for the body.
_FENCE_RE = re.compile(r"```([a-zA-Z_]+)(?: ([^\n`]*))?\n(.*?)(?:\n|^)```", re.DOTALL)

_SUPPORTED = frozenset({"plan", "steps", "summary", "insight", "choice"})


def extract_self_report_blocks(text: str) -> dict[str, Any]:
    """Return the parsed fenced blocks of one assistant turn.

    Output shape: ``{"summary": {"kind": "summary", "status": "success", "body": ...}, ...}``
    Unknown fence kinds are ignored.  Empty text returns {}.
    """
    if not text:
        return {}
    result: dict[str, Any] = {}
    for match in _FENCE_RE.finditer(text):
        kind = match.group(1).strip().lower()
        if kind not in _SUPPORTED:
            continue
        status_line = (match.group(2) or "").strip()
        body = match.group(3).strip()
        if not body and not status_line:
            continue
        entry: dict[str, Any] = {"kind": kind, "body": body}
        if status_line:
            entry["status"] = status_line.split()[0].strip().lower()
        result[kind] = entry
    return result


def summarize_turn_blocks(text: str) -> dict[str, Any]:
    """Compact projection of one assistant turn's self-report for persistence.

    Returns a small dict a runner can store as run working memory: the plan
    (first appearance), the steps, and the closing summary status/body, each
    bounded.  Absent kinds are omitted.
    """
    blocks = extract_self_report_blocks(text)
    if not blocks:
        return {}
    summary: dict[str, Any] = {}
    if blocks.get("plan"):
        summary["plan"] = _bounded(blocks["plan"]["body"], 800)
    if blocks.get("steps"):
        summary["steps"] = _bounded(blocks["steps"]["body"], 800)
    closing = blocks.get("summary")
    if closing:
        summary["summary"] = {
            "status": closing.get("status") or "",
            "body": _bounded(closing["body"], 400),
        }
    if blocks.get("insight"):
        summary["insight"] = _bounded(blocks["insight"]["body"], 200)
    return summary


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
