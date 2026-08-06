"""Canonical Modus execution forms.

The default single-model Agent is the product's baseline, not a named mode in
the user interface. MOA and Peri are opt-in collaboration forms.
"""

from __future__ import annotations

from typing import Final


DEFAULT_MODE: Final = "default"
MOA_MODE: Final = "moa"
PERI_MODE: Final = "peri"
# Reserved seam for a future AGI mode.  No runner exists yet; registering it
# keeps ``normalize_mode`` from silently relabelling an AGI run as "default"
# and lets the frontend offer it once a runner lands.
AGI_MODE: Final = "agi"

MODES: Final[dict[str, str]] = {
    DEFAULT_MODE: DEFAULT_MODE,
    MOA_MODE: MOA_MODE,
    PERI_MODE: PERI_MODE,
    AGI_MODE: AGI_MODE,
}

CANONICAL_MODES: Final[frozenset[str]] = frozenset({
    DEFAULT_MODE, MOA_MODE, PERI_MODE, AGI_MODE,
})
COLLABORATION_MODES: Final[frozenset[str]] = frozenset({MOA_MODE, PERI_MODE})


def normalize_mode(value: object, *, strict: bool = False) -> str:
    """Return a canonical mode, rejecting anything else at strict boundaries."""
    key = str(value or "").strip().lower()
    normalized = MODES.get(key)
    if normalized is not None:
        return normalized
    if strict:
        raise ValueError(f"unsupported mode: {key or value!s}")
    return DEFAULT_MODE


def is_collaboration_mode(value: object) -> bool:
    return normalize_mode(value) in COLLABORATION_MODES


def mode_label(value: object) -> str:
    """Return a user-facing label; the unnamed default deliberately has none."""
    return {
        MOA_MODE: "MOA", PERI_MODE: "Peri", AGI_MODE: "AGI",
    }.get(normalize_mode(value), "")
