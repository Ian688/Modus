"""Process resource limits for the shell tools (RLIMIT).

The bash and run_tests tools spawn an unrestricted shell today.  This module
adds an OS-level resource cap via ``preexec_fn``: the limits are computed up
front, then applied in the forked child just before exec.  Only POSIX
(macOS/Linux) supports ``resource``; Windows returns None (no limits).

Deliberately excludes RLIMIT_AS / RLIMIT_DATA: on macOS the child inherits the
parent interpreter's already-mapped address space, so lowering those below the
current footprint fails inside preexec.  Also excludes RLIMIT_NPROC: it is
user-global (counts every process the user already owns, not just this
command's descendants), so a sensible default breaks ordinary shell pipelines
on a busy machine.  CPU, FSIZE and NOFILE are the reliable, correctly-scoped
POSIX limits.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from modus.config import SandboxConfig

# Resource names that are reliably settable in preexec on macOS/Linux.
_RELIABLE = (
    "RLIMIT_CPU",
    "RLIMIT_FSIZE",
    "RLIMIT_NOFILE",
)


def _resource_map(config: SandboxConfig) -> list[tuple[str, int]]:
    """Return the ``(RLIMIT_*, value)`` pairs to apply, skipping unset ones."""
    pairs: list[tuple[str, int]] = []
    values = {
        "RLIMIT_CPU": int(config.cpu_seconds),
        "RLIMIT_FSIZE": int(config.fsize_bytes),
        "RLIMIT_NOFILE": int(config.nofile),
    }
    for name in _RELIABLE:
        value = values[name]
        if value > 0:
            pairs.append((name, value))
    return pairs


def rlimit_preexec(config: Any) -> Callable[[], None] | None:
    """Return a preexec_fn applying configured RLIMITs, or None when disabled.

    The returned callable only invokes ``resource.setrlimit`` (async-signal-safe
    enough for preexec).  The mapping is computed here, outside the child, so
    no allocation happens after fork.  A limit that cannot be set (e.g. below
    current usage) is skipped rather than failing the spawn.
    """
    sandbox = getattr(config, "sandbox", None)
    if not sandbox or not bool(getattr(sandbox, "enabled", False)):
        return None
    if os.name == "nt":
        return None
    try:
        import resource
    except ImportError:
        return None
    pairs = _resource_map(sandbox)
    if not pairs:
        return None

    def _apply() -> None:
        for name, value in pairs:
            try:
                resource.setrlimit(getattr(resource, name), (value, value))
            except (OSError, ValueError):
                continue

    return _apply
