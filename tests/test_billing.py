"""Local billing: price conversion, recharge, atomic charge, aggregation."""

from __future__ import annotations

import json

import pytest

from modus.desktop import accounts, billing, db


@pytest.fixture
def user_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    monkeypatch.setattr(billing, "_pricing_path", lambda: tmp_path / "pricing.json")
    db.init_db()
    return tmp_path


def _owner() -> str:
    return str(accounts.ensure_default_user()["user_id"])


def test_compute_charge_cents_rounds_up():
    price = {"input": 0.27, "output": 1.10}
    # 10_000 in + 0 out => 0.0027 USD => 0.27 cents => ceil => 1 cent
    assert billing.compute_charge_cents(input_tokens=10_000, output_tokens=0, price=price) == 1
    # free tokens cover a small run entirely
    assert billing.compute_charge_cents(
        input_tokens=10_000, output_tokens=0, price=price, free_tokens=20_000,
    ) == 0
    # zero price => zero cost
    assert billing.compute_charge_cents(
        input_tokens=1_000_000, output_tokens=1_000_000, price={"input": 0, "output": 0},
    ) == 0


def test_price_fallback_for_unknown_model():
    pricing = billing.load_pricing()
    exact = billing.price_for_model(pricing, "deepseek-v4-flash")
    assert exact["input"] == 0.27
    fallback = billing.price_for_model(pricing, "totally-unknown-model")
    assert fallback["input"] == pricing["fallback"]["input"]


def test_recharge_increases_balance(user_db):
    uid = _owner()
    assert billing.get_balance(uid)["balance_cents"] == 0
    billing.recharge(uid, 500, "intro")
    assert billing.get_balance(uid)["balance_cents"] == 500


def test_charge_debits_and_updates_lifetime(user_db):
    uid = _owner()
    billing.recharge(uid, 1000)
    debit = billing.charge_run(
        user_id=uid, run_id="r1", model_id="deepseek-v4-flash",
        input_tokens=100_000, output_tokens=20_000,
    )
    assert debit < 0
    bal = billing.get_balance(uid)
    assert bal["balance_cents"] == 1000 + debit
    assert bal["lifetime_cents"] == -debit


def test_charge_is_idempotent(user_db):
    uid = _owner()
    billing.recharge(uid, 1000)
    first = billing.charge_run(user_id=uid, run_id="r1", model_id="m", input_tokens=10_000, output_tokens=0)
    second = billing.charge_run(user_id=uid, run_id="r1", model_id="m", input_tokens=10_000, output_tokens=0)
    assert second == 0
    assert billing.get_balance(uid)["balance_cents"] == 1000 + first


def test_charge_never_goes_below_zero(user_db):
    uid = _owner()
    debit = billing.charge_run(user_id=uid, run_id="r2", model_id="m", input_tokens=999_999, output_tokens=999_999)
    assert billing.get_balance(uid)["balance_cents"] == 0
    assert debit == 0  # nothing to debit


def test_sufficient_balance(user_db):
    uid = _owner()
    assert billing.sufficient_balance(uid) is True  # 0 >= 0
    assert billing.sufficient_balance(uid, 10) is False
    billing.recharge(uid, 100)
    assert billing.sufficient_balance(uid, 50) is True


def test_settlement_charges_atomically(user_db):
    """settle_run_event debits in the same transaction as the terminal state."""
    uid = _owner()
    billing.recharge(uid, 500)
    sess = db.create_session("billing")
    admitted = db.create_run_admission(
        "run-set", sess["id"], "default",
        config_snapshot={"host_model_id": "deepseek-v4-flash"},
        root_title="t", root_description="d", root_actor_id="p", root_actor_label="Host",
    )
    assert admitted.get("owner_id") == uid
    event = {
        "event_id": "evt-1", "run_id": "run-set", "channel_id": "user_host",
        "sequence": 1, "timestamp": "2026-08-04T00:00:00Z", "mode": "default",
        "actor": {"kind": "agent", "id": "h"}, "type": "run_completed",
        "status": "completed", "revision": 0, "part_id": "p",
        "payload": {"stop_reason": "completed",
                    "budget": {"input_tokens": 50_000, "output_tokens": 10_000}},
    }
    assert db.settle_run_event(sess["id"], event) is True
    # Balance was debited for this run.
    charge = billing.run_usage(uid, "run-set")
    assert charge is not None
    assert charge["delta_cents"] < 0
    assert billing.get_balance(uid)["balance_cents"] == 500 + charge["delta_cents"]


def test_settlement_best_effort_when_billing_off(user_db, monkeypatch):
    """Billing failure during settlement must not block the terminal state."""
    uid = _owner()
    sess = db.create_session("billing-off")
    admitted = db.create_run_admission(
        "run-off", sess["id"], "default",
        config_snapshot={"host_model_id": "deepseek-v4-flash"},
        root_title="t", root_description="d", root_actor_id="p", root_actor_label="Host",
    )
    assert admitted.get("owner_id") == uid

    def boom(**kwargs):
        raise RuntimeError("pricing table corrupted")

    monkeypatch.setattr(billing, "charge_run", boom)
    event = {
        "event_id": "evt-2", "run_id": "run-off", "channel_id": "user_host",
        "sequence": 1, "timestamp": "2026-08-04T00:00:00Z", "mode": "default",
        "actor": {"kind": "agent", "id": "h"}, "type": "run_completed",
        "status": "completed", "revision": 0, "part_id": "p",
        "payload": {"stop_reason": "completed", "budget": {"input_tokens": 1, "output_tokens": 1}},
    }
    assert db.settle_run_event(sess["id"], event) is True
    assert db.get_run("run-off")["state"] == "completed"


def test_daily_and_model_aggregation(user_db):
    uid = _owner()
    billing.recharge(uid, 10_000)
    billing.charge_run(user_id=uid, run_id="a", model_id="deepseek-v4-flash",
                       input_tokens=100_000, output_tokens=10_000)
    billing.charge_run(user_id=uid, run_id="b", model_id="deepseek-v4-flash",
                       input_tokens=50_000, output_tokens=5_000)
    billing.charge_run(user_id=uid, run_id="c", model_id="other",
                       input_tokens=200_000, output_tokens=20_000)

    daily = billing.daily_usage(uid)
    assert len(daily) == 1
    assert daily[0]["input_tokens"] == 350_000
    assert daily[0]["output_tokens"] == 35_000
    assert daily[0]["cost_cents"] > 0

    models = billing.model_usage(uid)
    assert len(models) == 2
    by_model = {m["model_id"]: m for m in models}
    assert by_model["deepseek-v4-flash"]["runs"] == 2

    summary = billing.usage_summary(uid)
    assert summary["total_recharge_cents"] == 10_000
    assert summary["lifetime_cents"] > 0
    assert summary["balance_cents"] >= 0
