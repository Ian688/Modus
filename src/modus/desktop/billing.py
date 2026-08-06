"""Local billing: price table, token→cost conversion, balance ledger.

Money is stored as integer cents to avoid float drift.  Charging happens
atomically inside ``settle_run_event``'s ``BEGIN IMMEDIATE`` transaction
(see ``db.settle_run_event``), so a run's terminal state and its balance
debit are one durable fact.  The ledger is append-only with a
``UNIQUE(user_id, run_id)`` partial index on ``kind='charge'`` for idempotency.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

# Default price table (USD per 1M tokens, cents as floats for readability).
# The fallback entry guards every model not explicitly priced.
_DEFAULT_PRICING: dict[str, Any] = {
    "fallback": {"input": 2.0, "output": 8.0},  # USD / 1M tokens
    "free_tokens": 0,  # tokens consumed before charging begins
    "models": {
        "deepseek-v4-flash": {"input": 0.27, "output": 1.10},
        "deepseek-v3": {"input": 0.27, "output": 1.10},
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    },
}

_PRICING_DIR_ENV = "MODUS_PRICING_DIR"


def _pricing_path() -> Path:
    base = Path(os.environ.get(_PRICING_DIR_ENV, str(Path.home() / ".modus")))
    return base / "pricing.json"


def load_pricing() -> dict[str, Any]:
    """Load the user price table, falling back to defaults on any error."""
    try:
        path = _pricing_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "fallback" in data:
                return data
    except Exception:
        pass
    return json.loads(json.dumps(_DEFAULT_PRICING))


def save_pricing(pricing: dict[str, Any]) -> None:
    path = _pricing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(pricing, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def price_for_model(pricing: dict[str, Any], model_id: str) -> dict[str, float]:
    """Resolve per-model price or the fallback. ``model_id`` may be empty."""
    models = pricing.get("models") or {}
    key = str(model_id or "")
    if key and isinstance(models.get(key), dict):
        entry = models[key]
        return {
            "input": float(entry.get("input") or 0),
            "output": float(entry.get("output") or 0),
        }
    fallback = pricing.get("fallback") or {}
    return {
        "input": float(fallback.get("input") or 0),
        "output": float(fallback.get("output") or 0),
    }


def compute_charge_cents(
    *, input_tokens: int, output_tokens: int, price: dict[str, float],
    free_tokens: int = 0,
) -> int:
    """Convert token usage to integer cents.

    ``free_tokens`` are consumed first against the combined token total, so a
    small allowance can keep low-usage runs effectively free.
    """
    combined = max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0))
    if free_tokens > 0:
        combined = max(0, combined - int(free_tokens))
    if combined <= 0:
        return 0
    in_px = float(price.get("input") or 0)
    out_px = float(price.get("output") or 0)
    if in_px <= 0 and out_px <= 0:
        return 0
    # Scale the free-token deduction proportionally across input/output so the
    # mixed price stays consistent.
    total = max(1, int(input_tokens or 0) + int(output_tokens or 0))
    eff_in = max(0, int(input_tokens or 0) * combined // total)
    eff_out = max(0, int(output_tokens or 0) * combined // total)
    cost = (eff_in * in_px + eff_out * out_px) / 1_000_000.0
    return int(math.ceil(cost * 100))  # cents, round up (never undercharge)


# ── Account / ledger queries ──


def _conn():
    from modus.desktop import db

    return db._get_conn()


def get_balance(user_id: str) -> dict[str, int]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT balance_cents, lifetime_cents FROM accounts WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
    if not row:
        return {"balance_cents": 0, "lifetime_cents": 0}
    return {
        "balance_cents": int(row["balance_cents"] or 0),
        "lifetime_cents": int(row["lifetime_cents"] or 0),
    }


def recharge(user_id: str, amount_cents: int, note: str = "") -> dict[str, int]:
    """Add local balance (充值). Returns the new balance."""
    amount = max(0, int(amount_cents))
    now = time.time()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT balance_cents FROM accounts WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
        balance = int(row["balance_cents"] or 0) if row else 0
        balance += amount
        conn.execute(
            """INSERT INTO accounts (user_id, balance_cents, lifetime_cents, updated_at)
               VALUES (?,?,0,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 balance_cents=excluded.balance_cents,
                 updated_at=excluded.updated_at""",
            (str(user_id), balance, now),
        )
        conn.execute(
            """INSERT INTO billing_ledger
               (user_id, run_id, kind, delta_cents, balance_after_cents,
                model_id, input_tokens, output_tokens, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(user_id), "", "recharge", amount, balance,
             "", 0, 0, now),
        )
        conn.execute(
            """INSERT INTO recharge_records
               (recharge_id, user_id, amount_cents, note, created_at)
               VALUES (?,?,?,?,?)""",
            (f"re_{int(now)}", str(user_id), amount, str(note)[:200], now),
        )
    return {"balance_cents": balance}


def charge_run(
    *,
    user_id: str, run_id: str, model_id: str,
    input_tokens: int, output_tokens: int,
    conn=None,
) -> int:
    """Atomically debit balance for one run. Returns delta cents (≤0).

    Idempotent: the ``UNIQUE(user_id, run_id) WHERE kind='charge'`` partial
    index makes a second charge for the same run a no-op.

    ``conn`` may be an open sqlite connection (used inside
    ``settle_run_event``'s BEGIN IMMEDIATE transaction); otherwise a new
    connection is opened.
    """
    pricing = load_pricing()
    price = price_for_model(pricing, model_id)
    free = int(pricing.get("free_tokens") or 0)
    delta = -compute_charge_cents(
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        price=price, free_tokens=free,
    )
    now = time.time()

    def _run(c):
        # Inside an existing transaction (settle_run_event) the caller already
        # holds BEGIN IMMEDIATE; only a fresh connection starts its own.
        if conn is None:
            c.execute("BEGIN IMMEDIATE")
        existing = c.execute(
            "SELECT 1 FROM billing_ledger WHERE user_id=? AND run_id=? AND kind='charge'",
            (str(user_id), str(run_id)),
        ).fetchone()
        if existing:
            return 0
        row = c.execute(
            "SELECT balance_cents FROM accounts WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
        balance = int(row["balance_cents"] or 0) if row else 0
        debit = max(delta, -balance)
        new_balance = balance + debit
        c.execute(
            """INSERT INTO accounts (user_id, balance_cents, lifetime_cents, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 balance_cents=excluded.balance_cents,
                 lifetime_cents=lifetime_cents+?,
                 updated_at=excluded.updated_at""",
            (str(user_id), new_balance, abs(debit), now, abs(debit)),
        )
        c.execute(
            """INSERT INTO billing_ledger
               (user_id, run_id, kind, delta_cents, balance_after_cents,
                model_id, input_tokens, output_tokens, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(user_id), str(run_id), "charge", debit, new_balance,
             str(model_id or ""), int(input_tokens or 0),
             int(output_tokens or 0), now),
        )
        return debit

    if conn is not None:
        return _run(conn)
    with _conn() as conn:
        return _run(conn)


def sufficient_balance(user_id: str, minimum_cents: int = 0) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT balance_cents FROM accounts WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
    balance = int(row["balance_cents"] or 0) if row else 0
    return balance >= max(0, int(minimum_cents))


# ── Aggregation ──


def daily_usage(user_id: str, days: int = 14) -> list[dict[str, Any]]:
    """Per-day token + cost aggregation from the ledger."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT date(created_at, 'unixepoch') AS day,
                      SUM(input_tokens) AS in_tokens,
                      SUM(output_tokens) AS out_tokens,
                      SUM(-delta_cents) AS cost_cents
               FROM billing_ledger
               WHERE user_id=? AND kind='charge'
                 AND created_at > ?
               GROUP BY day ORDER BY day ASC""",
            (str(user_id), time.time() - days * 86400),
        ).fetchall()
    return [
        {
            "day": str(r["day"]),
            "input_tokens": int(r["in_tokens"] or 0),
            "output_tokens": int(r["out_tokens"] or 0),
            "cost_cents": int(r["cost_cents"] or 0),
        }
        for r in rows
    ]


def model_usage(user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT model_id, SUM(input_tokens) AS in_tokens,
                      SUM(output_tokens) AS out_tokens,
                      SUM(-delta_cents) AS cost_cents,
                      COUNT(*) AS runs
               FROM billing_ledger
               WHERE user_id=? AND kind='charge'
               GROUP BY model_id
               ORDER BY cost_cents DESC LIMIT ?""",
            (str(user_id), max(1, int(limit))),
        ).fetchall()
    return [
        {
            "model_id": str(r["model_id"] or "fallback"),
            "input_tokens": int(r["in_tokens"] or 0),
            "output_tokens": int(r["out_tokens"] or 0),
            "cost_cents": int(r["cost_cents"] or 0),
            "runs": int(r["runs"] or 0),
        }
        for r in rows
    ]


def run_usage(user_id: str, run_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            """SELECT run_id, kind, delta_cents, balance_after_cents, model_id,
                      input_tokens, output_tokens, created_at
               FROM billing_ledger
               WHERE user_id=? AND run_id=? AND kind='charge'""",
            (str(user_id), str(run_id)),
        ).fetchone()
    return dict(row) if row else None


def usage_summary(user_id: str) -> dict[str, Any]:
    """One aggregate payload for the account center."""
    balance = get_balance(user_id)
    daily = daily_usage(user_id)
    models = model_usage(user_id)
    with _conn() as conn:
        recharge_row = conn.execute(
            """SELECT COALESCE(SUM(amount_cents),0) AS total
               FROM recharge_records WHERE user_id=?""",
            (str(user_id),),
        ).fetchone()
    total_recharge = int(recharge_row["total"] or 0) if recharge_row else 0
    return {
        "balance_cents": balance["balance_cents"],
        "lifetime_cents": balance["lifetime_cents"],
        "total_recharge_cents": total_recharge,
        "daily": daily,
        "models": models,
    }
