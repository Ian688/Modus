"""Local user accounts: registration, password lock, and per-user isolation.

Modus is a single-machine desktop app bound to loopback.  Users are a
front-end-led soft isolation (sessions/runs/workspaces carry an ``owner_id``)
plus an optional password lock.  No cloud, no network egress.

Password hashing uses scrypt via :mod:`hashlib` (stdlib, no dependencies).
Session tokens are stored as hashes only, so a DB leak does not expose usable
tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid
from typing import Any

_LOCAL_DEFAULT_USERNAME = "local"

_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def _derive_salt() -> str:
    return secrets.token_hex(16)


def _scrypt_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("utf-8"),
        n=2**14, r=8, p=1, dklen=64,
    ).hex()


def _public_user(row: dict[str, Any]) -> dict[str, Any]:
    """Browser-safe user record: never leaks the password hash or salt."""
    return {
        "user_id": str(row["user_id"]),
        "username": str(row["username"]),
        "is_local_default": bool(row.get("is_local_default")),
        "has_password": bool(str(row.get("password_hash") or "")),
    }


# ── Users ──


def _row_to_dict(cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    return dict(row) if row else None


def get_user(user_id: str) -> dict[str, Any] | None:
    from modus.desktop import db

    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (str(user_id),),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict[str, Any] | None:
    from modus.desktop import db

    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (str(username),),
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    from modus.desktop import db

    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY is_local_default DESC, created_at",
        ).fetchall()
    return [_public_user(dict(row)) for row in rows]


def ensure_default_user(conn=None) -> dict[str, Any]:
    """Idempotently create the local default user (no password, auto-login).

    Returns the default user record.  This is the ownership anchor for all
    pre-existing sessions/runs/workspaces, so first launch is unchanged.

    ``conn`` may be an open sqlite connection (used by init_db to stay inside
    one transaction); otherwise a new connection is opened.
    """
    from modus.desktop import db

    now = time.time()

    def _run(c):
        row = c.execute(
            "SELECT * FROM users WHERE is_local_default=1", {},
        ).fetchone()
        if row:
            return dict(row)
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        c.execute(
            """INSERT INTO users (user_id, username, password_hash, salt,
                                  is_local_default, created_at, updated_at)
               VALUES (?,?,?,?,1,?,?)""",
            (user_id, _LOCAL_DEFAULT_USERNAME, "", "", now, now),
        )
        c.execute(
            """INSERT INTO accounts (user_id, balance_cents, lifetime_cents, updated_at)
               VALUES (?,0,0,?)""",
            (user_id, now),
        )
        return {"user_id": user_id, "username": _LOCAL_DEFAULT_USERNAME,
                "password_hash": "", "salt": "", "is_local_default": 1,
                "created_at": now, "updated_at": now}

    if conn is not None:
        return _run(conn)
    with db._get_conn() as conn:
        return _run(conn)


def create_user(username: str, password: str = "") -> dict[str, Any]:
    """Create a user. Empty password means passwordless (auto-login)."""
    from modus.desktop import db

    name = str(username or "").strip()
    if not name:
        raise ValueError("username is required")
    if name == _LOCAL_DEFAULT_USERNAME:
        raise ValueError("username reserved")
    now = time.time()
    salt = _derive_salt()
    pwd_hash = _scrypt_hash(password, salt) if password else ""
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    with db._get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO users (user_id, username, password_hash, salt,
                                      is_local_default, created_at, updated_at)
                   VALUES (?,?,?,?,0,?,?)""",
                (user_id, name, pwd_hash, salt, now, now),
            )
        except Exception:
            raise ValueError(f"username already exists: {name}")
        conn.execute(
            """INSERT INTO accounts (user_id, balance_cents, lifetime_cents, updated_at)
               VALUES (?,0,0,?)""",
            (user_id, now),
        )
    return _public_user({
        "user_id": user_id, "username": name, "is_local_default": 0,
        "password_hash": pwd_hash,
    })


# ── Authentication ──


def verify_login(username: str, password: str) -> dict[str, Any] | None:
    """Return the user record if credentials are valid, else None."""
    user = get_user_by_username(str(username or "").strip())
    if not user:
        return None
    stored = str(user.get("password_hash") or "")
    if not stored:
        # Passwordless user: matches without a password.
        return _public_user(user)
    salt = str(user.get("salt") or "")
    candidate = _scrypt_hash(str(password or ""), salt)
    if hmac.compare_digest(candidate, stored):
        return _public_user(user)
    return None


def set_password(user_id: str, new_password: str) -> bool:
    """Set or clear a user's password (empty clears the lock)."""
    user = get_user(str(user_id or ""))
    if not user:
        return False
    salt = _derive_salt()
    pwd_hash = _scrypt_hash(str(new_password or ""), salt) if new_password else ""
    from modus.desktop import db

    with db._get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, salt=?, updated_at=? WHERE user_id=?",
            (pwd_hash, salt, time.time(), str(user_id)),
        )
    return True


# ── Account management ──

_DEMO_USERNAME = "demo"
_DEMO_PASSWORD = "123456"


def rename_user(user_id: str, new_username: str) -> dict[str, Any]:
    """Rename a user. Raises ValueError on empty/reserved/duplicate names."""
    from modus.desktop import db

    name = str(new_username or "").strip()
    if not name:
        raise ValueError("用户名不能为空")
    if name == _LOCAL_DEFAULT_USERNAME:
        raise ValueError("该用户名已保留")
    if get_user_by_username(name) is not None:
        raise ValueError(f"用户名已存在: {name}")
    with db._get_conn() as conn:
        cursor = conn.execute(
            "UPDATE users SET username=?, updated_at=? WHERE user_id=?",
            (name, time.time(), str(user_id)),
        )
    if cursor.rowcount == 0:
        raise ValueError("用户不存在")
    user = get_user(str(user_id))
    return _public_user(user)


def ensure_demo_user() -> dict[str, Any]:
    """Idempotently create the demo account (demo/123456).

    The demo password is deliberately pinned so the pre-filled login card
    always works: an existing demo user is re-locked to ``123456`` rather than
    drifting after a manual password change.
    """
    from modus.desktop import db

    now = time.time()
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (_DEMO_USERNAME,),
        ).fetchone()
        if row:
            salt = _derive_salt()
            pwd_hash = _scrypt_hash(_DEMO_PASSWORD, salt)
            conn.execute(
                "UPDATE users SET password_hash=?, salt=?, updated_at=? WHERE user_id=?",
                (pwd_hash, salt, now, str(row["user_id"])),
            )
            return _public_user(dict(row))
        salt = _derive_salt()
        pwd_hash = _scrypt_hash(_DEMO_PASSWORD, salt)
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO users (user_id, username, password_hash, salt,
                                  is_local_default, created_at, updated_at)
               VALUES (?,?,?,?,0,?,?)""",
            (user_id, _DEMO_USERNAME, pwd_hash, salt, now, now),
        )
        conn.execute(
            """INSERT INTO accounts (user_id, balance_cents, lifetime_cents, updated_at)
               VALUES (?,0,0,?)""",
            (user_id, now),
        )
        return {
            "user_id": user_id, "username": _DEMO_USERNAME,
            "has_password": True, "is_local_default": False,
        }


def delete_user(user_id: str, *, delete_data: bool = False) -> bool:
    """Delete a user; optionally cascade-delete all owned data.

    Refuses to delete the last remaining user. Returns True on success.
    """
    from modus.desktop import db

    uid = str(user_id or "")
    user = get_user(uid)
    if user is None:
        raise ValueError("用户不存在")
    remaining = [u for u in list_users() if u["user_id"] != uid]
    if not remaining:
        raise ValueError("不能删除最后一个账户")
    with db._get_conn() as conn:
        if delete_data:
            db.delete_user_data(uid)
        conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
    return True


# ── Session tokens ──


def create_session_token(user_id: str) -> str:
    """Issue an opaque token; only its hash is persisted."""
    from modus.desktop import db

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with db._get_conn() as conn:
        conn.execute(
            """INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at)
               VALUES (?,?,?,?)""",
            (token_hash, str(user_id), now, now + _TOKEN_TTL_SECONDS),
        )
    return token


def resolve_session_token(token: str) -> dict[str, Any] | None:
    """Resolve a token to its user, or None if invalid/expired."""
    from modus.desktop import db

    token_hash = hashlib.sha256(str(token or "").encode()).hexdigest()
    now = time.time()
    with db._get_conn() as conn:
        row = conn.execute(
            """SELECT a.*, u.user_id AS owner_id, u.username, u.is_local_default
               FROM auth_sessions a JOIN users u ON u.user_id=a.user_id
               WHERE a.token_hash=? AND a.expires_at>?""",
            (token_hash, now),
        ).fetchone()
    if not row:
        return None
    return {
        "user_id": str(row["owner_id"]),
        "username": str(row["username"]),
        "is_local_default": bool(row["is_local_default"]),
    }


def revoke_session_token(token: str) -> None:
    from modus.desktop import db

    token_hash = hashlib.sha256(str(token or "").encode()).hexdigest()
    with db._get_conn() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
