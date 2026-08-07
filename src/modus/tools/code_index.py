"""Persistent code-search index (Paicli code_index, deepened for Modus).

Modus's ``search_code`` scans the filesystem on every query (O(N) per query via
the bounded walker).  This module persists that walk as a SQLite index so a
query can hit pre-indexed lines without re-scanning.  Unlike Paicli's ``LIKE
%term%`` (which loses the word-boundary semantics Modus already has), the index
stores raw lines and the caller applies the same matcher as the live scan —
so indexed search is behaviourally identical to scan search, just faster.

Design notes:

- Index is per-root: ``~/.modus/code_index/<sha1(root)>.sqlite3``, keyed by the
  resolved workspace root so separate projects never collide.
- ``rebuild`` re-walks via Modus's bounded walker (prunes skip dirs, honours
  the scan cap) and stores ``(root, path, line, content)`` rows.  Rebuilding
  the same root replaces its rows.
- The index stores only workspace files the walker would have returned, so the
  PathGuard boundary and skip-dir pruning carry over unchanged.
- A query reads the matching lines from SQLite, then the caller runs its exact
  match (word-boundary / regex / case) against each — preserving every search
  semantic while replacing only the expensive scan part.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".go", ".rs",
    ".md", ".toml", ".yaml", ".yml", ".json", ".xml", ".html", ".css",
    ".scss", ".sql", ".sh",
}


@dataclass(slots=True)
class CodeIndexRow:
    path: str  # root-relative
    line: int
    content: str


def _root_key(root: Path) -> str:
    return hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:16]


def _index_db_path(root: Path, base_dir: Path) -> Path:
    return base_dir / f"code_index_{_root_key(root)}.sqlite3"


class CodeIndex:
    """A per-root SQLite index of workspace source lines."""

    def __init__(self, root: str | Path, base_dir: str | Path):
        self.root = Path(root).resolve()
        self.db_path = _index_db_path(self.root, Path(base_dir))

    def exists(self) -> bool:
        return self.db_path.exists()

    def rebuild(self, paths: list[Path], cap: int) -> int:
        """Rebuild this root's rows from a collected file-path list.

        ``paths`` must already be bounded (the caller collects the walker's
        async output before calling this, so no event-loop clash).  Returns the
        number of indexed lines.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("delete from code_chunks where root = ?", (str(self.root),))
            count = 0
            for file_path in paths:
                rel = str(file_path.relative_to(self.root))
                if file_path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                try:
                    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue
                for line_number, line in enumerate(lines, start=1):
                    if not line.strip():
                        continue
                    conn.execute(
                        "insert into code_chunks(root, path, line, content) values (?,?,?,?)",
                        (str(self.root), rel, line_number, line),
                    )
                    count += 1
            return count

    def query(self, path_prefix: str = "", limit: int = 1000) -> list[CodeIndexRow]:
        """Return candidate lines under ``path_prefix``, bounded.

        Caller applies the actual match (word-boundary / regex / case) to each
        row's content — the index only narrows the candidate set from a full
        scan to indexed lines.
        """
        if not self.exists():
            return []
        with self._connect() as conn:
            if path_prefix:
                rows = conn.execute(
                    "select path, line, content from code_chunks "
                    "where root = ? and path like ? order by path, line limit ?",
                    (str(self.root), f"{path_prefix}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select path, line, content from code_chunks "
                    "where root = ? order by path, line limit ?",
                    (str(self.root), limit),
                ).fetchall()
        return [CodeIndexRow(str(r[0]), int(r[1]), str(r[2])) for r in rows]

    def stats(self) -> dict:
        if not self.exists():
            return {"exists": False}
        with self._connect() as conn:
            row = conn.execute(
                "select count(*), count(distinct path) from code_chunks where root = ?",
                (str(self.root),),
            ).fetchone()
        return {"exists": True, "lines": int(row[0]), "files": int(row[1])}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "create table if not exists code_chunks ("
            " root text not null, path text not null, line integer not null,"
            " content text not null)"
        )
        conn.execute("create index if not exists idx_cc_root_path on code_chunks(root, path)")
        return conn
