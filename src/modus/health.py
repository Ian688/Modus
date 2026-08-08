"""Process-level health watchdog for the Modus Desktop.

System health and run health are two different time scales.  Run-scoped
reliability (budget, verification, approval flow) covers a single run; this
module watches what the runs cannot: cross-session and cross-run failure modes
— a data directory filling up, an audit log ballooning, approvals left pending
after their timer was lost, a dead browser page.

Design:
- One resident task, spawned by the Desktop server at startup (parallel to
  ``install_process_cleanup``), sampling every ``interval`` seconds.
- Fail-safe: a probe that raises is logged and skipped — the watchdog never
  takes an action on bad telemetry, and it only ever cleans up Modus's own data
  directory (``~/.modus``) or its own in-process state (browser / broker).
- Explicit action enumeration: every action is one of the module-level
  ``Action`` constants and is recorded to the AuditLog with outcome
  ``watchdog.<action>`` so the loop is observable and auditable.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from modus.config import load_config
from modus.paths import data_dir

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 60.0
DEFAULT_DISK_FREE_MIN_MB = 512.0
DEFAULT_APPROVAL_STALE_SECONDS = 600.0

# ── Explicit action enumeration ──
PRUNE_CACHE = "PRUNE_CACHE"
RECYCLE_BROWSER = "RECYCLE_BROWSER"
DENY_STALE_APPROVALS = "DENY_STALE_APPROVALS"
NOTIFY_USER = "NOTIFY_USER"
ACTIONS = (PRUNE_CACHE, RECYCLE_BROWSER, DENY_STALE_APPROVALS, NOTIFY_USER)


class Probes:
    """Callable probes, replaced in tests.  ``notify`` is the user-facing
    banner sink (the server wires it to a WebSocket broadcast); the default is
    a no-op so the module works standalone."""

    disk_free_mb: Callable[[], float] | None = None
    audit_size: Callable[[], int] | None = None
    db_size: Callable[[], int] | None = None
    pending_approval_max_age: Callable[[], float | None] | None = None
    browser_alive: Callable[[], bool] | None = None
    notify: Callable[[str], Any] | None = None


def _default_disk_free_mb() -> float:
    base = data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(base).free / (1024 * 1024)


def _default_audit_size() -> int:
    config = load_config()
    path = Path(config.policy.audit_log_path).expanduser()
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _default_db_size() -> int:
    from modus.desktop import db

    try:
        return db.DB_PATH.stat().st_size + db.DB_PATH.with_name(
            db.DB_PATH.name + "-wal"
        ).stat().st_size
    except OSError:
        return 0


def _default_pending_approval_max_age() -> float | None:
    from modus.desktop.approvals import approval_broker

    ages = [
        age
        for (run_id, approval_id), _future in list(approval_broker._pending.items())
        if (age := approval_broker.pending_age_seconds(run_id, approval_id)) is not None
    ]
    return max(ages) if ages else None


def _default_browser_alive() -> bool:
    """True when the shared page exists and is not closed (or absent)."""
    try:
        from modus.tools import browser

        holder = browser._holder
        if holder is None:
            return True  # nothing launched yet — nothing to recycle
        try:
            return not holder["page"].is_closed()
        except Exception:
            return False
    except Exception:
        return True


def _wire_default_probes() -> None:
    if Probes.disk_free_mb is None:
        Probes.disk_free_mb = _default_disk_free_mb
    if Probes.audit_size is None:
        Probes.audit_size = _default_audit_size
    if Probes.db_size is None:
        Probes.db_size = _default_db_size
    if Probes.pending_approval_max_age is None:
        Probes.pending_approval_max_age = _default_pending_approval_max_age
    if Probes.browser_alive is None:
        Probes.browser_alive = _default_browser_alive
    if Probes.notify is None:
        Probes.notify = lambda _message: None


def _audit(entry: dict[str, Any]) -> None:
    """Best-effort audit of a watchdog action; never raise."""
    try:
        from modus.policy.audit_log import AuditLog

        config = load_config()
        action = str(entry.get("action") or "action")
        AuditLog(config.policy.audit_log_path).record(
            tool_name="watchdog",
            input_data={k: v for k, v in entry.items() if k not in {"outcome", "action"}},
            outcome=f"watchdog.{action}",
            approver="system",
            cwd="",
            phase="watchdog",
        )
    except Exception:
        logger.exception("watchdog audit write failed")


def _safe(name: str, probe: Callable[[], Any] | None) -> Any:
    if probe is None:
        return None
    try:
        return probe()
    except Exception as exc:
        logger.warning("watchdog probe %s failed: %s", name, exc)
        return None


async def sample() -> dict[str, Any]:
    """Run one sampling pass: probe, act, audit, and return the report.

    Runs in the event loop (probes are fast filesystem reads) so the browser
    recycle remedy can await ``_close_browser`` directly.  Every probe failure
    degrades to "no action"; every action taken is audited.
    """
    _wire_default_probes()

    disk_free_mb = _safe("disk_free_mb", Probes.disk_free_mb)
    audit_size = _safe("audit_size", Probes.audit_size)
    db_size = _safe("db_size", Probes.db_size)
    approval_age = _safe("pending_approval_max_age", Probes.pending_approval_max_age)
    browser_alive = _safe("browser_alive", Probes.browser_alive)

    config = load_config()
    actions: list[str] = []
    notes: dict[str, Any] = {}

    # ── Disk pressure -> PRUNE_CACHE (WAL checkpoint + artifact prune) ──
    disk_low = disk_free_mb is not None and disk_free_mb < DEFAULT_DISK_FREE_MIN_MB
    if disk_low:
        try:
            from modus.desktop import db

            actions.append(PRUNE_CACHE)
            _audit({
                "action": PRUNE_CACHE, "reason": "disk_low",
                "disk_free_mb": round(float(disk_free_mb), 1),
            })
            db.checkpoint_now()
            db.prune_expired(config=config)
            notes["prune_reported"] = True
        except Exception:
            logger.exception("watchdog PRUNE_CACHE failed")

    # ── Browser page dead -> RECYCLE_BROWSER ──
    if browser_alive is False:
        actions.append(RECYCLE_BROWSER)
        _audit({"action": RECYCLE_BROWSER, "reason": "page_closed"})
        try:
            from modus.tools.browser import _close_browser

            await _close_browser()
            notes["browser_recycled"] = True
        except Exception:
            logger.exception("watchdog RECYCLE_BROWSER failed")

    # ── Stale pending approvals -> DENY_STALE_APPROVALS ──
    if approval_age is not None and float(approval_age) >= DEFAULT_APPROVAL_STALE_SECONDS:
        try:
            from modus.desktop.approvals import approval_broker

            denied = approval_broker.deny_stale(
                max_age_seconds=DEFAULT_APPROVAL_STALE_SECONDS,
            )
            if denied:
                actions.append(DENY_STALE_APPROVALS)
                _audit({
                    "action": DENY_STALE_APPROVALS, "reason": "stale_approval",
                    "max_age_seconds": DEFAULT_APPROVAL_STALE_SECONDS, "denied": denied,
                })
        except Exception:
            logger.exception("watchdog DENY_STALE_APPROVALS failed")

    # ── Audit log ballooning / low disk -> NOTIFY_USER (WS banner) ──
    notify_threshold = max(1, config.storage.audit_rotate_bytes)
    audit_big = audit_size is not None and int(audit_size) >= notify_threshold
    if disk_low or audit_big:
        try:
            message = (
                "Modus 数据目录空间不足，已自动清理缓存。"
                if disk_low
                else "Modus 审计日志已接近轮转阈值。"
            )
            actions.append(NOTIFY_USER)
            _audit({"action": NOTIFY_USER, "reason": "disk_low" if disk_low else "audit_oversized"})
            Probes.notify(message)
            notes["notified"] = True
        except Exception:
            logger.exception("watchdog NOTIFY_USER failed")

    return {
        "disk_free_mb": disk_free_mb,
        "audit_bytes": audit_size,
        "db_bytes": db_size,
        "pending_approval_max_age": approval_age,
        "browser_alive": browser_alive,
        "actions": actions,
        "notes": notes,
    }


class Watchdog:
    """Resident periodic sampler spawned by the Desktop server."""

    def __init__(self, interval: float = DEFAULT_INTERVAL):
        self.interval = max(1.0, float(interval))
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await sample()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("watchdog sample failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.get_event_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


_watchdog = Watchdog()


def install_watchdog(interval: float = DEFAULT_INTERVAL) -> None:
    """Spawn the resident watchdog task.  Idempotent.

    Called by the Desktop server at startup, parallel to
    ``install_process_cleanup``.  The task is best-effort: a failing sample is
    logged and the loop continues.
    """
    global _watchdog
    if _watchdog._task is not None and not _watchdog._task.done():
        return
    _watchdog = Watchdog(interval=interval)
    _watchdog.start()
