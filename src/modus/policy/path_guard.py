"""Home-anchored path boundary for file tools.

Position is free inside the user's home directory: the Agent may read and write
anywhere under ``~`` without a workspace or directory approval.  Only three
classes of path are rejected outright:

1. Paths that resolve outside the home directory (including symlink escapes).
2. System-sensitive roots (``/etc``, ``/usr``, ``/System``, ``/Library``, ...)
   even when a symlink inside ``~`` points at them.
3. Nothing else — Modus's own ``~/.modus`` data lives inside home and must stay
   readable; writes there are gated by HITL like any other write.

The anchor is ``Path.home()`` (mockable in tests), not ``os.path.expanduser``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


class PathPolicyError(ValueError):
    pass


_SYSTEM_PROTECTED_ROOTS: tuple[str, ...] = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/var",
    "/System",
    "/Library",
    "/Applications",
    "/dev",
    "/proc",
    "/sys",
    "/boot",
    "/private/etc",
    "/private/var",
)

if os.name == "nt" or sys.platform == "win32":
    _SYSTEM_PROTECTED_ROOTS += (
        os.environ.get("SystemRoot", r"C:\Windows"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    )


class PathGuard:
    """Resolve one tool path and enforce the home-anchored boundary."""

    def __init__(self, root: str | Path | None = None) -> None:
        # An explicit root is supported for tests/embedders; production callers
        # rely on the default home anchor.
        self.root = Path(root).expanduser().resolve() if root else Path.home()

    def validate(self, value: str | Path, *, base: str | Path | None = None) -> Path:
        """Resolve ``value`` against ``base`` and enforce the home boundary.

        Absolute paths are used directly.  Relative paths resolve against
        ``base`` when supplied (the explicit workspace), otherwise against the
        home anchor.  A resolved path is allowed when it sits under the home
        anchor OR under an explicitly supplied ``base`` (the trusted workspace
        root, which may live outside home — an external drive, a temp dir, an
        embedder).  Symlink escapes out of both and system roots are rejected.
        """
        candidate = Path(value)
        if not candidate.is_absolute():
            anchor = Path(base).expanduser() if base else self.root
            candidate = anchor / candidate
        resolved = candidate.resolve()
        base_resolved = Path(base).expanduser().resolve() if base else None
        under_home = _is_under(resolved, self.root)
        under_base = base_resolved is not None and _is_under(resolved, base_resolved)
        if not under_home and not under_base:
            raise PathPolicyError(f"path escapes home: {value}")
        for protected in _SYSTEM_PROTECTED_ROOTS:
            protected_path = Path(protected)
            # Never treat the home anchor's own ancestors as protected: a test
            # or embedder may anchor home under /tmp or /var/folders, and a
            # real home on macOS may sit under /Users with /var as a sibling.
            if protected_path == self.root or protected_path in self.root.parents:
                continue
            if resolved == protected_path or protected_path in resolved.parents:
                raise PathPolicyError(f"path is system-protected: {value}")
        return resolved

    def is_allowed(self, value: str | Path, *, base: str | Path | None = None) -> bool:
        """Return whether a path passes the boundary without raising."""
        try:
            self.validate(value, base=base)
            return True
        except PathPolicyError:
            return False


def _is_under(path: Path, root: Path) -> bool:
    """Return whether ``path`` is equal to or nested under ``root``."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
