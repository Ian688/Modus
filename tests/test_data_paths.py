from pathlib import Path

from modus.config import load_config
from modus.paths import data_dir, data_path


def test_data_directory_defaults_to_modus_location(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert data_dir({}) == tmp_path / ".modus"
    assert data_path("desktop.db", {}) == tmp_path / ".modus" / "desktop.db"


def test_modus_data_directory_can_be_overridden(tmp_path):
    selected = tmp_path / "modus-data"

    assert data_dir({"MODUS_DATA_DIR": str(selected)}) == selected


def test_config_reads_user_file_from_selected_data_directory(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "config.json").write_text('{"llm":{"model":"from-selected-dir"}}')

    config = load_config(
        project_root=tmp_path,
        env={"MODUS_DATA_DIR": str(selected)},
    )

    assert config.llm.model == "from-selected-dir"
    assert config.memory.auto_memorize is False
    assert config.memory.max_retrieval_results == 8
