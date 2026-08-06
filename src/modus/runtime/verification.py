"""Run-local verification evidence for the edit -> test -> report loop.

The ledger is intentionally in-memory.  The desktop event/run ledger remains
the durable audit source; this object only answers whether the current run has
unverified file mutations before it emits its terminal event.
"""

from __future__ import annotations

import json
from typing import Any

from modus.tools.result_verifier import file_mutation_result_landed

_FILE_MUTATIONS = frozenset({"write_file", "edit_file", "patch"})


class RunVerification:
    """Track mutation generations and the latest structured test evidence."""

    def __init__(self, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max verification attempts must be positive")
        self.max_attempts = max_attempts
        self.mutation_generation = 0
        self.verified_generation = 0
        self.mutations: list[dict[str, str]] = []
        self.last_evidence: dict[str, Any] | None = None
        self.attempts = 0

    @property
    def has_mutations(self) -> bool:
        return self.mutation_generation > 0

    def require_verification(self, *, path: str = "") -> None:
        """Force a new passing test for a manually resumed verification run."""
        self.mutation_generation = max(self.mutation_generation, self.verified_generation + 1)
        if path:
            self.mutations.append({"tool": "verification_retry", "path": path})

    def observe_tool(
        self,
        *,
        name: str,
        payload: dict[str, Any] | None,
        result: str,
        is_error: bool,
    ) -> None:
        """Observe one completed tool result without trusting display text."""
        payload = payload or {}
        if name in _FILE_MUTATIONS and not is_error and file_mutation_result_landed(name, result):
            self.mutation_generation += 1
            path = str(payload.get("path") or payload.get("file_path") or payload.get("file") or "")
            self.mutations.append({"tool": name, "path": path})
            return

        if name != "run_tests":
            return
        self.attempts += 1
        try:
            evidence = json.loads(str(result or ""))
        except (TypeError, ValueError):
            self.last_evidence = {"schema": "modus.verification.v1", "status": "failed"}
            return
        if not isinstance(evidence, dict) or evidence.get("schema") != "modus.verification.v1":
            self.last_evidence = {"schema": "modus.verification.v1", "status": "failed"}
            return
        self.last_evidence = evidence
        if evidence.get("status") == "passed" and not is_error:
            self.verified_generation = self.mutation_generation

    def snapshot(self) -> dict[str, Any]:
        required = self.mutation_generation > self.verified_generation
        if self.last_evidence is None:
            status = "not_required" if not self.has_mutations else "missing"
        elif self.last_evidence.get("status") != "passed":
            status = "failed"
        elif not self.has_mutations:
            status = "passed"
        elif required:
            status = "missing"
        else:
            status = "passed"
        retry_exhausted = (
            self.has_mutations
            and required
            and
            self.last_evidence is not None
            and self.last_evidence.get("status") != "passed"
            and self.attempts >= self.max_attempts
        )
        return {
            "required": required,
            "status": status,
            "mutation_generation": self.mutation_generation,
            "verified_generation": self.verified_generation,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "retry_exhausted": retry_exhausted,
            "mutations": list(self.mutations[-20:]),
            "last_evidence": self.last_evidence,
        }
