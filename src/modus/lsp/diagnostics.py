"""Lightweight static diagnostics for edited Python files.

PaiCLI injects parser diagnostics after an edit so the model sees compile
errors before it claims a change is done.  Modus mirrors this for Python using
the stdlib ``ast`` module — no language server, no subprocess, no network.

The diagnostic is a bounded, sorted list the runner can inject into the model
context as a plain user message (never as a tool result or instruction).
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Diagnostic:
    path: str
    line: int
    column: int
    message: str
    severity: str = "error"
    source: str = "ast"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "line": self.line, "column": self.column,
            "message": self.message, "severity": self.severity, "source": self.source,
        }


def diagnose_file(path: str | Path, *, max_results: int = 20) -> list[Diagnostic]:
    """Parse one Python file and return syntax diagnostics, newest bounded.

    Empty list means the file parses cleanly (or is not a Python file / does
    not exist).  Never raises.
    """
    file_path = Path(path)
    if not file_path.is_file() or file_path.suffix != ".py":
        return []
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        ast.parse(text, filename=str(file_path))
    except SyntaxError as exc:
        lineno = exc.lineno or 1
        col = exc.offset or 1
        message = exc.msg or "syntax error"
        return [Diagnostic(
            path=str(file_path), line=lineno, column=col,
            message=message, severity="error",
        )]
    except ValueError:
        return [Diagnostic(
            path=str(file_path), line=1, column=1,
            message="null bytes or invalid encoding", severity="error",
        )]
    return []


def diagnose_files(paths: list[str | Path], *, max_total: int = 50) -> list[Diagnostic]:
    """Diagnose many edited files, capped and sorted by path/line/column."""
    results: list[Diagnostic] = []
    for path in paths:
        if len(results) >= max_total:
            break
        for diagnostic in diagnose_file(path):
            results.append(diagnostic)
            if len(results) >= max_total:
                break
    results.sort(key=lambda d: (d.path, d.line, d.column))
    return results


def diagnostics_to_text(diagnostics: list[Diagnostic], *, max_chars: int = 2_000) -> str:
    """Render diagnostics as a bounded, reference-only text block for the model."""
    if not diagnostics:
        return ""
    lines = [
        f"{d.path}:{d.line}:{d.column} [{d.severity}] {d.message}"
        for d in diagnostics
    ]
    text = (
        "[LSP DIAGNOSTICS — REFERENCE ONLY]\n"
        "The following Python files have syntax errors after the last edit:\n"
        + "\n".join(lines)
    )
    if len(text) > max_chars:
        text = text[: max_chars - 60].rstrip() + "\n[... diagnostics truncated ...]"
    return text
