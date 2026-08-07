"""System-status lens: a bounded, schema-capped host snapshot (Phase 2).

The blueprint's read lens: ``system_probe`` gives the agent a ``modus.system.v1``
JSON snapshot of CPU/load, memory, disk, a bounded process list, and a log
summary — enough to answer "why is this machine slow / is disk full / what's
running" without any file-content disclosure.

Security posture (all verified against the codebase and the host):

- Pure stdlib, no psutil.  Platform splits are explicit (macOS / Linux / other).
- ``data_disclosure="none"``: the snapshot returns derived statistics only.
  Log directories report path + file count + total size, never log content.
  Process rows carry pid/ppid/cpu/mem/rss/time and the truncated ``comm``
  column, never ``/proc/*/cmdline`` or argv.
- Bounded by construction: at most ``_MAX_PROCESSES`` rows, log dirs walked
  with the shared capped walker, every probe wrapped in a try/except so one
  unreadable source degrades to ``{exists:false, readable:false, error}``
  instead of failing the whole probe (blueprint: a blocked OS path surfaces as
  blocked, never silent truncation).
- This module reads system paths (``/proc``, ``/var/log``) directly.  It does
  NOT call ``PathGuard.validate`` — those paths are system-protected by design,
  and the lens is read-only sampling, not a workspace path tool.
"""

from __future__ import annotations

import errno
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Any

_MAX_PROCESSES = 20
_LOG_DIRS: tuple[str, ...] = (
    "/var/log",
    "/private/var/log",
)
if sys.platform == "darwin":
    _LOG_DIRS = _LOG_DIRS + (os.path.expanduser("~/Library/Logs"),)


def _probe_cpu() -> dict[str, Any]:
    out: dict[str, Any] = {"cpu_count": os.cpu_count()}
    try:
        load = os.getloadavg()
        out["loadavg_1m"], out["loadavg_5m"], out["loadavg_15m"] = (
            round(float(load[0]), 2), round(float(load[1]), 2), round(float(load[2]), 2),
        )
    except OSError:
        pass  # loadavg unavailable (e.g. Windows)
    times = os.times()
    out["times"] = {
        "user": round(times.user, 2),
        "system": round(times.system, 2),
    }
    return out


def _probe_memory() -> dict[str, Any]:
    """Physical total + available memory, platform-split.

    ``os.sysconf`` gives total on macOS and Linux.  Free/available needs a
    platform parse: macOS ``vm_stat``, Linux ``/proc/meminfo``.  Windows has no
    stdlib path and reports ``unsupported``.
    """
    out: dict[str, Any] = {}
    try:
        out["total_bytes"] = (
            int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        )
    except (OSError, ValueError):
        out["total_bytes"] = None

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=5,
                check=False,
            )
            for line in result.stdout.splitlines():
                if "page size" in line:
                    page_size = int("".join(ch for ch in line if ch.isdigit()) or 4096)
                if line.startswith("Pages free:"):
                    free_pages = int(line.split(":")[1].strip().rstrip("."))
            out["free_bytes"] = free_pages * page_size
        except Exception:
            out["free_bytes"] = None
    elif os.path.exists("/proc/meminfo"):
        try:
            values: dict[str, int] = {}
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].rstrip(":") in {"MemTotal", "MemAvailable"}:
                        values[parts[0].rstrip(":")] = int(parts[1]) * 1024
            out["free_bytes"] = values.get("MemAvailable")
        except (OSError, ValueError):
            out["free_bytes"] = None
    else:
        out["free_bytes"] = None
        out["memory_detail"] = "unsupported"
    return out


def _probe_disk() -> list[dict[str, Any]]:
    """disk_usage for /, home, and the current working directory."""
    targets: list[str] = ["/", os.path.expanduser("~"), os.getcwd()]
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        try:
            usage = shutil.disk_usage(target)
            free_pct = round(usage.free / usage.total * 100, 1) if usage.total else 0.0
            rows.append({
                "path": target,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "free_pct": free_pct,
            })
        except (OSError, ValueError):
            rows.append({"path": target, "error": "unavailable"})
    return rows


def _probe_processes() -> list[dict[str, Any]]:
    """Bounded process rows: id/parent/cpu/mem/rss/time + truncated comm.

    Deliberately never reads a process's argv/cmdline.  macOS uses ``ps``'s
    ``comm`` column (truncated name); Linux reads ``/proc/*/stat`` and
    ``/proc/*/status`` only.  Capped at ``_MAX_PROCESSES`` rows sorted by RSS.
    """
    rows: list[dict[str, Any]] = []
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid,ppid,%cpu,%mem,rss,etime,comm", "-r"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            lines = result.stdout.splitlines()
            for line in lines[1:]:
                parts = line.split(None, 6)
                if len(parts) < 7:
                    continue
                try:
                    rows.append({
                        "pid": int(parts[0]), "ppid": int(parts[1]),
                        "cpu_pct": float(parts[2]), "mem_pct": float(parts[3]),
                        "rss_kb": int(parts[4]), "etime": parts[5],
                        "name": parts[6][:60],
                    })
                except ValueError:
                    continue
                if len(rows) >= _MAX_PROCESSES:
                    break
        except Exception:
            return rows
    elif os.path.isdir("/proc"):
        # Linux: read /proc/<pid>/stat (name, state, rss) + /proc/<pid>/status
        # (VmRSS).  No cmdline.
        import glob
        candidates = sorted(glob.glob("/proc/[0-9]*"), key=lambda p: int(p.rsplit("/", 1)[1]))
        for proc_dir in candidates:
            pid = int(proc_dir.rsplit("/", 1)[1])
            try:
                with open(f"{proc_dir}/stat", encoding="utf-8") as handle:
                    stat = handle.read()
                # comm is in parens, may contain spaces/')'.
                start = stat.index("(")
                end = stat.rindex(")")
                comm = stat[start + 1:end]
                rest = stat[end + 2:].split()
                state = rest[0] if rest else "?"
                rss_pages = int(rest[21]) if len(rest) > 21 else 0
                page_kb = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4
                rows.append({
                    "pid": pid, "ppid": None, "cpu_pct": None, "mem_pct": None,
                    "rss_kb": rss_pages * page_kb, "etime": None,
                    "name": comm[:60], "state": state,
                })
            except (OSError, ValueError, IndexError):
                continue
            if len(rows) >= _MAX_PROCESSES:
                break
    elif os.name == "nt":
        # Windows: tasklist gives image name + pid + memory.  No parent/cpu/etime
        # without extra queries; fill what tasklist /FO CSV provides.
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                text=True, timeout=5, check=False,
            )
            import csv as _csv
            import io as _io

            for row in _csv.reader(_io.StringIO(result.stdout)):
                if len(row) < 5:
                    continue
                name, pid = row[0], row[1]
                try:
                    mem = row[4].replace(" K", "").replace(",", "")
                    rss_kb = int(mem)
                except (ValueError, IndexError):
                    rss_kb = 0
                rows.append({
                    "pid": int(pid) if pid.isdigit() else None, "ppid": None,
                    "cpu_pct": None, "mem_pct": None, "rss_kb": rss_kb,
                    "etime": None, "name": name[:60], "state": None,
                })
                if len(rows) >= _MAX_PROCESSES:
                    break
        except Exception:
            return rows
    rows.sort(key=lambda r: r.get("rss_kb") or 0, reverse=True)
    return rows[:_MAX_PROCESSES]


def _probe_logs(cap: int = 500) -> list[dict[str, Any]]:
    """Log directory summary: path, entry count, total bytes.  Never content."""
    summary: list[dict[str, Any]] = []
    for log_dir in _LOG_DIRS:
        entry = {
            "path": log_dir,
            "exists": os.path.isdir(log_dir),
            "readable": os.access(log_dir, os.R_OK),
            "file_count": 0,
            "total_bytes": 0,
        }
        if entry["exists"] and entry["readable"]:
            count = 0
            try:
                for root, _dirs, files in os.walk(log_dir):
                    for name in files:
                        try:
                            entry["total_bytes"] += os.path.getsize(os.path.join(root, name))
                            count += 1
                        except OSError:
                            pass
                        if count >= cap:
                            break
                    if count >= cap:
                        break
                entry["file_count"] = count
                if count >= cap:
                    entry["truncated"] = True
            except OSError:
                entry["readable"] = False
        else:
            try:
                os.stat(log_dir)
            except FileNotFoundError:
                entry["error"] = "not_found"
            except PermissionError:
                entry["error"] = "permission"
        summary.append(entry)
    return summary


def _probe_platform() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def system_probe_payload(*, max_processes: int | None = None,
                         include_logs: bool = True,
                         log_cap: int = 500) -> dict[str, Any]:
    """Build the full ``modus.system.v1`` snapshot dict.  Pure stdlib, bounded."""
    processes = _probe_processes()
    if max_processes is not None:
        processes = processes[: max(max_processes, 0)]
    payload: dict[str, Any] = {
        "schema": "modus.system.v1",
        "timestamp": time.time(),
        "platform": _probe_platform(),
        "cpu": _probe_cpu(),
        "memory": _probe_memory(),
        "disk": _probe_disk(),
        "processes": processes,
        "process_count_capped": len(processes) >= (max_processes or _MAX_PROCESSES),
        "sources": [],
    }
    if include_logs:
        payload["logs"] = _probe_logs(cap=log_cap)
    return payload


async def system_probe(payload: dict[str, Any], context: Any) -> Any:
    """Tool handler: return the bounded host snapshot as compact JSON."""
    from modus.tools.base import ToolResult

    max_processes = int(payload.get("max_processes") or _MAX_PROCESSES)
    include_logs = bool(payload.get("include_logs", True))
    snapshot = system_probe_payload(
        max_processes=max_processes, include_logs=include_logs,
    )
    import json

    text = json.dumps(snapshot, ensure_ascii=False, default=str)
    # Keep only small scalars in metadata so the timeline can show one line
    # without parsing the whole snapshot.
    cpu = snapshot.get("cpu", {})
    disk_free = next(
        (round(d.get("free_pct", 0), 1) for d in snapshot.get("disk", []) if d.get("path") == "/"),
        None,
    )
    metadata = {
        "operation": "system_probe",
        "schema": "modus.system.v1",
        "cpu_loadavg_1m": cpu.get("loadavg_1m"),
        "disk_free_pct": disk_free,
        "process_count": len(snapshot.get("processes", [])),
    }
    return ToolResult(text, metadata=metadata)
