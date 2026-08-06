"""Private Modus data paths."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return Modus's private data directory."""
    values = os.environ if env is None else env
    configured = values.get("MODUS_DATA_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".modus"


def data_path(name: str, env: Mapping[str, str] | None = None) -> Path:
    return data_dir(env) / name


def output_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return Modus's default output directory (idempotent mkdir).

    Runs without an explicit workspace write temporary files, logs and
    generated artifacts here so the user's own filesystem stays untouched.
    """
    path = data_dir(env) / "output"
    path.mkdir(parents=True, exist_ok=True)
    return path
