from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modus.paths import data_path


_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    prompt: str
    path: Path

    def to_wire(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description, "prompt": self.prompt}


class SkillRepository:
    """Local user skill store with strict names and no executable code loading."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or data_path("skills")).expanduser()

    def list_public(self) -> list[dict[str, str]]:
        return [skill.to_wire() for skill in self.list()]

    def list(self) -> list[Skill]:
        if not self.root.is_dir():
            return []
        skills: list[Skill] = []
        for path in sorted(self.root.glob("*.json")):
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
        return Skill(path=path, **payload)

    def delete(self, name: str) -> None:
        self._validate_name(name)
        path = self.root / f"{name}.json"
        if not path.exists():
            raise ValueError("skill not found")
        path.unlink()

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
        return Skill(name=name, description=str(raw.get("description") or ""), prompt=prompt, path=path)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _NAME.fullmatch(name):
            raise ValueError("skill name must use lowercase letters, numbers, _ or -")
