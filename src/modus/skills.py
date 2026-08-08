from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modus.paths import data_path


_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# ── skill lifecycle (Wave5 E2) ────────────────────────────────────────────────
#
# A skill is a procedure learned (or curated) for reuse.  It has a lifecycle
# state machine ``active → stale → archived`` driven by *real activity*
# (``mark_used`` / edits / reads bump ``last_activity_at``), never by file
# mtime: ``active`` skills are loadable, ``stale`` ones are loadable but
# surfaced as candidates for review, ``archived`` ones are hidden but
# recoverable (curation never deletes).  Usage is tracked in a ``.usage.json``
# sidecar so ``mark_used`` does not rewrite the skill file itself.
SKILL_ACTIVE = "active"
SKILL_STALE = "stale"
SKILL_ARCHIVED = "archived"
_VALID_STATUS = (SKILL_ACTIVE, SKILL_STALE, SKILL_ARCHIVED)

# Curator thresholds (in seconds).  A skill with no activity for
# ``stale_after`` is demoted to ``stale``; one stale for ``archive_after`` is
# demoted to ``archived``.  ``last_activity_at`` reflects the later of creation
# and the last use/patch/view bump, so a freshly saved skill starts ``active``.
_STALE_AFTER = 180 * 24 * 3600  # 180 days
_ARCHIVE_AFTER = 365 * 24 * 3600  # 365 days (stale for ~6 months)


def _now() -> float:
    return time.time()


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    prompt: str
    path: Path
    status: str = SKILL_ACTIVE
    last_activity_at: float = 0.0
    usage_count: int = 0

    def to_wire(self) -> dict[str, str]:
        # Backward-compatible minimal shape: existing consumers (server skills
        # CRUD, tests) assert this exact dict.  Lifecycle fields are exposed via
        # ``to_wire_lifecycle`` / ``list_public_lifecycle`` instead.
        return {"name": self.name, "description": self.description, "prompt": self.prompt}

    def to_wire_lifecycle(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
            "status": self.status,
            "last_activity_at": round(self.last_activity_at, 3) if self.last_activity_at else None,
            "usage_count": self.usage_count,
        }


class SkillRepository:
    """Local user skill store with strict names and no executable code loading.

    Each skill is a ``<name>.json`` file plus an optional ``<name>.usage.json``
    sidecar holding ``{last_activity_at, usage_count}``.  Curation (Wave5 E2)
    demotes idle skills ``active → stale → archived`` in-place and never deletes,
    so every skill stays recoverable.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or data_path("skills")).expanduser()

    def list_public(self) -> list[dict[str, str]]:
        return [skill.to_wire() for skill in self.list()]

    def list_public_lifecycle(self) -> list[dict[str, Any]]:
        return [skill.to_wire_lifecycle() for skill in self.list()]

    def list(self) -> list[Skill]:
        if not self.root.is_dir():
            return []
        skills: list[Skill] = []
        for path in sorted(self.root.glob("*.json")):
            if path.name.endswith(".usage.json"):
                continue
            skill = self._read(path)
            if skill:
                skills.append(skill)
        return skills

    def get(self, name: str) -> Skill | None:
        self._validate_name(name)
        return self._read(self.root / f"{name}.json")

    def save(self, *, name: str, description: str, prompt: str) -> Skill:
        self._validate_name(name)
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("skill prompt is required")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / f"{name}.json"
        payload = {"name": name, "description": description.strip(), "prompt": prompt}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        now = _now()
        self._write_usage(name, {"last_activity_at": now, "usage_count": 0})
        return Skill(path=path, **payload, status=SKILL_ACTIVE, last_activity_at=now, usage_count=0)

    def delete(self, name: str) -> None:
        self._validate_name(name)
        path = self.root / f"{name}.json"
        if not path.exists():
            raise ValueError("skill not found")
        path.unlink()
        usage = self.root / f"{name}.usage.json"
        try:
            usage.unlink()
        except OSError:
            pass

    def _read(self, path: Path) -> Skill | None:
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        name, prompt = str(raw.get("name") or ""), str(raw.get("prompt") or "")
        try:
            self._validate_name(name)
        except ValueError:
            return None
        if not prompt.strip():
            return None
        usage = self._read_usage(name)
        return Skill(
            name=name,
            description=str(raw.get("description") or ""),
            prompt=prompt,
            path=path,
            status=str(raw.get("status") or SKILL_ACTIVE) if str(raw.get("status") or "") in _VALID_STATUS else SKILL_ACTIVE,
            last_activity_at=float(usage.get("last_activity_at") or 0),
            usage_count=int(usage.get("usage_count") or 0),
        )

    # ── usage sidecar (Wave5 E2) ──────────────────────────────────────────

    def _usage_path(self, name: str) -> Path:
        return self.root / f"{name}.usage.json"

    def _read_usage(self, name: str) -> dict[str, Any]:
        try:
            raw = json.loads(self._usage_path(name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_usage(self, name: str, payload: dict[str, Any]) -> None:
        try:
            self._usage_path(name).write_text(
                json.dumps(payload, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def mark_used(self, name: str) -> bool:
        """Bump a skill's activity (use/patch/view) and record the usage sidecar.

        Returns False when the skill does not exist (no-op).  A used skill is
        promoted back to ``active``, so real reuse revives a stale skill.
        """
        self._validate_name(name)
        if not (self.root / f"{name}.json").exists():
            return False
        usage = self._read_usage(name)
        usage["last_activity_at"] = _now()
        usage["usage_count"] = int(usage.get("usage_count") or 0) + 1
        self._write_usage(name, usage)
        self._set_status(name, SKILL_ACTIVE)
        return True

    def _set_status(self, name: str, status: str) -> None:
        if status not in _VALID_STATUS:
            return
        path = self.root / f"{name}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        raw["status"] = status
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ── curator (Wave5 E2) ────────────────────────────────────────────────

    def curate(self, *, now: float | None = None, stale_after: float = _STALE_AFTER,
               archive_after: float = _ARCHIVE_AFTER) -> list[str]:
        """Demote idle skills active → stale → archived based on real activity.

        Never deletes: an archived skill stays on disk and is recovered on the
        next ``mark_used``.  Returns the list of names whose status changed.
        """
        now = _now() if now is None else now
        changed: list[str] = []
        for skill in self.list():
            last = skill.last_activity_at or skill.path.stat().st_mtime
            next_status: str | None = None
            if skill.status == SKILL_ACTIVE and now - last > stale_after:
                next_status = SKILL_STALE
            elif skill.status == SKILL_STALE and now - last > archive_after:
                next_status = SKILL_ARCHIVED
            if next_status is not None and next_status != skill.status:
                self._set_status(skill.name, next_status)
                changed.append(skill.name)
        return changed

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _NAME.fullmatch(name):
            raise ValueError("skill name must use lowercase letters, numbers, _ or -")
