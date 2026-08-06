"""Modus 云账户门面：未配置时降级，配置后走真实 HTTP（接口预留）。

Modus 云端服务目前不存在。该模块提供稳定的注册/登录/状态接口，未配置
``MODUS_CLOUD_API`` 时全部返回 ``unconfigured`` 降级；未来接入真实后端时
只需实现 TODO 分支，前端契约与调用方无需改动。
"""
from __future__ import annotations

import os
from typing import Any

import httpx

_CLOUD_API_ENV = "MODUS_CLOUD_API"


def _api_base() -> str:
    return os.environ.get(_CLOUD_API_ENV, "").rstrip("/")


def is_configured() -> bool:
    return bool(_api_base())


def _unconfigured() -> dict[str, Any]:
    return {
        "status": "unconfigured",
        "configured": False,
        "message": "Modus 云账户尚未配置（设置 MODUS_CLOUD_API 环境变量后可用邮箱注册/登录）",
    }


async def email_register(
    email: str, password: str, display_name: str = "",
) -> dict[str, Any]:
    """邮箱注册。未配置时降级返回。"""
    if not is_configured():
        return _unconfigured()
    try:
        # TODO(cloud): POST {base}/auth/register {email,password,display_name}
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            response = await client.post(
                f"{_api_base()}/auth/register",
                json={"email": email, "password": password, "display_name": display_name},
            )
        if response.status_code == 200:
            data = response.json()
            return {"status": "ok", "configured": True, **data}
        return {
            "status": "error", "configured": True,
            "message": f"HTTP {response.status_code}",
        }
    except Exception as exc:
        return {"status": "error", "configured": True, "message": str(exc)[:200]}


async def email_login(email: str, password: str) -> dict[str, Any]:
    """邮箱登录。未配置时降级返回。"""
    if not is_configured():
        return _unconfigured()
    try:
        # TODO(cloud): POST {base}/auth/login -> {token,user}
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            response = await client.post(
                f"{_api_base()}/auth/login",
                json={"email": email, "password": password},
            )
        if response.status_code == 200:
            data = response.json()
            return {"status": "ok", "configured": True, **data}
        return {
            "status": "error", "configured": True,
            "message": f"HTTP {response.status_code}",
        }
    except Exception as exc:
        return {"status": "error", "configured": True, "message": str(exc)[:200]}


async def account_status() -> dict[str, Any]:
    """当前云账户状态。未配置时降级返回。"""
    if not is_configured():
        return _unconfigured()
    try:
        # TODO(cloud): GET {base}/account
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            response = await client.get(f"{_api_base()}/account")
        if response.status_code == 200:
            data = response.json()
            return {"status": "ok", "configured": True, **data}
        return {
            "status": "error", "configured": True,
            "message": f"HTTP {response.status_code}",
        }
    except Exception as exc:
        return {"status": "error", "configured": True, "message": str(exc)[:200]}
