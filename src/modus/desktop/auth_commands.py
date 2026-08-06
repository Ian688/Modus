"""WebSocket auth/user commands for the Desktop server.

Registered on the ``DesktopCommandRouter``.  Handlers follow the router's
``(websocket, session, message)`` signature and reply with browser-safe
payloads (never password hashes or salts).
"""
from __future__ import annotations

from typing import Any
from pathlib import Path

from modus.desktop import accounts
from modus.redact import redact_text


async def _send(websocket, payload: dict[str, Any]) -> None:
    await websocket.send_json(payload)


def _request_id(message: dict[str, Any]) -> str:
    return str(message.get("request_id") or "")[:128]


def _account_switch_busy(session: Any) -> bool:
    task = getattr(session, "active_run_task", None)
    return bool(
        (task is not None and not task.done())
        or getattr(session, "active_controller", None) is not None
        or getattr(session, "active_run_id", None)
    )


async def _change_account(
    websocket: Any, session: Any, user: dict[str, Any], *,
    packet_type: str, operation: str, request_id: str, token: str = "",
    extra: dict[str, Any] | None = None,
) -> bool:
    """Atomically enter another account with no conversation state attached."""
    if _account_switch_busy(session):
        await _send(websocket, {
            "type": "error", "code": "account_switch_busy",
            "operation": operation, "request_id": request_id,
            "message": "Agent 任务仍在运行，结束后才能切换账号。",
        })
        return False
    session.owner_id = str(user["user_id"])
    session.db_id = ""
    session.workspace_id = ""
    session.workspace_root = ""
    session.workspace_name = ""
    session.worldview = ""
    session.world_view_history = []
    session.system_prompt = ""
    session.model_id = ""
    session.mode = "default"
    session.mode_config = {}
    session.reasoning_effort = None
    session.main_history = []
    session.model_discovery = {}
    session.pending_session_create_key = None
    session.engine = None
    public_user = {
        "user_id": str(user["user_id"]),
        "username": str(user["username"]),
        "is_local_default": bool(user.get("is_local_default")),
        "has_password": bool(
            user.get("has_password")
            if "has_password" in user
            else str(user.get("password_hash") or "")
        ),
    }
    payload = {
        "type": packet_type, "operation": operation,
        "request_id": request_id, "account_reset": True,
        "runtime_session_id": str(getattr(session, "id", "") or ""),
        "db_id": "", "workspace": None,
        "user": public_user,
    }
    if token:
        payload["token"] = token
    if extra:
        payload.update(extra)
    await _send(websocket, payload)
    return True


async def handle_auth_status(websocket, session, message: dict[str, Any]) -> None:
    """Report the current user session state (soft, front-end-driven)."""
    from modus.desktop import db

    users = accounts.list_users()
    locked_users = [
        u for u in users if u["has_password"]
    ]
    current_id = str(getattr(session, "owner_id", "") or "")
    current_user = None
    if current_id:
        row = accounts.get_user(current_id)
        if row:
            current_user = {
                "user_id": str(row["user_id"]),
                "username": str(row["username"]),
                "is_local_default": bool(row.get("is_local_default")),
                "has_password": bool(str(row.get("password_hash") or "")),
            }
    await _send(websocket, {
        "type": "auth_status",
        "operation": "auth_status",
        "request_id": _request_id(message),
        "users": users,
        "current_user": current_user,
        "password_lock_available": bool(locked_users),
        "has_users_beyond_default": any(
            not u["is_local_default"] for u in users
        ),
    })


async def handle_user_create(websocket, session, message: dict[str, Any]) -> None:
    """Create a local user (and optionally an account)."""
    username = str(message.get("username") or "").strip()
    password = str(message.get("password") or "")
    try:
        user = accounts.create_user(username, password)
    except ValueError as exc:
        await _send(websocket, {
            "type": "error", "code": "user_create_failed",
            "operation": "user_create", "request_id": _request_id(message),
            "message": str(exc),
        })
        return
    await _send(websocket, {
        "type": "user_created", "operation": "user_create",
        "request_id": _request_id(message), "user": user,
    })


async def handle_auth_login(websocket, session, message: dict[str, Any]) -> None:
    """Verify credentials and switch the session to that user."""
    username = str(message.get("username") or "").strip()
    password = str(message.get("password") or "")
    user = accounts.verify_login(username, password)
    if user is None:
        await _send(websocket, {
            "type": "error", "code": "auth_failed",
            "operation": "auth_login", "request_id": _request_id(message),
            "message": "用户名或口令不正确",
        })
        return
    if _account_switch_busy(session):
        await _send(websocket, {
            "type": "error", "code": "account_switch_busy",
            "operation": "auth_login", "request_id": _request_id(message),
            "message": "Agent 任务仍在运行，结束后才能切换账号。",
        })
        return
    # Issue a session token for this login.
    token = accounts.create_session_token(user["user_id"])
    await _change_account(
        websocket, session, user, packet_type="auth_login_ok",
        operation="auth_login", request_id=_request_id(message), token=token,
    )


async def handle_auth_switch_user(websocket, session, message: dict[str, Any]) -> None:
    """Switch the session owner without credentials (used for passwordless users)."""
    user_id = str(message.get("user_id") or "").strip()
    user = accounts.get_user(user_id)
    if user is None:
        await _send(websocket, {
            "type": "error", "code": "auth_switch_failed",
            "operation": "auth_switch_user", "request_id": _request_id(message),
            "message": "用户不存在",
        })
        return
    if str(user.get("password_hash") or ""):
        await _send(websocket, {
            "type": "error", "code": "auth_required",
            "operation": "auth_switch_user", "request_id": _request_id(message),
            "message": "该账号已设置口令，请登录后切换。",
        })
        return
    await _change_account(
        websocket, session, user, packet_type="auth_switch_ok",
        operation="auth_switch_user", request_id=_request_id(message),
    )


async def handle_auth_logout(websocket, session, message: dict[str, Any]) -> None:
    """Log out to the local default user."""
    default = accounts.ensure_default_user()
    if (
        str(getattr(session, "owner_id", "") or "") != str(default["user_id"])
        and str(default.get("password_hash") or "")
    ):
        await _send(websocket, {
            "type": "error", "code": "auth_required",
            "operation": "auth_logout", "request_id": _request_id(message),
            "message": "本地默认账号已设置口令，请登录后切换。",
        })
        return
    token = str(message.get("token") or "")
    if token:
        accounts.revoke_session_token(token)
    await _change_account(
        websocket, session, default, packet_type="auth_logout_ok",
        operation="auth_logout", request_id=_request_id(message),
    )


async def handle_auth_set_password(websocket, session, message: dict[str, Any]) -> None:
    """Set or clear a password lock on the current user."""
    user_id = str(getattr(session, "owner_id", "") or "")
    new_password = str(message.get("password") or "")
    if not user_id:
        await _send(websocket, {
            "type": "error", "code": "auth_set_password_failed",
            "operation": "auth_set_password", "request_id": _request_id(message),
            "message": "no active user",
        })
        return
    ok = accounts.set_password(user_id, new_password)
    if not ok:
        await _send(websocket, {
            "type": "error", "code": "auth_set_password_failed",
            "operation": "auth_set_password", "request_id": _request_id(message),
            "message": "user not found",
        })
        return
    user = accounts.get_user(user_id)
    await _send(websocket, {
        "type": "auth_set_password_ok", "operation": "auth_set_password",
        "request_id": _request_id(message),
        "user": {
            "user_id": str(user["user_id"]) if user else user_id,
            "username": str(user["username"]) if user else "",
            "is_local_default": bool(user.get("is_local_default")) if user else False,
            "has_password": bool(str(user.get("password_hash") or "")) if user else False,
        },
    })


async def handle_usage_summary(websocket, session, message: dict[str, Any]) -> None:
    """Return the account center payload (balance + daily/model usage)."""
    from modus.desktop import accounts, billing

    user_id = str(getattr(session, "owner_id", "") or "")
    if not user_id:
        user_id = str(accounts.ensure_default_user()["user_id"])
    try:
        summary = billing.usage_summary(user_id)
    except Exception as exc:
        await _send(websocket, {
            "type": "error", "code": "usage_summary_failed",
            "operation": "usage_summary", "request_id": _request_id(message),
            "message": redact_text(str(exc)),
        })
        return
    await _send(websocket, {
        "type": "usage_summary", "operation": "usage_summary",
        "request_id": _request_id(message), "summary": summary,
    })


async def handle_recharge(websocket, session, message: dict[str, Any]) -> None:
    """Add local balance (充值). amount_cents is an integer."""
    from modus.desktop import accounts, billing

    user_id = str(getattr(session, "owner_id", "") or "")
    if not user_id:
        user_id = str(accounts.ensure_default_user()["user_id"])
    try:
        amount = int(message.get("amount_cents") or 0)
        note = str(message.get("note") or "")[:200]
    except (TypeError, ValueError):
        await _send(websocket, {
            "type": "error", "code": "recharge_failed",
            "operation": "recharge", "request_id": _request_id(message),
            "message": "充值金额无效",
        })
        return
    if amount <= 0:
        await _send(websocket, {
            "type": "error", "code": "recharge_failed",
            "operation": "recharge", "request_id": _request_id(message),
            "message": "充值金额必须大于 0",
        })
        return
    balance = billing.recharge(user_id, amount, note)
    await _send(websocket, {
        "type": "recharge_done", "operation": "recharge",
        "request_id": _request_id(message),
        "amount_cents": amount, "balance_cents": balance["balance_cents"],
    })


async def handle_provider_usage(websocket, session, message: dict[str, Any]) -> None:
    """Query each repository model's provider usage/balance (adapter facade)."""
    from modus.desktop import provider_usage
    from modus.desktop.server import model_repository

    request_id = _request_id(message)
    models = model_repository.list_public()
    results = []
    for model in models:
        model_id = str(getattr(model, "id", "") or "")
        # Resolve the runtime credential without exposing it.
        runtime = model_repository.runtime_model(model_id)
        if not runtime:
            continue
        query_model = {
            "provider": str(getattr(model, "provider", "") or ""),
            "api_key": str(runtime.get("api_key") or ""),
            "base_url": str(getattr(model, "base_url", "") or "") or None,
        }
        result = await provider_usage.query_provider_usage(query_model)
        results.append({
            "model_id": model_id,
            "name": str(getattr(model, "name", "") or getattr(model, "model", "") or ""),
            "provider": result.provider,
            "status": result.status,
            "balance": result.balance,
            "usage": result.usage,
            "cost": result.cost,
            "rate_limits": result.rate_limits,
            "capabilities": result.capabilities,
            "message": result.message,
            "queried_at": result.queried_at,
        })
    await _send(websocket, {
        "type": "provider_usage", "operation": "provider_usage",
        "request_id": request_id, "models": results,
    })


async def handle_auth_rename_user(websocket, session, message: dict[str, Any]) -> None:
    """Rename the current (or specified) local user."""
    user_id = str(message.get("user_id") or getattr(session, "owner_id", "") or "")
    new_username = str(message.get("new_username") or "").strip()
    try:
        user = accounts.rename_user(user_id, new_username)
    except ValueError as exc:
        await _send(websocket, {
            "type": "error", "code": "user_rename_failed",
            "operation": "auth_rename_user", "request_id": _request_id(message),
            "message": str(exc),
        })
        return
    await _send(websocket, {
        "type": "user_renamed", "operation": "auth_rename_user",
        "request_id": _request_id(message), "user": user,
    })


async def handle_auth_delete_user(websocket, session, message: dict[str, Any]) -> None:
    """Delete a user; cascade-remove owned data when requested."""
    user_id = str(message.get("user_id") or getattr(session, "owner_id", "") or "")
    delete_data = bool(message.get("delete_data"))
    deleting_current = bool(
        user_id and str(getattr(session, "owner_id", "")) == user_id
    )
    if deleting_current and _account_switch_busy(session):
        await _send(websocket, {
            "type": "error", "code": "account_switch_busy",
            "operation": "auth_delete_user", "request_id": _request_id(message),
            "message": "Agent 任务仍在运行，结束后才能删除当前账号。",
        })
        return
    try:
        accounts.delete_user(user_id, delete_data=delete_data)
    except ValueError as exc:
        await _send(websocket, {
            "type": "error", "code": "user_delete_failed",
            "operation": "auth_delete_user", "request_id": _request_id(message),
            "message": str(exc),
        })
        return
    if delete_data:
        from modus.paths import data_dir

        resources = Path(data_dir()) / "users" / user_id
        if resources.is_dir() and resources.parent == Path(data_dir()) / "users":
            import shutil

            shutil.rmtree(resources)
    if deleting_current:
        default = accounts.ensure_default_user()
        await _change_account(
            websocket, session, default, packet_type="user_deleted",
            operation="auth_delete_user", request_id=_request_id(message),
            extra={"deleted_user_id": user_id},
        )
        return
    await _send(websocket, {
        "type": "user_deleted", "operation": "auth_delete_user",
        "request_id": _request_id(message), "deleted_user_id": user_id,
    })


async def handle_auth_demo_account(websocket, session, message: dict[str, Any]) -> None:
    """Ensure the demo account exists and return its pre-filled credentials."""
    from modus.desktop.accounts import _DEMO_PASSWORD, ensure_demo_user

    user = ensure_demo_user()
    await _send(websocket, {
        "type": "demo_account", "operation": "auth_demo_account",
        "request_id": _request_id(message),
        "user": {**user, "password": _DEMO_PASSWORD},
    })


async def handle_modus_account_status(websocket, session, message: dict[str, Any]) -> None:
    """Report the Modus cloud account status (degraded when unconfigured)."""
    from modus.desktop import cloud_accounts

    cloud = await cloud_accounts.account_status()
    await _send(websocket, {
        "type": "modus_account_status", "operation": "modus_account_status",
        "request_id": _request_id(message), "cloud": cloud,
    })


def register_auth_commands(router) -> None:
    router.register("auth_status", handle_auth_status)
    router.register("auth_login", handle_auth_login)
    router.register("auth_logout", handle_auth_logout)
    router.register("auth_set_password", handle_auth_set_password)
    router.register("auth_switch_user", handle_auth_switch_user)
    router.register("user_create", handle_user_create)
    router.register("usage_summary", handle_usage_summary)
    router.register("recharge", handle_recharge)
    router.register("provider_usage", handle_provider_usage)
    router.register("auth_rename_user", handle_auth_rename_user)
    router.register("auth_delete_user", handle_auth_delete_user)
    router.register("auth_demo_account", handle_auth_demo_account)
    router.register("modus_account_status", handle_modus_account_status)
