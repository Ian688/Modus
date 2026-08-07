"""Command policy: normalize a shell command and block destructive patterns.

The previous implementation matched a few exact regexes (``rm -rf /`` etc.),
which real commands trivially bypass (``rm -rfv /``, ``rm -f -r /``, ``rm -rf
"/"``, ``rm -rf /etc``).  This version tokenizes with ``shlex``, normalizes
flag spelling and ordering, and blocks destructive intent on first principles:

- ``rm`` that is recursive AND force, targeting ``/``, a system root, or the
  user's home/``~`` (including ``~/.ssh``) — regardless of flag order or quotes.
- ``mkfs*`` (any filesystem formatting).
- ``dd`` writing to a raw block/character device.
- ``shutdown`` / ``reboot`` / ``halt`` / ``poweroff``.
- ``chmod -R 777`` on ``/`` or a system root.
- ``<shell> -c <string>`` and ``<interpreter> -c <string>`` — a command whose
  real body is an opaque string.  The guard cannot analyze inside it, so it
  fails closed (the classic ``sh -c 'rm -rf /'`` / ``python3 -c '...'`` wrapper).
- ``sudo <anything>`` — privilege escalation to a boundary the guard does not
  model (T5 semantics: sudo is almost always denied).  Configurable via
  ``block_sudo``.

The blacklist remains a coarse extra net; the normalized analysis is the real
defense.  Everything fails closed: a tokenization failure blocks the command.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

# Paths never allowed as destructive targets, on first principles.
_SYSTEM_ROOTS = ("/etc", "/usr", "/bin", "/sbin", "/var", "/System", "/Library",
                 "/Applications", "/dev", "/proc", "/sys", "/boot", "/private")

# Shells whose ``-c`` runs an opaque string.
_SHELL_CMDS = ("sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish")
# Interpreters whose ``-c`` runs opaque code.
_INTERPRETER_CMDS = ("python", "python3", "perl", "ruby", "node", "php")


class CommandPolicyError(ValueError):
    pass


def _is_destructive_rm_target(target: str) -> bool:
    """True when a normalized path targets the root, home, or a system root."""
    if not target:
        return False
    # ``~`` and ``$HOME`` resolve to home.
    if target in {"~", "$HOME", "${HOME}"} or target.startswith("~/"):
        return True
    if target == "/" or target.startswith("/~"):
        return True
    expanded = str(Path(target).expanduser())
    for root in _SYSTEM_ROOTS:
        if expanded == root or expanded.startswith(root + "/"):
            return True
    return False


class CommandGuard:
    """Block destructive commands before HITL approval, fail-closed."""

    def __init__(self, blacklist: list[str] | None = None, *, block_sudo: bool = True):
        self.blacklist = blacklist or []
        self.block_sudo = block_sudo

    def validate(self, command: str) -> None:
        if not command or not command.strip():
            return
        # Coarse substring blacklist first (configurable).
        normalized = " ".join(command.strip().split())
        for blocked in self.blacklist:
            if blocked and blocked in normalized:
                raise CommandPolicyError(f"command rejected by policy: {blocked}")

        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            raise CommandPolicyError(f"command rejected: unparsable ({exc})") from exc
        if not tokens:
            return

        first = tokens[0]
        # ``sudo`` prefixes a command run with elevated privileges the guard
        # cannot model.  Deny it on first principles (T5 semantics).
        if self.block_sudo and first == "sudo":
            raise CommandPolicyError("command rejected by policy: sudo")
        # ``env`` is a thin wrapper: skip options and ``NAME=VALUE``
        # assignments, then treat the first real command token as the lead.
        if first == "env":
            lead = next(
                (t for t in tokens[1:] if "=" not in t and not t.startswith("-")),
                None,
            )
            first = lead if lead is not None else first
        # A shell or interpreter with ``-c``/``-e`` runs an opaque string the
        # guard cannot analyze.  Fail closed on the wrapper itself.  Scan the
        # whole token list after the lead so ``env FOO=1 sh -c '...'`` and
        # ``python3 -c '...'`` are both caught.  For shells, ``-e`` is the
        # legal ``errexit`` flag and is not a code-execution switch; for
        # interpreters (perl -e) it is.
        if first in _SHELL_CMDS or first in _INTERPRETER_CMDS:
            if "-c" in tokens:
                raise CommandPolicyError(
                    f"command rejected by policy: {first} -c opaque execution"
                )
            if first in _INTERPRETER_CMDS and "-e" in tokens:
                raise CommandPolicyError(
                    f"command rejected by policy: {first} -e opaque execution"
                )

        # Normalize each flag: strip a leading ``-``/``--``, keep known letters.
        def norm_flag(token: str) -> str:
            stripped = token.lstrip("-")
            # ``-rfv`` -> {r, f, v}; ``--recursive`` -> recursive
            letters = set(c for c in stripped if c.isalpha())
            words = set(part for part in stripped.split(",") if part)
            return stripped, letters, words

        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "rm":
                flags: set[str] = set()
                j = i + 1
                target = ""
                while j < len(tokens):
                    arg = tokens[j]
                    if arg.startswith("-"):
                        flags.update(c for c in arg.lstrip("-") if c.isalpha())
                        # Long flags: --recursive / --force / -r / -f
                        flags.update(part for part in (arg.lstrip("-").split(",")) if part in {"r", "f"})
                    else:
                        target = arg
                        break
                    j += 1
                recursive = "r" in flags or "recursive" in flags
                force = "f" in flags or "force" in flags
                if recursive and force and _is_destructive_rm_target(target):
                    raise CommandPolicyError("command rejected by policy: destructive recursive rm")
                i = j + 1
                continue
            if token == "shred":
                # ``shred`` securely overwrites and (with -u) removes files.
                raise CommandPolicyError("command rejected by policy: shred")
            if token.startswith("mkfs"):
                raise CommandPolicyError("command rejected by policy: filesystem formatting")
            if token in {"shutdown", "reboot", "halt", "poweroff"}:
                raise CommandPolicyError("command rejected by policy: system power control")
            if token == "dd":
                # ``dd if=/dev/... of=/dev/...`` or ``of=/dev/sdX`` -> raw device write.
                args = tokens[i + 1:]
                if any(arg.startswith("of=/dev/") for arg in args):
                    raise CommandPolicyError("command rejected by policy: raw device write")
            if token == "chmod":
                args = tokens[i + 1:]
                # -R 777 on / or a system root.
                rest = " ".join(args)
                if re.search(r"-R\b", rest) and "777" in rest:
                    m = re.search(r"(?:^| )(/[A-Za-z0-9_.-]*)", rest)
                    target = m.group(1) if m else ""
                    if _is_destructive_rm_target(target):
                        raise CommandPolicyError("command rejected by policy: chmod -R 777 on system root")
            i += 1
