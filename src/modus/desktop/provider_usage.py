"""Provider usage / balance queries behind a common adapter.

Each provider exposes different capabilities:
- DeepSeek: ``GET /user/balance`` with the normal API key → remaining balance.
- OpenAI: organization usage/cost endpoints require an *Organization Admin* key
  (a normal project key cannot read them).  There is no direct balance endpoint.
- Anthropic: usage / cost / rate-limits Admin API (``sk-ant-admin-*``), Team or
  Enterprise orgs only.

The design is deliberately graceful: a capability probe returns ``supported``
when the adapter recognises the provider, ``queried`` after a real call, and
``unavailable`` when the credential or org type cannot support it.  A failing
call never raises out of the facade.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# Default API origins per provider (overridable via the model's base_url).
_PROVIDER_ORIGINS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
}

# Capability flags a provider adapter may expose.
CAP_BALANCE = "balance"
CAP_USAGE = "usage"
CAP_COST = "cost"
CAP_RATE_LIMITS = "rate_limits"


@dataclass
class ProviderUsage:
    """One provider's query result (browser-safe)."""
    provider: str
    status: str  # "supported" | "queried" | "unavailable" | "error"
    balance: dict[str, Any] | None = None          # balance cards
    usage: dict[str, Any] | None = None            # token usage buckets
    cost: dict[str, Any] | None = None             # monetary cost
    rate_limits: dict[str, Any] | None = None      # rate limit config
    capabilities: list[str] = field(default_factory=list)
    message: str = ""
    queried_at: float = field(default_factory=time.time)


async def _get(
    url: str, *, api_key: str, headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> httpx.Response:
    merged = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if headers:
        merged.update(headers)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        return await client.get(url, headers=merged)


def _base_url_for(model: dict[str, Any], provider: str) -> str:
    base = str(model.get("base_url") or "").strip().rstrip("/")
    if base:
        return base
    return _PROVIDER_ORIGINS.get(provider, "https://api.deepseek.com")


# ── DeepSeek ──


async def query_deepseek(model: dict[str, Any]) -> ProviderUsage:
    api_key = str(model.get("api_key") or "")
    if not api_key:
        return ProviderUsage("deepseek", "unavailable", message="未配置 API Key")
    base = _base_url_for(model, "deepseek")
    try:
        resp = await _get(f"{base}/user/balance", api_key=api_key)
        data = resp.json()
    except Exception as exc:
        return ProviderUsage(
            "deepseek", "error", capabilities=[CAP_BALANCE],
            message=f"查询失败: {str(exc)[:120]}",
        )
    if resp.status_code != 200:
        return ProviderUsage(
            "deepseek", "error", capabilities=[CAP_BALANCE],
            message=f"HTTP {resp.status_code}: {str(data)[:120]}",
        )
    infos = data.get("balance_infos") or []
    cards = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        cards.append({
            "currency": str(info.get("currency") or "CNY"),
            "total_balance": str(info.get("total_balance") or "0"),
            "granted_balance": str(info.get("granted_balance") or "0"),
            "topped_up_balance": str(info.get("topped_up_balance") or "0"),
        })
    return ProviderUsage(
        "deepseek", "queried", balance={"is_available": bool(data.get("is_available")), "cards": cards},
        capabilities=[CAP_BALANCE], message="余额查询成功",
    )


# ── OpenAI (Organization Admin key) ──


async def query_openai(model: dict[str, Any]) -> ProviderUsage:
    api_key = str(model.get("api_key") or "")
    if not api_key:
        return ProviderUsage("openai", "unavailable", message="未配置 API Key")
    base = _base_url_for(model, "openai")
    now = int(time.time())
    results: dict[str, Any] = {}
    capabilities: list[str] = []
    try:
        # Usage: last 24h in 1-day buckets.
        usage_url = (
            f"{base}/v1/organization/usage/completions"
            f"?start_time={now - 86400}&limit=7"
        )
        usage_resp = await _get(usage_url, api_key=api_key)
        if usage_resp.status_code == 200:
            data = usage_resp.json()
            results["usage"] = {
                "total_tokens": data.get("data", [{}])[0].get("total_tokens") if isinstance(data.get("data"), list) and data.get("data") else 0,
                "start_time": now - 86400,
            }
            capabilities.append(CAP_USAGE)
    except Exception:
        pass
    try:
        cost_url = f"{base}/v1/organization/costs?start_time={now - 86400}&limit=7"
        cost_resp = await _get(cost_url, api_key=api_key)
        if cost_resp.status_code == 200:
            data = cost_resp.json()
            results["cost"] = {
                "total": data.get("total", {}),
                "daily": (data.get("data") or [])[:7],
            }
            capabilities.append(CAP_COST)
    except Exception:
        pass
    if not capabilities:
        # Admin key missing / unsupported org → the endpoints return 401/403.
        return ProviderUsage(
            "openai", "unavailable", capabilities=[], message="需要 Organization Admin API Key（普通 Key 无法读取组织用量）",
        )
    return ProviderUsage(
        "openai", "queried", usage=results.get("usage"), cost=results.get("cost"),
        capabilities=capabilities, message="用量查询成功",
    )


# ── Anthropic (Admin API, Team/Enterprise) ──


async def query_anthropic(model: dict[str, Any]) -> ProviderUsage:
    api_key = str(model.get("api_key") or "")
    if not api_key:
        return ProviderUsage("anthropic", "unavailable", message="未配置 API Key")
    base = _base_url_for(model, "anthropic")
    results: dict[str, Any] = {}
    capabilities: list[str] = []
    headers = {"anthropic-version": "2023-06-01"}
    now = int(time.time())
    try:
        usage_url = f"{base}/v1/organizations/usage_report/messages?granularity=1d&start_time={now - 7*86400}"
        resp = await _get(usage_url, api_key=api_key, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            results["usage"] = data
            capabilities.append(CAP_USAGE)
    except Exception:
        pass
    try:
        cost_url = f"{base}/v1/organizations/cost_report?granularity=1d&start_time={now - 7*86400}"
        resp = await _get(cost_url, api_key=api_key, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            results["cost"] = data
            capabilities.append(CAP_COST)
    except Exception:
        pass
    try:
        rate_url = f"{base}/v1/organizations/rate_limits"
        resp = await _get(rate_url, api_key=api_key, headers=headers)
        if resp.status_code == 200:
            results["rate_limits"] = resp.json()
            capabilities.append(CAP_RATE_LIMITS)
    except Exception:
        pass
    if not capabilities:
        return ProviderUsage(
            "anthropic", "unavailable", capabilities=[],
            message="需要 Organization Admin Key（sk-ant-admin-*，Team/Enterprise 计划）",
        )
    return ProviderUsage(
        "anthropic", "queried", usage=results.get("usage"), cost=results.get("cost"),
        rate_limits=results.get("rate_limits"), capabilities=capabilities,
        message="用量查询成功",
    )


# ── Facade ──


async def query_provider_usage(model: dict[str, Any]) -> ProviderUsage:
    """Query one repository model's provider usage. Never raises."""
    provider = str(model.get("provider") or "").lower()
    try:
        if provider == "deepseek":
            return await query_deepseek(model)
        if provider == "openai":
            return await query_openai(model)
        if provider == "anthropic":
            return await query_anthropic(model)
        return ProviderUsage(
            provider or "custom", "supported", capabilities=[],
            message="该提供商暂未提供官方用量查询接口",
        )
    except Exception as exc:
        return ProviderUsage(
            provider or "custom", "error", message=f"查询失败: {str(exc)[:120]}",
        )
