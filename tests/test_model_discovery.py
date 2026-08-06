from __future__ import annotations

import httpx
import pytest

from modus.desktop.model_discovery import discover_models, validate_discovery_url


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_openai_discovery_uses_server_key_and_keeps_unknown_capabilities_unknown():
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [
            {"id": "gpt-new", "owned_by": "provider"},
            {"id": "gpt-current", "context_window": 200_000, "supports_tools": True},
        ]})

    source = {
        "id": "repository-record", "provider": "openai", "model": "gpt-current",
        "api_key": "server-secret-key", "context_window": 128_000,
        "max_output_tokens": 8_192, "supports_tools": False,
        "supports_images": False, "reasoning_efforts": ["low", "high"],
        "capability_sources": {
            "context_window": "user_configuration",
            "max_output_tokens": "user_configuration",
            "supports_tools": "user_configuration",
            "supports_images": "user_configuration",
            "reasoning_efforts": "user_configuration",
        },
    }
    client = _client(handler)
    try:
        result = await discover_models(source, client=client)
    finally:
        await client.aclose()

    assert observed == {
        "url": "https://api.openai.com/v1/models",
        "authorization": "Bearer server-secret-key",
    }
    assert "server-secret-key" not in str(result)
    assert [item["id"] for item in result["models"]] == ["gpt-current", "gpt-new"]
    current = result["models"][0]
    assert current["capabilities"]["context_window"] == 128_000
    assert current["capabilities"]["supports_tools"] is False
    assert set(current["capability_sources"].values()) == {"user_configuration"}
    unknown = result["models"][1]
    assert unknown["capabilities"]["context_window"] is None
    assert unknown["capabilities"]["supports_tools"] is None
    assert unknown["capability_sources"]["context_window"] == "unknown"


@pytest.mark.asyncio
async def test_google_and_anthropic_adapters_use_provider_headers():
    google_headers = {}

    def google_handler(request: httpx.Request) -> httpx.Response:
        google_headers.update(request.headers)
        return httpx.Response(200, json={"models": [{
            "name": "models/gemini-test", "displayName": "Gemini Test",
            "inputTokenLimit": 1_000_000, "outputTokenLimit": 16_000,
        }]})

    google_client = _client(google_handler)
    try:
        google = await discover_models({
            "id": "google-source", "provider": "google", "model": "other",
            "api_key": "google-secret",
        }, client=google_client)
    finally:
        await google_client.aclose()
    assert google_headers["x-goog-api-key"] == "google-secret"
    assert google["models"][0]["id"] == "gemini-test"
    assert google["models"][0]["capabilities"]["context_window"] == 1_000_000
    assert google["models"][0]["capability_sources"]["context_window"] == "provider_api"

    anthropic_headers = {}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        anthropic_headers.update(request.headers)
        return httpx.Response(200, json={"data": [{"id": "claude-test", "display_name": "Claude Test"}]})

    anthropic_client = _client(anthropic_handler)
    try:
        anthropic = await discover_models({
            "id": "anthropic-source", "provider": "anthropic", "model": "other",
            "api_key": "anthropic-secret",
        }, client=anthropic_client)
    finally:
        await anthropic_client.aclose()
    assert anthropic_headers["x-api-key"] == "anthropic-secret"
    assert anthropic_headers["anthropic-version"] == "2023-06-01"
    assert anthropic["models"][0]["id"] == "claude-test"
    assert "anthropic-secret" not in str(anthropic)


@pytest.mark.asyncio
@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.8", "172.16.2.4", "192.168.1.3", "169.254.1.2", "::1",
])
async def test_custom_remote_endpoint_rejects_non_public_addresses(address):
    async def resolver(_host: str, _port: int) -> list[str]:
        return [address]

    with pytest.raises(ValueError, match="不允许访问"):
        await validate_discovery_url(
            "https://models.example.test/v1/models", "custom", resolver=resolver,
        )


@pytest.mark.asyncio
async def test_remote_requires_https_and_ollama_requires_loopback_http():
    async def public(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    async def loopback(_host: str, _port: int) -> list[str]:
        return ["127.0.0.1"]

    with pytest.raises(ValueError, match="HTTPS"):
        await validate_discovery_url(
            "http://models.example.test/v1/models", "custom", resolver=public,
        )
    assert await validate_discovery_url(
        "http://localhost:11434/v1/models", "ollama", resolver=loopback,
    ) == "http://localhost:11434/v1/models"
    with pytest.raises(ValueError, match="loopback"):
        await validate_discovery_url(
            "http://ollama.example.test/v1/models", "ollama", resolver=public,
        )


@pytest.mark.asyncio
async def test_http_errors_are_sanitized_without_endpoint_or_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "secret provider detail"})

    client = _client(handler)
    try:
        with pytest.raises(ValueError) as caught:
            await discover_models({
                "id": "source", "provider": "openai", "model": "gpt",
                "api_key": "must-not-leak",
            }, client=client)
    finally:
        await client.aclose()
    assert str(caught.value) == "模型发现失败：厂商返回 HTTP 401"
    assert "must-not-leak" not in str(caught.value)
