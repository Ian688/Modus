"""Provider-backed model discovery with explicit capability provenance."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


MAX_DISCOVERY_BYTES = 2 * 1024 * 1024
CAPABILITY_FIELDS = (
    "context_window", "max_output_tokens", "supports_tools",
    "supports_images", "reasoning_efforts",
)

# This catalog is intentionally exact-match and sparse. Unknown data must stay
# unknown instead of being inferred from a model-name prefix. Entries can be
# expanded from provider documentation without changing the discovery protocol.
CAPABILITY_CATALOG_VERSION = "2026-07-31"
CAPABILITY_CATALOG: dict[tuple[str, str], dict[str, Any]] = {}

DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/models",
    "deepseek": "https://api.deepseek.com/v1/models",
    "groq": "https://api.groq.com/openai/v1/models",
    "xai": "https://api.x.ai/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "ollama": "http://127.0.0.1:11434/v1/models",
}

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def _resolve_addresses(host: str, port: int) -> list[str]:
    def resolve() -> list[str]:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(str(row[4][0]) for row in rows))

    return await asyncio.to_thread(resolve)


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


async def validate_discovery_url(
    url: str, provider: str, *, resolver: Resolver = _resolve_addresses,
) -> str:
    """Reject local/private custom endpoints; allow loopback only for Ollama."""
    parsed = urlsplit(url)
    provider = provider.lower()
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("模型发现 Endpoint 格式无效")
    if parsed.query or parsed.fragment:
        raise ValueError("模型发现 Endpoint 不允许 query 或 fragment")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("模型发现 Endpoint 仅支持 http/https")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await resolver(parsed.hostname, port)
    if not addresses:
        raise ValueError("模型发现 Endpoint 无法解析")
    if provider == "ollama":
        if parsed.scheme != "http" or not all(_is_loopback(item) for item in addresses):
            raise ValueError("Ollama 模型发现只允许本机 loopback HTTP Endpoint")
    else:
        if parsed.scheme != "https":
            raise ValueError("远程模型发现 Endpoint 必须使用 HTTPS")
        if not all(_is_public(item) for item in addresses):
            raise ValueError("模型发现 Endpoint 不允许访问本机、内网或保留地址")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _endpoint_for(model: dict[str, Any]) -> tuple[str, bool]:
    provider = str(model.get("provider") or "custom").lower()
    configured = str(model.get("base_url") or "").strip()
    if configured:
        base = configured.rstrip("/")
        return (base if base.endswith("/models") else base + "/models"), False
    endpoint = DEFAULT_ENDPOINTS.get(provider)
    if not endpoint:
        raise ValueError("自定义提供商必须先配置 Endpoint")
    return endpoint, True


def _headers(model: dict[str, Any]) -> dict[str, str]:
    provider = str(model.get("provider") or "custom").lower()
    key = str(model.get("api_key") or "")
    if provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if provider == "google":
        return {"x-goog-api-key": key}
    return {"Authorization": f"Bearer {key}"} if key else {}


def _positive_int(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _first(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if raw.get(key) is not None:
            return raw[key]
    return None


def _provider_capabilities(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    capabilities: dict[str, Any] = {
        "context_window": _positive_int(_first(
            raw, "context_window", "context_length", "input_token_limit", "inputTokenLimit",
        )),
        "max_output_tokens": _positive_int(_first(
            raw, "max_output_tokens", "output_token_limit", "outputTokenLimit",
        )),
        "supports_tools": None,
        "supports_images": None,
        "reasoning_efforts": [],
    }
    sources: dict[str, str] = {field: "unknown" for field in CAPABILITY_FIELDS}
    for field in ("context_window", "max_output_tokens"):
        if capabilities[field] is not None:
            sources[field] = "provider_api"

    input_modalities = raw.get("input_modalities") or raw.get("inputModalities")
    if isinstance(input_modalities, list):
        capabilities["supports_images"] = any(str(item).lower() in {"image", "vision"} for item in input_modalities)
        sources["supports_images"] = "provider_api"
    tool_flag = _first(raw, "supports_tools", "supportsTools")
    if isinstance(tool_flag, bool):
        capabilities["supports_tools"] = tool_flag
        sources["supports_tools"] = "provider_api"
    efforts = _first(raw, "reasoning_efforts", "reasoningEfforts")
    if isinstance(efforts, list):
        capabilities["reasoning_efforts"] = list(dict.fromkeys(str(item) for item in efforts if str(item)))
        sources["reasoning_efforts"] = "provider_api"
    return capabilities, sources


def _model_id(raw: dict[str, Any]) -> str:
    value = str(raw.get("id") or raw.get("name") or "").strip()
    return value.removeprefix("models/")


def _display_name(raw: dict[str, Any], model_id: str) -> str:
    return str(raw.get("display_name") or raw.get("displayName") or model_id).strip()


def _normalize_model(
    raw: dict[str, Any], source_model: dict[str, Any],
) -> dict[str, Any] | None:
    model_id = _model_id(raw)
    if not model_id:
        return None
    provider = str(source_model.get("provider") or "custom").lower()
    capabilities, sources = _provider_capabilities(raw)
    catalog = CAPABILITY_CATALOG.get((provider, model_id), {})
    for field in CAPABILITY_FIELDS:
        if sources[field] == "unknown" and field in catalog:
            capabilities[field] = catalog[field]
            sources[field] = "modus_catalog"

    # The configured record is the user's explicit correction layer and wins
    # over provider metadata or the embedded catalog for that exact model.
    if model_id == str(source_model.get("model") or ""):
        configured_sources = source_model.get("capability_sources") or {}
        for field in CAPABILITY_FIELDS:
            configured_source = str(configured_sources.get(field) or "unknown")
            if field in source_model and configured_source != "unknown":
                capabilities[field] = source_model[field]
                sources[field] = configured_source

    return {
        "id": model_id,
        "name": _display_name(raw, model_id),
        "provider": provider,
        "availability_source": "provider_api",
        "capabilities": capabilities,
        "capability_sources": sources,
    }


def _payload_rows(provider: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    key = "models" if provider == "google" else "data"
    rows = payload.get(key, [])
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []


async def discover_models(
    source_model: dict[str, Any], *, client: httpx.AsyncClient | None = None,
    resolver: Resolver = _resolve_addresses,
) -> dict[str, Any]:
    """List models using one repository credential without exposing its key."""
    provider = str(source_model.get("provider") or "custom").lower()
    if provider != "ollama" and not str(source_model.get("api_key") or ""):
        raise ValueError("该模型仓库记录尚未配置 API Key")
    endpoint, trusted_default = _endpoint_for(source_model)
    if not trusted_default:
        endpoint = await validate_discovery_url(endpoint, provider, resolver=resolver)

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False,
        )
    try:
        response = await client.get(endpoint, headers=_headers(source_model))
        response.raise_for_status()
        if len(response.content) > MAX_DISCOVERY_BYTES:
            raise ValueError("模型发现响应过大")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("模型发现响应格式无效")
        normalized = [
            item for raw in _payload_rows(provider, payload)
            if (item := _normalize_model(raw, source_model)) is not None
        ]
        deduped = {item["id"]: item for item in normalized}
        return {
            "source_model_id": str(source_model.get("id") or ""),
            "provider": provider,
            "models": sorted(deduped.values(), key=lambda item: item["id"].lower()),
            "catalog_version": CAPABILITY_CATALOG_VERSION,
            "warning": (
                "厂商模型列表通常不包含上下文窗口、工具能力或思考深度；"
                "未知字段保持为空，需由 Modus 能力目录或用户明确校正。"
            ),
        }
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"模型发现失败：厂商返回 HTTP {exc.response.status_code}") from exc
    except ValueError:
        raise
    except httpx.HTTPError as exc:
        raise ValueError("模型发现网络请求失败") from exc
    except Exception as exc:
        raise ValueError("模型发现响应无法解析") from exc
    finally:
        if owns_client:
            await client.aclose()
