from modus.desktop.peri import _subagent_system_prompt


def test_subagent_prompt_declares_workspace_and_requires_relative_evidence_paths():
    prompt = _subagent_system_prompt(
        {"description": "inspect", "context": "repository", "success_criteria": "evidence"},
        "request",
        "/Users/yinsijie/CodeRepo/Modus",
    )

    assert "WORKING DIRECTORY: /Users/yinsijie/CodeRepo/Modus" in prompt
    assert "Use relative paths only" in prompt
    assert "must obtain the relevant evidence with your available tools before concluding" in prompt


def test_revision_prompt_preserves_prior_output_and_tool_evidence():
    from modus.desktop.peri import build_revision_request

    request = build_revision_request(
        "Use the actual README.",
        "Initial conclusion",
        [{"name": "read_file", "result": "# Modus", "is_error": False}],
    )

    assert "Use the actual README." in request
    assert "Initial conclusion" in request
    assert "read_file: # Modus" in request
    assert "Do not discard verified evidence" in request


def test_decompose_prompt_formats_with_model_count_without_crashing():
    """The decompose prompt embeds JSON examples; its braces must be escaped
    so ``.format(model_count=...)`` does not raise KeyError."""
    from modus.desktop.peri import HOST_DECOMPOSE_PROMPT

    prompt = HOST_DECOMPOSE_PROMPT.format(model_count=3)

    assert "Split into exactly 3 sub-tasks" in prompt
    # The JSON example must survive formatting as literal single braces.
    assert '\n{\n  "subtasks": [' in prompt
    assert '"success_criteria": {' in prompt
    # No stray double-brace escapes should leak into the final prompt.
    assert "{{" not in prompt
