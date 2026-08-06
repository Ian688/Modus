"""Capability negotiation seam: model record + live-client probing.

Verifies ``ModelCapabilities`` flag resolution, the ``advertised()`` surface
(a future AGI advertises capabilities through the same list), and that
``resolve_capabilities`` merges a model record's declared metadata with
duck-typed client probes like an ``embed`` method.
"""

from __future__ import annotations

from modus.agent.capabilities import Capability, ModelCapabilities, resolve_capabilities


def test_flags_reflect_constructor_kwargs():
    caps = ModelCapabilities(
        supports_tools=True,
        supports_images=True,
        reasoning_effort="high",
        supports_embeddings=False,
        supports_structured_output=True,
    )
    assert caps.can(Capability.TOOLS)
    assert caps.can(Capability.IMAGES)
    assert caps.can(Capability.REASONING)
    assert not caps.can(Capability.EMBEDDINGS)
    assert caps.can(Capability.STRUCTURED_OUTPUT)


def test_reasoning_requires_effort_level():
    # A reasoning_effort of None must not advertise REASONING.
    caps = ModelCapabilities()
    assert not caps.can(Capability.REASONING)
    assert Capability.REASONING not in caps.advertised()


def test_advertised_lists_only_supported():
    caps = ModelCapabilities(supports_tools=True, reasoning_effort="medium")
    advertised = caps.advertised()
    assert advertised == [Capability.TOOLS.value, Capability.REASONING.value]


def test_resolve_from_model_record():
    record = {
        "supports_tools": True,
        "supports_images": True,
        "reasoning_effort": "low",
        "supports_structured_output": True,
    }
    caps = resolve_capabilities(model_record=record)
    assert caps.can(Capability.TOOLS)
    assert caps.can(Capability.IMAGES)
    assert caps.can(Capability.REASONING)
    assert caps.can(Capability.STRUCTURED_OUTPUT)
    assert not caps.can(Capability.EMBEDDINGS)


def test_resolve_probes_client_for_embeddings():
    class EmbedClient:
        async def embed(self, texts):
            return [[0.1, 0.2]] * len(texts)

    caps = resolve_capabilities(client=EmbedClient())
    assert caps.can(Capability.EMBEDDINGS)


def test_resolve_prefers_record_over_llm_config():
    class Client:
        pass

    record = {"reasoning_effort": "high", "supports_tools": True}
    llm_config = type("Cfg", (), {"reasoning_effort": "low"})
    caps = resolve_capabilities(llm_config=llm_config, client=Client(), model_record=record)
    assert caps.can(Capability.REASONING)
    assert caps.can(Capability.TOOLS)
