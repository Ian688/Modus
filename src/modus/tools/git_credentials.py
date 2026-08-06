"""Git remote credentials backed by the shared credential backend.

Reuses ``modus.desktop.credential_backend`` (JSON file or macOS Keychain) with
``service="modus"`` and account keys of the form ``git:<remote>``.  Only a
trailing hint (last 4 chars) is ever exposed to the browser.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

_GIT_CRED_SERVICE = "modus"
_GIT_CRED_PREFIX = "git:"


def _backend():
    try:
        from modus.desktop import credential_backend as cb
        from modus.config import load_config

        cfg = load_config()
        marker = getattr(cfg, "credential_marker", None) or cb.KEYCHAIN_MARKER
        return cb.backend_for(marker, service=_GIT_CRED_SERVICE)
    except Exception:
        # Fall back to JSON so git creds still work in non-Desktop runs.
        from modus.desktop.credential_backend import JsonCredentialBackend

        return JsonCredentialBackend()


def _account_key(remote: str) -> str:
    return _GIT_CRED_PREFIX + str(remote or "").strip().lower()


def _record_for(remote: str) -> dict[str, Any]:
    # The backend expects a model record dict; git keys are self-describing.
    return {"id": _account_key(remote), "name": remote}


def set_git_credential(remote: str, username: str, password: str) -> None:
    """Persist a credential for one remote. ``password`` may be a token."""
    remote = str(remote or "").strip()
    if not remote:
        raise ValueError("remote is required")
    combined = f"{username}:{password}"
    _backend().set_credential(_account_key(remote), combined)


def get_git_credential(remote: str) -> str | None:
    try:
        return _backend().get_credential(_account_key(remote), _record_for(remote))
    except Exception:
        return None


def has_git_credential(remote: str) -> bool:
    try:
        return _backend().has_credential(_account_key(remote), _record_for(remote))
    except Exception:
        return False


def clear_git_credential(remote: str) -> None:
    try:
        _backend().remove_credential(_account_key(remote), _record_for(remote))
    except Exception:
        pass


def git_credential_hint(remote: str) -> str:
    """Browser-safe tail hint (last 4 chars of the secret), or '' if absent."""
    try:
        value = get_git_credential(remote)
    except Exception:
        return ""
    if not value:
        return ""
    return "…" + value[-4:]
