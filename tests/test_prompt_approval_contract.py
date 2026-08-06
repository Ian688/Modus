from modus.config import ModusConfig
from modus.prompt import PromptAssembler


def test_system_prompt_tells_model_to_call_side_effect_tools_without_textual_confirmation():
    prompt = PromptAssembler(
        config=ModusConfig(),
        cwd=".",
        tool_names=["read_file", "write_file", "bash"],
        model="test-model",
        provider="test-provider",
    ).build()

    assert "call it directly" in prompt.lower()
    assert "approval card" in prompt.lower()
    assert "do not ask for permission in your text response" in prompt.lower()
    assert "write_file" in prompt
    assert "bash" in prompt


def test_system_prompt_distinguishes_read_only_and_side_effect_tool_behavior():
    prompt = PromptAssembler(
        config=ModusConfig(),
        cwd=".",
        tool_names=["read_file", "grep", "write_file", "bash"],
        model="test-model",
        provider="test-provider",
    ).build()

    assert "local data source" in prompt.lower()
    assert "require a user approval card" in prompt.lower()
    assert "never recursively read an entire large workspace" in prompt.lower()
    assert "side-effect tools" in prompt.lower()
    assert "system handles approval" in prompt.lower()
