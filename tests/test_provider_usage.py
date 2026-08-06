"""Provider usage adapters: DeepSeek balance, OpenAI/Anthropic graceful failures."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from modus.desktop import provider_usage as pu


def _run(coro):
    return asyncio.run(coro)


def _resp(status, data):
    return type("R", (), {"status_code": status, "json": lambda self: data})()


def test_deepseek_balance_parses_cards():
    payload = {
        "is_available": True,
        "balance_infos": [
            {"currency": "CNY", "total_balance": "110.00",
             "granted_balance": "10.00", "topped_up_balance": "100.00"},
        ],
    }
    with patch.object(pu.httpx.AsyncClient, "get", new=AsyncMock(return_value=_resp(200, payload))) as mock:
        result = _run(pu.query_deepseek({"provider": "deepseek", "api_key": "sk-x"}))
    assert result.status == "queried"
    assert result.capabilities == [pu.CAP_BALANCE]
    assert result.balance["is_available"] is True
    assert result.balance["cards"][0]["currency"] == "CNY"
    assert result.balance["cards"][0]["total_balance"] == "110.00"
    # URL is the DeepSeek balance endpoint
    assert "user/balance" in mock.call_args.args[0]


def test_deepseek_no_key_is_unavailable():
    result = _run(pu.query_deepseek({"provider": "deepseek", "api_key": ""}))
    assert result.status == "unavailable"


def test_deepseek_error_is_graceful():
    with patch.object(pu.httpx.AsyncClient, "get", new=AsyncMock(side_effect=Exception("net down"))):
        result = _run(pu.query_deepseek({"provider": "deepseek", "api_key": "sk-x"}))
    assert result.status == "error"
    assert "net down" in result.message


def test_openai_without_admin_key_is_unavailable():
    with patch.object(pu.httpx.AsyncClient, "get", new=AsyncMock(return_value=_resp(403, {}))):
        result = _run(pu.query_openai({"provider": "openai", "api_key": "sk-project"}))
    assert result.status == "unavailable"
    assert "Admin" in result.message


def test_anthropic_without_admin_key_is_unavailable():
    with patch.object(pu.httpx.AsyncClient, "get", new=AsyncMock(return_value=_resp(401, {}))):
        result = _run(pu.query_anthropic({"provider": "anthropic", "api_key": "sk-ant-plain"}))
    assert result.status == "unavailable"


def test_unknown_provider_is_supported_but_empty():
    result = _run(pu.query_provider_usage({"provider": "groq", "api_key": "x"}))
    assert result.status == "supported"
    assert result.capabilities == []


def test_facade_never_raises():
    # A garbage adapter call must never escape.
    with patch.object(pu, "query_deepseek", side_effect=RuntimeError("boom")):
        result = _run(pu.query_provider_usage({"provider": "deepseek", "api_key": "k"}))
    assert result.status == "error"
