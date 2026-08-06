from __future__ import annotations

import pytest

from modus.modes import (
    CANONICAL_MODES,
    DEFAULT_MODE,
    MOA_MODE,
    PERI_MODE,
    is_collaboration_mode,
    mode_label,
    normalize_mode,
)


@pytest.mark.parametrize("mode", [DEFAULT_MODE, MOA_MODE, PERI_MODE])
def test_canonical_modes_round_trip(mode: str) -> None:
    assert normalize_mode(mode, strict=True) == mode


def test_public_protocol_contains_internal_values() -> None:
    assert CANONICAL_MODES == {"default", "moa", "peri", "agi"}
    assert mode_label(DEFAULT_MODE) == ""
    assert mode_label(MOA_MODE) == "MOA"
    assert mode_label(PERI_MODE) == "Peri"
    assert mode_label("agi") == "AGI"
    assert is_collaboration_mode(DEFAULT_MODE) is False
    assert is_collaboration_mode(MOA_MODE) is True
    assert is_collaboration_mode(PERI_MODE) is True
    # AGI is a reserved seam, not a collaboration mode yet.
    assert is_collaboration_mode("agi") is False


@pytest.mark.parametrize("value", [None, "", "unknown", "single", "group"])
def test_noncanonical_modes_fail_at_strict_protocol_boundaries(value: object) -> None:
    with pytest.raises(ValueError, match="unsupported mode"):
        normalize_mode(value, strict=True)


def test_lenient_internal_fallback_uses_the_unnamed_default() -> None:
    assert normalize_mode(None) == DEFAULT_MODE
    assert normalize_mode("unknown") == DEFAULT_MODE
