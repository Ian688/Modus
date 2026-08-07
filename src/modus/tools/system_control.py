"""Cross-platform system control (Phase A5): ports + services.

The user wants "掌控、把玩级" system management across OSes.  This module adds
two tools the agent can actually use day-to-day:

- ``port_list`` — which processes are listening on which ports (lsof on
  macOS/Linux, netstat -ano on Windows).
- ``service_status`` / ``service_restart`` — inspect and restart a system
  service (launchctl on macOS, systemctl on Linux, sc/Get-Service on Windows).

Ports are read-only (safe lens).  Service restart is a destructive T4 action
(requires approval) — it is the first real "service management" capability.

Platform backends are split explicitly.  macOS/Linux are exercised by tests;
Windows is written against documented tool output but is NOT real-machine
verified (no Windows host available) — callers get a clear "unsupported on this
platform" error if the backend cannot run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any

from modus.tools.base import ToolContext, ToolResult

_PORT_MAX = 50


def _has_tool(name: str) -> bool:
    return shutil.which(name) is not None


# ── ports ──


async def port_list(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """List listening TCP/UDP ports and the owning process (read-only)."""
    port = str(payload.get("port") or "").strip()
    rows: list[str] = []
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            if not _has_tool("lsof"):
                return ToolResult("lsof not available", is_error=True)
            cmd = ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]
            if port:
                cmd = ["lsof", "-nP", "-iTCP", f":{port}", "-sTCP:LISTEN"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            lines = result.stdout.splitlines()
            # lsof header: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
            for line in lines[1:]:
                parts = line.split()
                if len(parts) < 9:
                    continue
                rows.append(f"{parts[0]}  pid={parts[1]}  {parts[-1]}")
                if len(rows) >= _PORT_MAX:
                    break
        elif os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=10, check=False,
            )
            for line in result.stdout.splitlines():
                if "LISTENING" not in line:
                    continue
                parts = line.split()
                if len(parts) >= 5:
                    rows.append(f"pid={parts[-1]}  {parts[1]}")
                if len(rows) >= _PORT_MAX:
                    break
        else:
            return ToolResult("port_list unsupported on this platform", is_error=True)
    except Exception as exc:
        return ToolResult(f"port_list failed: {exc}", is_error=True)
    text = "\n".join(rows) if rows else "(no listening ports found)"
    return ToolResult(
        text,
        metadata={"operation": "port_list", "port": port or None, "count": len(rows)},
    )


# ── services ──


def _service_backend() -> str | None:
    """Return the service backend name for this platform, or None."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "launchctl"
    if sys.platform.startswith("linux"):
        return "systemctl"
    return None


async def service_status(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Inspect a system service (read-only)."""
    service = str(payload.get("service") or "").strip()
    if not service:
        return ToolResult("service_status requires a service name", is_error=True)
    backend = _service_backend()
    try:
        if backend == "launchctl":
            result = subprocess.run(
                ["launchctl", "print", f"system/{service}"], capture_output=True,
                text=True, timeout=10, check=False,
            )
            if result.returncode != 0:
                # Try user domain as a fallback.
                result = subprocess.run(
                    ["launchctl", "print", f"gui/{os.getuid()}/{service}"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
            body = result.stdout or result.stderr
        elif backend == "systemctl":
            result = subprocess.run(
                ["systemctl", "status", service], capture_output=True,
                text=True, timeout=10, check=False,
            )
            body = result.stdout or result.stderr
        elif backend == "windows":
            result = subprocess.run(
                ["sc", "query", service], capture_output=True,
                text=True, timeout=10, check=False,
            )
            body = result.stdout or result.stderr
        else:
            return ToolResult("service_status unsupported on this platform", is_error=True)
    except FileNotFoundError:
        return ToolResult(f"service management tool not available on this platform", is_error=True)
    except Exception as exc:
        return ToolResult(f"service_status failed: {exc}", is_error=True)
    return ToolResult(
        body.strip()[:4000] or f"(no output for {service})",
        metadata={"operation": "service_status", "service": service},
    )


async def service_restart(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Restart a system service (destructive T4 — requires approval)."""
    service = str(payload.get("service") or "").strip()
    if not service:
        return ToolResult("service_restart requires a service name", is_error=True)
    backend = _service_backend()
    try:
        if backend == "launchctl":
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", f"system/{service}"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{service}"],
                    capture_output=True, text=True, timeout=15, check=False,
                )
            body = result.stdout or result.stderr
        elif backend == "systemctl":
            result = subprocess.run(
                ["systemctl", "restart", service], capture_output=True,
                text=True, timeout=15, check=False,
            )
            body = result.stdout or result.stderr
        elif backend == "windows":
            result = subprocess.run(
                ["sc", "stop", service], capture_output=True,
                text=True, timeout=15, check=False,
            )
            start = subprocess.run(
                ["sc", "start", service], capture_output=True,
                text=True, timeout=15, check=False,
            )
            body = (result.stdout or result.stderr) + (start.stdout or start.stderr)
        else:
            return ToolResult("service_restart unsupported on this platform", is_error=True)
    except FileNotFoundError:
        return ToolResult("service management tool not available on this platform", is_error=True)
    except Exception as exc:
        return ToolResult(f"service_restart failed: {exc}", is_error=True)
    return ToolResult(
        body.strip()[:4000] or f"(restart issued for {service})",
        metadata={"operation": "service_restart", "service": service},
    )
