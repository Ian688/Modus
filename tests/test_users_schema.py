"""User schema: default user, owner backfill, account creation, auth tokens."""

from __future__ import annotations

import pytest

from modus.desktop import accounts, db


@pytest.fixture
def user_db(tmp_path, monkeypatch):
    """Point the desktop DB at a temp file and init once."""
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    return tmp_path


def test_default_user_created_idempotently(user_db):
    first = accounts.ensure_default_user()
    second = accounts.ensure_default_user()
    assert first["user_id"] == second["user_id"]
    assert first["username"] == "local"
    assert first["is_local_default"] == 1


def test_default_user_owns_preexisting_rows(user_db):
    # Simulate legacy rows that predate the user schema.
    from modus.desktop.workspace import WorkspaceIdentity

    identity = WorkspaceIdentity.from_path(user_db)
    db.upsert_workspace(identity)
    session = db.create_session("Legacy")
    db.create_run("legacy-run", session["id"], "default")

    default = accounts.ensure_default_user()
    with db._get_conn() as conn:
        assert conn.execute(
            "SELECT owner_id FROM sessions WHERE id=?", (session["id"],),
        ).fetchone()["owner_id"] == default["user_id"]
        assert conn.execute(
            "SELECT owner_id FROM runs WHERE run_id=?", ("legacy-run",),
        ).fetchone()["owner_id"] == default["user_id"]


def test_session_and_workspace_queries_are_owner_isolated(user_db):
    from modus.desktop.workspace import WorkspaceIdentity

    local = accounts.ensure_default_user()
    other = accounts.create_user("isolated-user")
    workspace = WorkspaceIdentity.from_path(user_db)
    db.upsert_workspace(workspace, owner_id=local["user_id"])
    local_session = db.create_session(
        "Local only", owner_id=local["user_id"],
        workspace_id=workspace.workspace_id,
    )
    other_session = db.create_session(
        "Other only", owner_id=other["user_id"],
    )

    local_page = db.session_catalog_page(owner_id=local["user_id"])
    other_page = db.session_catalog_page(owner_id=other["user_id"])
    assert {row["id"] for row in local_page["sessions"]} == {local_session["id"]}
    assert {row["id"] for row in other_page["sessions"]} == {other_session["id"]}
    assert db.restore_session(other_session["id"], owner_id=local["user_id"]) is None
    assert db.get_workspace(
        workspace.workspace_id, owner_id=other["user_id"],
    ) is None


def test_workspace_default_and_forget_are_account_local_and_non_destructive(user_db, tmp_path):
    from modus.desktop.workspace import WorkspaceIdentity

    local = accounts.ensure_default_user()
    other = accounts.create_user("workspace-other")
    root = tmp_path / "remembered-project"
    root.mkdir()
    (root / "keep.txt").write_text("untouched", encoding="utf-8")
    workspace = WorkspaceIdentity.from_path(root)
    db.upsert_workspace(workspace, owner_id=local["user_id"])
    db.upsert_workspace(workspace, owner_id=other["user_id"])

    assert db.set_default_workspace(local["user_id"], workspace.workspace_id)
    assert db.get_default_workspace(local["user_id"])["workspace_id"] == workspace.workspace_id
    assert db.get_default_workspace(other["user_id"]) is None

    assert db.forget_workspace(local["user_id"], workspace.workspace_id) is True
    assert db.get_workspace(workspace.workspace_id, owner_id=local["user_id"]) is None
    assert db.get_workspace(workspace.workspace_id, owner_id=other["user_id"]) is not None
    assert (root / "keep.txt").read_text(encoding="utf-8") == "untouched"


def test_create_user_password_scrypt_and_login(user_db):
    user = accounts.create_user("alice", "s3cret!")
    assert user["username"] == "alice"
    assert user["has_password"] is True
    assert user["is_local_default"] is False

    ok = accounts.verify_login("alice", "s3cret!")
    assert ok and ok["user_id"] == user["user_id"]
    assert accounts.verify_login("alice", "wrong") is None
    assert accounts.verify_login("nobody", "x") is None


def test_duplicate_username_rejected(user_db):
    accounts.create_user("bob", "pw")
    with pytest.raises(ValueError):
        accounts.create_user("bob", "pw2")


def test_passwordless_user_logs_in_without_password(user_db):
    user = accounts.create_user("guest")
    assert user["has_password"] is False
    ok = accounts.verify_login("guest", "")
    assert ok and ok["user_id"] == user["user_id"]


def test_set_password_locks_and_clears(user_db):
    user = accounts.create_user("carol")
    assert user["has_password"] is False

    assert accounts.set_password(user["user_id"], "lock")
    assert accounts.verify_login("carol", "lock") is not None
    assert accounts.verify_login("carol", "") is None

    assert accounts.set_password(user["user_id"], "")
    assert accounts.verify_login("carol", "") is not None


def test_session_token_roundtrip_and_revoke(user_db):
    user = accounts.create_user("dave", "pw")
    token = accounts.create_session_token(user["user_id"])
    resolved = accounts.resolve_session_token(token)
    assert resolved["user_id"] == user["user_id"]
    assert resolved["username"] == "dave"

    accounts.revoke_session_token(token)
    assert accounts.resolve_session_token(token) is None


def test_ledger_tables_exist(user_db):
    with db._get_conn() as conn:
        for table in ("users", "auth_sessions", "accounts", "billing_ledger", "recharge_records"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone()
            assert row, f"missing table {table}"


def test_rename_user_updates_username(user_db):
    user = accounts.create_user("alice", "pw")
    renamed = accounts.rename_user(user["user_id"], "alice2")
    assert renamed["username"] == "alice2"
    assert accounts.get_user_by_username("alice2") is not None
    assert accounts.get_user_by_username("alice") is None


def test_rename_user_rejects_reserved_and_duplicate(user_db):
    user = accounts.create_user("bob", "pw")
    with pytest.raises(ValueError):
        accounts.rename_user(user["user_id"], "local")
    accounts.create_user("carol", "pw")
    with pytest.raises(ValueError):
        accounts.rename_user(user["user_id"], "carol")


def test_demo_user_created_idempotently_and_logs_in(user_db):
    first = accounts.ensure_demo_user()
    second = accounts.ensure_demo_user()
    assert first["user_id"] == second["user_id"]
    assert first["username"] == "demo"
    assert first["has_password"] is True
    assert accounts.verify_login("demo", "123456") is not None
    assert accounts.verify_login("demo", "wrong") is None


def test_demo_password_is_pinned_after_manual_change(user_db):
    # Even if the demo password was changed, ensure_demo_user re-locks it.
    demo = accounts.ensure_demo_user()
    accounts.set_password(demo["user_id"], "changed")
    assert accounts.verify_login("demo", "changed") is not None
    accounts.ensure_demo_user()
    assert accounts.verify_login("demo", "123456") is not None
    assert accounts.verify_login("demo", "changed") is None


def test_delete_user_soft_keeps_data(user_db):
    user = accounts.create_user("dave", "pw")
    sess = db.create_session("dave-sess", owner_id=user["user_id"])
    assert accounts.delete_user(user["user_id"], delete_data=False) is True
    assert accounts.get_user(user["user_id"]) is None
    assert db.get_session(sess["id"]) is not None  # data retained


def test_delete_user_cascade_removes_data(user_db):
    from modus.desktop import billing

    user = accounts.create_user("erin", "pw")
    billing.recharge(user["user_id"], 500)
    sess = db.create_session("erin-sess", owner_id=user["user_id"])
    db.create_run("erin-run", sess["id"], "default")
    db.add_memory_record(session_id=sess["id"], scope="session",
                         content="erin memory", category="fact")

    assert accounts.delete_user(user["user_id"], delete_data=True) is True
    with db._get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE owner_id=?", (user["user_id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM billing_ledger WHERE user_id=?", (user["user_id"],),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_delete_user_keeps_other_owner_data(user_db):
    a = accounts.create_user("frank", "pw")
    b = accounts.create_user("grace", "pw")
    sess_b = db.create_session("grace-sess", owner_id=b["user_id"])
    accounts.delete_user(a["user_id"], delete_data=True)
    assert db.get_session(sess_b["id"]) is not None
    assert accounts.get_user(b["user_id"]) is not None


def test_cannot_delete_last_remaining_user(user_db):
    accounts.delete_user(accounts.ensure_demo_user()["user_id"], delete_data=True)
    accounts.create_user("x", "pw")
    # Reduce to a single user (local) plus the temp one, delete down to one.
    accounts.delete_user(accounts.get_user_by_username("x")["user_id"], delete_data=True)
    # Only local remains.
    assert len(accounts.list_users()) == 1
    with pytest.raises(ValueError):
        accounts.delete_user(accounts.ensure_default_user()["user_id"])
