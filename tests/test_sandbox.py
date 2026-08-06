"""Shell sandbox: RLIMIT resource limits applied via preexec_fn.

These tests run real subprocesses to prove the limits take effect.  CPU and
FSIZE use small configured limits so a busy loop is killed quickly and an
over-sized write is truncated, without waiting on the asyncio timeout.
"""

from __future__ import annotations

import pytest

from modus.config import ModusConfig, SandboxConfig
from modus.sandbox import rlimit_preexec
from modus.tools.base import ToolContext


def _config(**sandbox_overrides) -> ModusConfig:
    cfg = ModusConfig()
    cfg.sandbox = SandboxConfig(**sandbox_overrides)
    return cfg


@pytest.mark.skipif(__import__("os").name == "nt", reason="RLIMIT is POSIX-only")
class TestRLimits:
    @pytest.mark.asyncio
    async def test_cpu_limit_kills_busy_loop(self):
        from modus.tools.builtins import bash

        ctx = ToolContext(cwd="/tmp", config=_config(enabled=True, cpu_seconds=1, fsize_bytes=0, nofile=0))
        result = await bash({"command": "while :; do :; done"}, ctx)
        assert result.is_error is True
        assert "CPU limit" in result.content

    @pytest.mark.asyncio
    async def test_fsize_truncates_large_write(self, tmp_path):
        from modus.tools.builtins import bash

        target = tmp_path / "big.txt"
        ctx = ToolContext(cwd=str(tmp_path), config=_config(enabled=True, cpu_seconds=0, fsize_bytes=4096, nofile=0))
        result = await bash({"command": f"head -c 100000 /dev/zero > {target}"}, ctx)
        assert result.is_error is True
        assert target.stat().st_size <= 4096
        assert "size limit" in result.content

    @pytest.mark.asyncio
    async def test_nofile_is_applied(self):
        from modus.tools.builtins import bash

        ctx = ToolContext(cwd="/tmp", config=_config(enabled=True, cpu_seconds=0, fsize_bytes=0, nofile=64))
        result = await bash({"command": "ulimit -n"}, ctx)
        assert result.is_error is False
        assert result.content.strip() == "64"

    @pytest.mark.asyncio
    async def test_normal_pipeline_still_works(self):
        from modus.tools.builtins import bash

        ctx = ToolContext(cwd="/tmp", config=_config(enabled=True, cpu_seconds=60, fsize_bytes=10 * 1024 * 1024, nofile=1024))
        result = await bash({"command": "echo hello | wc -l"}, ctx)
        assert result.is_error is False
        assert result.content.strip() == "1"


def test_disabled_returns_none():
    assert rlimit_preexec(_config(enabled=False)) is None


def test_default_config_is_disabled():
    cfg = ModusConfig()
    assert rlimit_preexec(cfg) is None


def test_all_zero_limits_returns_none():
    cfg = _config(enabled=True, cpu_seconds=0, fsize_bytes=0, nofile=0)
    assert rlimit_preexec(cfg) is None


def test_nproc_is_not_configured():
    """RLIMIT_NPROC is user-global and breaks pipelines; it must not be set."""
    from modus.sandbox import _RELIABLE
    assert "RLIMIT_NPROC" not in _RELIABLE
    assert "nproc" not in SandboxConfig.__dataclass_fields__
