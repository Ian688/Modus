"""Local auth commands: status, create, login, logout, switch, set-password."""

from __future__ import annotations

import asyncio

import pytest

from modus.desktop import accounts, db
from modus.desktop.auth_commands import (
    handle_auth_delete_user,
    handle_auth_demo_account,
    handle_auth_login,
    handle_auth_logout,
    handle_auth_rename_user,
    handle_auth_set_password,
    handle_auth_status,
    handle_auth_switch_user,
    handle_modus_account_status,
    handle_recharge,
    handle_usage_summary,
    handle_user_create,
)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class FakeSession:
    def __init__(self, owner_id: str = ""):
        self.owner_id = owner_id
        self.id = "s1"
        self.db_id = ""
        self.workspace_id = ""
        self.workspace_root = "/tmp"
        self.workspace_name = "tmp"
        self.mode = "default"
        self.model_id = ""
        self.mode_config = {}
        self.reasoning_effort = None
        self.worldview = ""


@pytest.fixture
def user_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


def test_auth_status_reports_default_user_and_others(user_db):
    accounts.create_user("zed", "pw")
    default = accounts.ensure_default_user()
    ws, sess = FakeWS(), FakeSession(default["user_id"])
    _run(handle_auth_status(ws, sess, {"type": "auth_status"}))
    status = ws.sent[-1]
    assert status["type"] == "auth_status"
    names = {u["username"] for u in status["users"]}
    assert {"local", "zed"} <= names
    assert status["current_user"]["username"] == "local"
    assert status["has_users_beyond_default"] is True


def test_user_create_rejects_duplicate(user_db):
    ws, sess = FakeWS(), FakeSession()
    _run(handle_user_create(ws, sess, {"type": "user_create", "username": "bob", "password": "p"}))
    assert ws.sent[-1]["type"] == "user_created"
    _run(handle_user_create(ws, sess, {"type": "user_create", "username": "bob", "password": "p"}))
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["code"] == "user_create_failed"


def test_login_sets_owner_and_issues_token(user_db):
    accounts.create_user("alice", "s3cret")
    ws, sess = FakeWS(), FakeSession()
    _run(handle_auth_login(ws, sess, {"type": "auth_login", "username": "alice", "password": "s3cret"}))
    ok = ws.sent[-1]
    assert ok["type"] == "auth_login_ok"
    assert ok["user"]["username"] == "alice"
    assert ok["token"]
    assert sess.owner_id == ok["user"]["user_id"]


def test_login_wrong_password_errors(user_db):
    accounts.create_user("alice", "s3cret")
    ws, sess = FakeWS(), FakeSession()
    _run(handle_auth_login(ws, sess, {"type": "auth_login", "username": "alice", "password": "nope"}))
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["code"] == "auth_failed"


def test_logout_returns_to_default(user_db):
    user = accounts.create_user("carol", "pw")
    default = accounts.ensure_default_user()
    ws, sess = FakeWS(), FakeSession(user["user_id"])
    _run(handle_auth_logout(ws, sess, {"type": "auth_logout"}))
    out = ws.sent[-1]
    assert out["type"] == "auth_logout_ok"
    assert out["user"]["user_id"] == default["user_id"]
    assert sess.owner_id == default["user_id"]


def test_switch_user_without_credentials(user_db):
    accounts.create_user("dave")
    default = accounts.ensure_default_user()
    ws, sess = FakeWS(), FakeSession(default["user_id"])
    dave = accounts.get_user_by_username("dave")
    _run(handle_auth_switch_user(ws, sess, {"type": "auth_switch_user", "user_id": dave["user_id"]}))
    out = ws.sent[-1]
    assert out["type"] == "auth_switch_ok"
    assert out["user"]["username"] == "dave"
    assert sess.owner_id == dave["user_id"]


def test_switch_user_rejects_password_protected_account(user_db):
    protected = accounts.create_user("locked", "secret")
    default = accounts.ensure_default_user()
    ws, sess = FakeWS(), FakeSession(default["user_id"])
    _run(handle_auth_switch_user(ws, sess, {
        "type": "auth_switch_user", "user_id": protected["user_id"],
    }))
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["code"] == "auth_required"
    assert sess.owner_id == default["user_id"]


def test_switch_user_clears_bound_conversation(user_db):
    guest = accounts.create_user("guest-two")
    default = accounts.ensure_default_user()
    ws, sess = FakeWS(), FakeSession(default["user_id"])
    sess.db_id = "old-session"
    sess.workspace_id = "old-workspace"
    sess.main_history = [object()]
    _run(handle_auth_switch_user(ws, sess, {
        "type": "auth_switch_user", "user_id": guest["user_id"],
    }))
    assert ws.sent[-1]["account_reset"] is True
    assert sess.db_id == ""
    assert sess.workspace_id == ""
    assert sess.main_history == []


def test_set_password_locks_current_user(user_db):
    user = accounts.create_user("erin")
    ws, sess = FakeWS(), FakeSession(user["user_id"])
    _run(handle_auth_set_password(ws, sess, {"type": "auth_set_password", "password": "lock"}))
    out = ws.sent[-1]
    assert out["type"] == "auth_set_password_ok"
    assert out["user"]["has_password"] is True
    assert accounts.verify_login("erin", "lock") is not None


def test_usage_summary_returns_balance_and_zero_usage(user_db):
    from modus.desktop import billing

    default = accounts.ensure_default_user()
    billing.recharge(default["user_id"], 300)
    ws, sess = FakeWS(), FakeSession(default["user_id"])
    _run(handle_usage_summary(ws, sess, {"type": "usage_summary", "request_id": "u1"}))
    out = ws.sent[-1]
    assert out["type"] == "usage_summary"
    assert out["summary"]["balance_cents"] == 300
    assert out["summary"]["daily"] == []


def test_recharge_updates_balance(user_db):
    default = accounts.ensure_default_user()
    ws, sess = FakeWS(), FakeSession(default["user_id"])
    _run(handle_recharge(ws, sess, {"type": "recharge", "amount_cents": 800, "note": "test", "request_id": "r1"}))
    out = ws.sent[-1]
    assert out["type"] == "recharge_done"
    assert out["amount_cents"] == 800
    assert out["balance_cents"] == 800


def test_recharge_rejects_invalid_amount(user_db):
    default = accounts.ensure_default_user()
    ws, sess = FakeWS(), FakeSession(default["user_id"])
    _run(handle_recharge(ws, sess, {"type": "recharge", "amount_cents": -5, "request_id": "r2"}))
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["code"] == "recharge_failed"


def test_auth_rename_user_ok_and_conflict(user_db):
    user = accounts.create_user("carol", "pw")
    ws, sess = FakeWS(), FakeSession(user["user_id"])
    _run(handle_auth_rename_user(ws, sess, {"type": "auth_rename_user", "new_username": "carol2"}))
    assert ws.sent[-1]["type"] == "user_renamed"
    assert ws.sent[-1]["user"]["username"] == "carol2"
    # duplicate name
    accounts.create_user("dave")
    _run(handle_auth_rename_user(ws, sess, {"type": "auth_rename_user", "new_username": "dave"}))
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["code"] == "user_rename_failed"


def test_auth_delete_user_cascades(user_db):
    user = accounts.create_user("erin", "pw")
    ws, sess = FakeWS(), FakeSession(user["user_id"])
    _run(handle_auth_delete_user(ws, sess, {"type": "auth_delete_user", "user_id": user["user_id"], "delete_data": True}))
    assert ws.sent[-1]["type"] == "user_deleted"
    assert accounts.get_user(user["user_id"]) is None


def test_auth_delete_active_user_switches_to_default(user_db):
    user = accounts.create_user("frank", "pw")
    default = accounts.ensure_default_user()
    ws, sess = FakeWS(), FakeSession(user["user_id"])
    _run(handle_auth_delete_user(ws, sess, {"type": "auth_delete_user", "delete_data": True}))
    assert ws.sent[-1]["type"] == "user_deleted"
    assert sess.owner_id == default["user_id"]


def test_auth_delete_last_user_rejected(user_db):
    # local + demo only; delete demo then local is last.
    demo = accounts.ensure_demo_user()
    ws, sess = FakeWS(), FakeSession(demo["user_id"])
    _run(handle_auth_delete_user(ws, sess, {"type": "auth_delete_user", "user_id": demo["user_id"], "delete_data": True}))
    assert ws.sent[-1]["type"] == "user_deleted"
    # Only local remains.
    _run(handle_auth_delete_user(ws, sess, {"type": "auth_delete_user", "user_id": accounts.ensure_default_user()["user_id"]}))
    assert ws.sent[-1]["type"] == "error"
    assert ws.sent[-1]["code"] == "user_delete_failed"


def test_auth_demo_returns_prefilled_credentials(user_db):
    ws, sess = FakeWS(), FakeSession()
    _run(handle_auth_demo_account(ws, sess, {"type": "auth_demo_account"}))
    out = ws.sent[-1]
    assert out["type"] == "demo_account"
    assert out["user"]["username"] == "demo"
    assert out["user"]["password"] == "123456"


def test_modus_account_status_unconfigured(user_db):
    ws, sess = FakeWS(), FakeSession()
    _run(handle_modus_account_status(ws, sess, {"type": "modus_account_status"}))
    out = ws.sent[-1]
    assert out["type"] == "modus_account_status"
    assert out["cloud"]["status"] == "unconfigured"
    assert out["cloud"]["configured"] is False
