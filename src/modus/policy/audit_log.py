from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modus.redact import redact_dict

SENSITIVE_KEYS = ("token", "key", "password", "secret", "authorization", "bearer")

class AuditLog:
    """操作审计日志，记录每次工具调用"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

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
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool_name": tool_name,
            "input": redact_dict(input_data),
            "outcome": outcome,
            "approver": approver,
            "cwd": cwd,
            "phase": phase,
        }
        if verification:
            event["verification"] = verification
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

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