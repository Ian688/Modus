from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modus.redact import redact_dict, redact_text

SENSITIVE_KEYS = ("token", "key", "password", "secret", "authorization", "bearer")

# Defaults for the conservative storage policy (mirrors StorageConfig).
_DEFAULT_ROTATE_BYTES = 100 * 1024 * 1024
_DEFAULT_ROTATE_KEEP = 5
# In-memory fallback ring when the disk is unwritable: last N events survive a
# full-disk failure without raising.
_DEGRADED_MEMORY_LIMIT = 500

logger = logging.getLogger(__name__)


def _rotated_candidates(path: Path) -> list[Path]:
    """Return the rolled audit files (audit-N.jsonl), oldest suffix first."""
    stem = path.stem
    candidates: list[Path] = []
    for sibling in path.parent.glob(f"{stem}-*.jsonl"):
        suffix = sibling.name[len(stem) + 1: -len(".jsonl")]
        try:
            index = int(suffix)
        except ValueError:
            continue
        candidates.append((index, sibling))
    return [candidate for _index, candidate in sorted(candidates)]


class AuditLog:
    """操作审计日志，记录每次工具调用

    Rotation + degradation (T2 data-plane governance): before each write the
    active file is stat'd; once it exceeds ``rotate_bytes`` it is rotated to
    ``audit-1.jsonl`` (shifting prior copies to ``audit-2…N``), keeping at most
    ``rotate_keep`` copies.  If the append itself fails (disk full / unwritable)
    the event is kept in an in-memory ring instead of raising, and a
    degradation marker is recorded so the health loop can observe it.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        rotate_bytes: int | None = None,
        rotate_keep: int | None = None,
    ):
        self.path = Path(path).expanduser()
        self.rotate_bytes = rotate_bytes or _DEFAULT_ROTATE_BYTES
        self.rotate_keep = max(1, int(rotate_keep or _DEFAULT_ROTATE_KEEP))
        # Ring of the most recent events kept alive while disk writes fail.
        self._degraded_memory: list[dict[str, Any]] = []
        self._degraded = False
        self._rotated_count = 0

    def _rotate(self) -> None:
        """Split the active file: audit.jsonl -> audit-1.jsonl, shift the rest."""
        if not self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stem = self.path.stem
        # Shift existing copies up: audit-N -> audit-(N+1), newest-first.
        existing = _rotated_candidates(self.path)
        for idx in range(len(existing) - 1, self.rotate_keep - 1, -1):
            with suppress(OSError):
                existing[idx].unlink()
        to_keep = existing[: max(0, self.rotate_keep - 1)]
        for index, candidate in reversed(list(enumerate(to_keep, start=1))):
            try:
                candidate.rename(self.path.with_name(f"{stem}-{index + 1}.jsonl"))
            except OSError:
                # A concurrent rotation may have moved it; re-stat to proceed.
                if not candidate.exists():
                    continue
                raise
        try:
            self.path.rename(self.path.with_name(f"{stem}-1.jsonl"))
        except OSError:
            if not self.path.exists():
                return
            raise
        self._rotated_count += 1
        logger.warning(
            "audit log rotated: %s exceeded %d bytes -> %s-1.jsonl",
            self.path, self.rotate_bytes, stem,
        )

    def _append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if size > self.rotate_bytes:
            self._rotate()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._degraded = False
        self._degraded_memory.clear()

    def record(
        self,
        *,
        tool_name: str,
        input_data: dict[str, Any],
        outcome: str,
        approver: str,
        cwd: str,
        phase: str = "execution",
        verification: dict[str, Any] | None = None,
        scope: str | None = None,
        resource_key: str | None = None,
    ) -> None:
        """Append one audited decision.

        A1 scope (Wave3): ``scope`` is the approval scope level that produced
        the decision (``per-invocation`` / ``per-resource`` / ``per-tool``) and
        ``resource_key`` is the scoped resource (a rewritten command, a URL
        origin, a target path) the decision was made against.  Recording both
        lets an audit replay answer *why this exact resource was allowed or
        denied* rather than only which tool ran.  Both are optional — legacy
        callers without a scope dimension omit them and the event stays
        backward compatible.
        """
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool_name": tool_name,
            "input": redact_dict(input_data),
            "outcome": outcome,
            "approver": approver,
            "cwd": cwd,
            "phase": phase,
        }
        if scope is not None:
            event["scope"] = str(scope)
        if resource_key is not None:
            event["resource_key"] = redact_text(str(resource_key))
        if verification:
            event["verification"] = verification
        try:
            self._append(event)
        except OSError:
            # Disk full / unwritable: keep the event in memory, mark degraded,
            # and never raise — a failed audit must not kill the operation it
            # is auditing.
            logger.exception("audit write failed; buffering in memory")
            self._degraded_memory.append(event)
            if len(self._degraded_memory) > _DEGRADED_MEMORY_LIMIT:
                self._degraded_memory.pop(0)
            self._degraded = True

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    @property
    def degraded(self) -> bool:
        """True when the last write was buffered in memory instead of disk."""
        return self._degraded

    @property
    def degraded_memory(self) -> list[dict[str, Any]]:
        return list(self._degraded_memory)

    @property
    def rotated_count(self) -> int:
        return self._rotated_count