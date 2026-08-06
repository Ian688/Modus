import pytest


@pytest.mark.asyncio
async def test_moa_reference_prefers_its_repository_credential(monkeypatch):
    from modus.agent import moa
    from modus.types import Message

    captured = {}

    class FakeClient:
        async def chat(self, messages, tools, system_prompt):
            yield {"type": "text_delta", "text": "advice"}

    def fake_client(cfg):
        captured["key"] = cfg.api_key
        captured["base_url"] = cfg.base_url
        return FakeClient()

    monkeypatch.setattr(moa, "create_llm_client", fake_client)
    text = await moa.call_reference(
        {"provider": "test", "model": "ref", "api_key": "repo-key", "base_url": "https://ref.example"},
        [Message(role="user", content="hello")], "system",
    )
    assert text == "advice"
    assert captured == {"key": "repo-key", "base_url": "https://ref.example"}


@pytest.mark.asyncio
async def test_peri_subagent_prefers_its_repository_credential(monkeypatch):
    from modus.desktop import peri

    captured = {}

    class FakeClient:
        async def chat(self, messages, tools, system_prompt):
            yield {"type": "text_delta", "text": "worker output"}

    def fake_client(cfg):
        captured["key"] = cfg.api_key
        captured["base_url"] = cfg.base_url
        return FakeClient()

    monkeypatch.setattr(peri, "create_llm_client", fake_client)
    output = await peri.execute_subtask(
        {"description": "research", "context": "ctx", "success_criteria": "good"},
        {"provider": "test", "model": "sub", "api_key": "sub-key", "base_url": "https://sub.example"},
        "original request",
    )
    assert output == "worker output"
    assert captured == {"key": "sub-key", "base_url": "https://sub.example"}


def test_explicit_model_credential_paths_are_present():
    from pathlib import Path

    root = Path(__file__).parents[1] / "src/modus"
    moa = (root / "agent/moa.py").read_text()
    peri = (root / "desktop/peri.py").read_text()
    assert 'api_key = str(ref_config.get("api_key") or "")' in moa
    assert 'api_key=str(ref_config.get("api_key") or "")' in peri
    assert 'base_url=ref_config.get("base_url") or None' in peri
    assert 'api_key=str(primary.get("api_key") or "")' in (root / "desktop/peri_runner.py").read_text()
