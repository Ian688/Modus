"""Capability classes a tool may declare, and the deny-first grant gate.

Phase 0 of the system-agent blueprint: each tool declares the capability
classes it needs (filesystem / exec / network / memory / agent).  The executor
denies any tool whose declared capabilities are not all granted by the active
run, *before* approval is even considered.  ``None`` grants (the default) mean
unrestricted — today's behavior.  An explicit grant set is the lockdown /
permission-ladder switch: T1 lens (``filesystem``), T3 exec, T4 network, etc.

This layer is deliberately orthogonal to the other guards — each answers one
question:

- ApprovalPolicy  — severity: is this dangerous enough to ask a human?
- PathGuard       — boundary: does a path stay inside the anchored workspace?
- CommandGuard    — content: does a shell command hit a blacklist entry?
- capability gate — class: is this tool class granted to the run at all?
"""
from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    FILESYSTEM = "filesystem"   # read / list / write files in the anchored boundary
    EXEC = "exec"               # run an OS subprocess (shell, tests, git)
    NETWORK = "network"         # web fetch/search, or remote git / credential ops
    MEMORY = "memory"           # semantic memory read / write
    AGENT = "agent"             # spawn child agents / sub-tasks


def capabilities_granted(
    tool_capabilities: list[str] | tuple[str, ...],
    granted: list[str] | None,
) -> bool:
    """Deny-first capability gate for one tool.

    ``granted`` is the active run's grant set.  ``None`` (the default) grants
    everything, preserving existing behavior.  An explicit grant set is
    fail-closed: a tool that declares no capabilities at all is denied under
    it, and every declared capability must be present in the grant.
    """
    if granted is None:
        return True
    if not tool_capabilities:
        return False
    return all(cap in granted for cap in tool_capabilities)
