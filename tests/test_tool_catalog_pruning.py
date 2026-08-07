"""Tool catalog pruning: ``tools.enabled`` / ``tools.disabled`` actually take effect."""

from __future__ import annotations

import pytest

from modus.bootstrap import build_tool_registry
from modus.config import ModusConfig


@pytest.mark.asyncio
async def test_disabled_tools_are_pruned_from_registry() -> None:
    config = ModusConfig()
    config.tools.disabled = ["bash", "run_tests"]
    registry = await build_tool_registry(config=config, cwd=".")
    names = registry.list_names()

    assert "bash" not in names
    assert "run_tests" not in names
    assert "read_file" in names


@pytest.mark.asyncio
async def test_enabled_allowlist_limits_catalog() -> None:
    config = ModusConfig()
    config.tools.enabled = ["read_file", "list_dir"]
    registry = await build_tool_registry(config=config, cwd=".")
    names = registry.list_names()

    assert set(names) == {"read_file", "list_dir"}


@pytest.mark.asyncio
async def test_both_allowlist_and_blacklist_apply() -> None:
    config = ModusConfig()
    config.tools.enabled = ["read_file", "bash", "grep"]
    config.tools.disabled = ["bash"]
    registry = await build_tool_registry(config=config, cwd=".")
    names = registry.list_names()

    # The allowlist admits read_file + grep; the blacklist further removes bash.
    assert set(names) == {"read_file", "grep"}


@pytest.mark.asyncio
async def test_default_config_keeps_full_catalog() -> None:
    config = ModusConfig()
    registry = await build_tool_registry(config=config, cwd=".")
    names = registry.list_names()

    assert "bash" in names
    assert "read_file" in names
