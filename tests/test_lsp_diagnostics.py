"""Lightweight Python static diagnostics (ast-based) for edited files."""

from __future__ import annotations

from modus.lsp.diagnostics import (
    Diagnostic, diagnose_file, diagnose_files, diagnostics_to_text,
)


def test_diagnose_file_reports_syntax_error(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n    pass\n", encoding="utf-8")

    result = diagnose_file(bad)

    assert len(result) == 1
    assert result[0].severity == "error"
    assert result[0].line == 1


def test_diagnose_file_clean_file_returns_empty(tmp_path):
    good = tmp_path / "good.py"
    good.write_text("def ok():\n    return 1\n", encoding="utf-8")

    assert diagnose_file(good) == []


def test_diagnose_file_non_python_or_missing_is_empty(tmp_path):
    txt = tmp_path / "note.txt"
    txt.write_text("x", encoding="utf-8")

    assert diagnose_file(txt) == []
    assert diagnose_file(tmp_path / "missing.py") == []


def test_diagnostics_text_is_bounded_and_reference_only(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("x = (1\n", encoding="utf-8")

    text = diagnostics_to_text(diagnose_file(bad))

    assert "REFERENCE ONLY" in text
    assert "bad.py:1" in text


def test_diagnose_files_sorts_and_caps(tmp_path):
    one = tmp_path / "one.py"
    two = tmp_path / "two.py"
    one.write_text("def a(:\n", encoding="utf-8")
    two.write_text("def b(:\n", encoding="utf-8")

    result = diagnose_files([two, one])

    assert [d.path for d in result] == [str(one), str(two)]
