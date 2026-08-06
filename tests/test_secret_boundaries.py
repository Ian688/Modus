def test_redaction_reaches_strings_inside_nested_lists():
    from modus.redact import redact_dict

    payload = {
        "items": [
            "token sk-1234567890abcdef",
            ["https://example.test/path?access_token=short-secret&ok=1"],
            {"notes": ("github_pat_1234567890abcdef",)},
        ],
    }

    redacted = redact_dict(payload)

    assert redacted["items"][0] == "token sk-123***cdef"
    assert redacted["items"][1][0] == "https://example.test/path?access_token=***&ok=1"
    assert redacted["items"][2]["notes"] == ("github***cdef",)


def test_redaction_masks_plaintext_secret_assignments_in_generated_text():
    from modus.redact import redact_text

    redacted = redact_text(
        "api_key=plain-secret password:another-secret authorization=Bearer-token ok=value"
    )

    assert redacted == "api_key=*** password:*** authorization=*** ok=value"


def test_manual_child_agent_table_is_not_part_of_canonical_schema(monkeypatch, tmp_path):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    with db._get_conn() as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "child_agents" not in tables


def test_run_snapshot_builder_only_copies_public_model_fields():
    from modus.desktop.run_config import build_run_config_snapshot

    snapshot = build_run_config_snapshot(
        mode="moa", host_model_id="host", reasoning_effort="high",
        roles={
            "host": {
                "model_id": "host", "temperature": 0.4,
                "api_key": "role-secret", "authorization": "Bearer role-secret",
            },
        },
        models=[{
            "id": "host", "name": "Host", "provider": "test", "model": "model-x",
            "api_key": "model-secret", "credential_hint": "...cret",
        }],
        budget={
            "max_turns": 8, "max_tokens": 9_000, "max_wall_seconds": 30,
            "max_verification_attempts": 2,
        },
        verification_required=True, has_custom_system_prompt=True,
    )

    serialized = str(snapshot)
    assert "role-secret" not in serialized
    assert "model-secret" not in serialized
    assert "...cret" not in serialized
    assert snapshot["roles"]["host"] == {
        "model_id": "host", "temperature": 0.4,
        "name": "Host", "provider": "test", "model": "model-x",
    }
    assert snapshot["verification"] == {"required": True, "max_attempts": 2}
    assert snapshot["prompt"] == {"custom": True}
