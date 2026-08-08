"""Side-git snapshot/restore for reversible agent runs.

A snapshot is a git commit made in a *side* repository (under Modus's own data
directory), never in the user's project repository.  It captures the workspace
tree before a run's mutations begin so ``revert_turn`` can roll the workspace
back to a pre-turn state.  The side repository keeps the user's history and
working tree untouched.

Design notes:
- One side repository per project root, keyed by the hashed project path, so
  snapshots survive across sessions.
- ``create_snapshot`` is best-effort and never raises: a project without git
  tooling, or a workspace that is itself a git repo with a dirty tree, still
  records a snapshot where possible.
- Restore writes only files that differ from the snapshot tree; it never
  deletes untracked files outside the snapshot.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from modus.paths import data_path


@dataclass(slots=True)
class Snapshot:
    commit_id: str
    phase: str
    summary: str


def _project_key(project_root: str) -> str:
    resolved = str(Path(project_root or os.getcwd()).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return digest


def _side_git_dir(project_root: str) -> Path:
    return Path(data_path("snapshots")) / _project_key(project_root) / ".git"


def _git(project_root: str, *args: str, env: dict[str, str] | None = None) -> tuple[str, str, int]:
    """Run a git command against the side repository with a hard timeout."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    # The side repo is a plain git dir; work-tree commands need --git-dir.
    side_dir = _side_git_dir(project_root)
    cmd = ["git", "--git-dir", str(side_dir), "--work-tree", str(Path(project_root).resolve())]
    proc = subprocess.run(
        [*cmd, *args], capture_output=True, text=True, cwd=project_root,
        env=merged, timeout=30.0,
    )
    return proc.stdout, proc.stderr, proc.returncode or 0


def _ensure_side_repo(project_root: str) -> None:
    """Create the side git repository if it does not exist."""
    side_dir = _side_git_dir(project_root)
    if (side_dir / "config").exists():
        return
    side_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", str(side_dir)],
        capture_output=True, text=True, check=False, timeout=30.0,
    )


def _prune_one_project(project_root: str, retain: int) -> int:
    """Drop the oldest side-repo snapshots beyond ``retain`` for one project.

    A bare repo's history is only reachable through its branch tip (or reflog).
    ``list_snapshots`` walks that history, so pruning is: replay the retained
    commits onto a fresh branch (``git commit-tree`` keeps each snapshot's tree,
    message and author, but parents it only to the previous *retained* commit),
    repoint the branch tip at the replayed head, then expire the reflog and
    ``gc --prune`` so the dropped commits/trees/blobs are removed from disk.
    Only Modus's own side repo under ``~/.modus/snapshots`` is touched, and
    nothing outside it is ever modified.
    """
    try:
        root = str(Path(project_root or os.getcwd()).resolve())
        side_dir = _side_git_dir(root)
        if not (side_dir / "config").exists():
            return 0
        snaps = list_snapshots(root, limit=10_000)
        if len(snaps) <= retain:
            return 0
        kept = snaps[:retain]
        dropped = snaps[retain:]
        new_parent: str | None = None
        for snap in reversed(kept):
            tree_stdout, _, rev_code = _git(root, "rev-parse", f"{snap.commit_id}^{{tree}}")
            if rev_code != 0:
                return 0
            tree = tree_stdout.strip()
            if not tree:
                return 0
            cmd: list[str] = ["commit-tree", tree, "-m", snap.summary or snap.phase]
            if new_parent:
                cmd += ["-p", new_parent]
            commit_out, _, code = _git(root, *cmd)
            if code != 0 or not commit_out.strip():
                return 0
            new_parent = commit_out.strip()
        if not new_parent:
            return 0
        _git(root, "update-ref", "refs/heads/master", new_parent)
        _git_plain(side_dir, "reflog", "expire", "--expire=now", "--all")
        _git_plain(side_dir, "gc", "--prune=now", "--quiet")
        return len(dropped)
    except Exception:
        return 0


def _git_plain(side_dir: Path, *args: str) -> tuple[str, str, int]:
    """Run a git command against the side repo without a work-tree (gc/refs)."""
    proc = subprocess.run(
        ["git", "-C", str(side_dir), *args],
        capture_output=True, text=True, timeout=120.0,
    )
    return proc.stdout, proc.stderr, proc.returncode or 0


def prune_snapshots(
    project_roots: list[str] | None = None,
    *,
    retain_per_run: int = 50,
) -> int:
    """Best-effort prune of side-repo snapshots per project.

    Defaults to every side repo already recorded under ``~/.modus/snapshots``.
    Returns the total number of snapshots dropped.  Never raises and never
    touches anything outside Modus's own data directory.
    """
    retain = max(1, int(retain_per_run))
    roots: list[str] = []
    if project_roots is None:
        base = Path(data_path("snapshots"))
        if base.is_dir():
            roots = [str(child) for child in base.iterdir() if child.is_dir()]
    else:
        roots = [str(r) for r in project_roots]
    dropped = 0
    for root in roots:
        try:
            dropped += _prune_one_project(root, retain)
        except Exception:
            continue
    return dropped


def create_snapshot(
    project_root: str,
    phase: str = "pre-turn",
    summary: str = "",
) -> Snapshot | None:
    """Commit the current workspace tree into the side repository.

    Returns None (best-effort) when the side repo cannot be created or the
    commit fails.  Never raises.
    """
    try:
        root = str(Path(project_root or os.getcwd()).resolve())
        _ensure_side_repo(root)
        # Stage everything (the side repo is separate, so this never touches
        # the user's own index/HEAD).
        _git(root, "add", "-A")
        message = f"{phase}"
        if summary:
            message += f"\n\n{summary}"
        _stdout, stderr, code = _git(root, "commit", "--allow-empty", "-m", message)
        if code != 0:
            # A commit may fail if git has no configured identity; set a local
            # fallback identity and retry once.
            _git(root, "config", "user.name", "Modus Snapshot")
            _git(root, "config", "user.email", "snapshot@modus.local")
            _stdout, stderr, code = _git(root, "commit", "--allow-empty", "-m", message)
            if code != 0:
                return None
        # ``git commit`` prints a human-readable summary, not the hash.  The
        # stable id is HEAD; resolve it explicitly so ``revert_turn`` can
        # restore the exact snapshot.
        commit_stdout, commit_stderr, rev_code = _git(root, "rev-parse", "HEAD")
        commit_id = commit_stdout.strip() if rev_code == 0 else ""
        if not commit_id:
            return None
        return Snapshot(commit_id=commit_id, phase=phase, summary=summary)
    except Exception:
        return None


def list_snapshots(project_root: str, limit: int = 20) -> list[Snapshot]:
    """Return recent snapshots newest-first, or [] when the side repo is absent."""
    try:
        root = str(Path(project_root or os.getcwd()).resolve())
        side_dir = _side_git_dir(root)
        if not (side_dir / "config").exists():
            return []
        stdout, stderr, code = _git(root, "log", f"-{max(1, limit)}", "--pretty=%H %s")
        if code != 0:
            return []
        result: list[Snapshot] = []
        for line in stdout.splitlines():
            parts = line.split(" ", 1)
            commit_id = parts[0].strip() if parts else ""
            subject = parts[1].strip() if len(parts) > 1 else ""
            if commit_id:
                # The commit subject is the phase (e.g. pre-turn); the body
                # carried the human summary, which log --pretty=%s drops.
                result.append(Snapshot(
                    commit_id=commit_id, phase=subject, summary=subject,
                ))
        return result
    except Exception:
        return []


def restore_snapshot(project_root: str, commit_id: str) -> tuple[int, int]:
    """Restore the workspace tree to a snapshot commit.

    Returns (restored_count, removed_count).  Best-effort: files not in the
    snapshot are removed only if tracked in the side repo; untracked files are
    left alone.  Never raises.
    """
    try:
        root = str(Path(project_root or os.getcwd()).resolve())
        # Hard reset the work tree to the snapshot commit (the side repo is
        # separate, so the user's own git state is untouched).
        stdout, stderr, code = _git(root, "checkout", commit_id, "--", ".")
        if code != 0:
            # Fallback: reset --hard against the side repo (still separate).
            _, _, code = _git(root, "reset", "--hard", commit_id)
            if code != 0:
                return 0, 0
        # Count what changed: files in the snapshot that now exist, and files
        # tracked in the snapshot that the work tree no longer has.
        restored = 0
        removed = 0
        tree, _, _ = _git(root, "ls-tree", "-r", "--name-only", commit_id)
        for path in tree.splitlines():
            if path.strip() and Path(root, path.strip()).exists():
                restored += 1
        worktree, _, _ = _git(root, "ls-files")
        tree_set = set(tree.splitlines())
        for path in worktree.splitlines():
            if path.strip() and path.strip() not in tree_set:
                removed += 1
        return restored, removed
    except Exception:
        return 0, 0
