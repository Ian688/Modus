"""Canonical project identity for Desktop sessions and Agent runs.

The Desktop used to inherit the server process' ``cwd`` implicitly.  That is
safe enough for a CLI, but ambiguous in a long-lived UI where conversations,
runs and artifacts outlive the process that created them.  ``WorkspaceIdentity``
turns the project root into explicit, serialisable application state.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORKSPACE_SCHEMA = "modus.workspace.v1"


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    workspace_id: str
    root: str
    name: str
    schema: str = WORKSPACE_SCHEMA

    @classmethod
    def from_path(cls, value: str | Path) -> "WorkspaceIdentity":
        root = Path(value).expanduser().resolve()
        if not root.exists():
            raise ValueError("workspace path does not exist")
        if not root.is_dir():
            raise ValueError("workspace path must be a directory")
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:20]
        return cls(
            workspace_id=f"ws_{digest}",
            root=str(root),
            name=_project_name(root),
        )

    @classmethod
    def current(cls) -> "WorkspaceIdentity":
        return cls.from_path(Path.cwd())

    @classmethod
    def from_record(cls, value: dict[str, Any] | None) -> "WorkspaceIdentity":
        if not value:
            return cls.current()
        root = str(value.get("root") or "").strip()
        if not root:
            return cls.current()
        canonical = cls.from_path(root)
        return cls(
            workspace_id=str(value.get("workspace_id") or canonical.workspace_id),
            root=canonical.root,
            name=str(value.get("name") or canonical.name),
        )

    def to_wire(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "workspace_id": self.workspace_id,
            "root": self.root,
            "name": self.name,
        }


def _project_name(root: Path) -> str:
    """Prefer declared project metadata over an incidental folder name."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            name = str((data.get("project") or {}).get("name") or "").strip()
            if name:
                return name
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError):
            pass
    return root.name or str(root)
