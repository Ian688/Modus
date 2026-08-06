import pytest


def test_review_prompt_includes_bounded_worker_evidence_summary():
    from modus.desktop import peri

    prompt = peri._review_packet(
        {"name": "Inspect README"},
        "The README says Modus is an agent.",
        [
            {"name": "read_file", "result": "# Modus\nAgent desktop", "is_error": False},
            {"name": "grep", "result": "README.md:1: Modus", "is_error": False},
        ],
    )

    assert "Inspect README" in prompt
    assert "The README says Modus is an agent." in prompt
    assert "Evidence tools used (2)" in prompt
    assert "read_file: # Modus" in prompt
    assert "grep: README.md:1: Modus" in prompt


@pytest.mark.asyncio
async def test_peri_review_receives_tool_evidence_for_each_worker(monkeypatch):
    from modus.desktop import server

    class FakeWebSocket:
        def __init__(self): self.sent = []
        async def send_json(self, packet): self.sent.append(packet)

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "host", "model_id": "host", "name": "Host", "provider": "test", "model": "host", "api_key": "key"},
            "worker_1": {"id": "sub", "model_id": "sub", "name": "Inspector", "provider": "test", "model": "sub", "api_key": "key"},
        },
    })

    async def decompose(*_args, **_kwargs):
        return [{"name": "Inspect", "description": "inspect", "context": "repo", "success_criteria": "evidence"}]

    async def execute(*_args, **kwargs):
        await kwargs["event_callback"]({"type": "subagent_tool_result", "name": "read_file", "result": "README evidence", "is_error": False})
        return "Worker conclusion"

    observed = {}
    async def review(tasks, outputs, provider, model, message, **kwargs):
        observed["outputs"] = outputs
        return True, [], [8.0]

    async def merge(*_args, **_kwargs): return "Final"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    await server._run_peri_stream(FakeWebSocket(), server.DaoSession(id="s", db_id="db"), "Inspect")

    assert "Worker conclusion" in observed["outputs"][0]
    assert "Evidence tools used (1)" in observed["outputs"][0]
    assert "read_file: README evidence" in observed["outputs"][0]
