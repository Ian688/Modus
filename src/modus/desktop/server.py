from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from pathlib import Path

import uvicorn

from modus.agent import QueryEngine
from modus.bootstrap import build_tool_registry
from modus.config import load_config
from modus.llm import create_llm_client

from modus.desktop.db import (
    init_db, create_session, get_session, list_sessions, update_session, delete_session,
    add_message, restore_session, upsert_run_event, settle_run_event,
    create_run, create_run_admission, fail_run_admission,
    get_run, get_run_by_client_request_id,
    TERMINAL_RUN_STATES, ADMISSION_FAILURE_STOP_REASONS,
    get_run_events_since, get_session_run_history,
    latest_run_for_session, get_run_task,
    get_legacy_messages, session_catalog_page, upsert_workspace,
)
from modus.desktop.approval_flow import resolve_pending_approval, wait_for_user_approval
from modus.desktop.events import Actor, ChannelId, EventStatus, EventType, RunEventEmitter
from modus.desktop.session_state import (
    DaoSession, SessionManager, active_run_owner, start_session_run,
)
from modus.desktop.default_runner import stream_to_ws
from modus.desktop.moa_runner import run_moa_stream
from modus.desktop.peri_runner import run_peri_stream
from modus.extensions import ExtensionRegistry
from modus.mcp_client import McpManager, McpServerConfig
from modus.desktop.model_repository import ModelRepository
from modus.desktop.model_discovery import CAPABILITY_FIELDS, discover_models
from modus.skills import SkillRepository
from modus.runtime.controller import RunController
from modus.runtime.budget import StopReason
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.executor import ToolExecutor
from modus.tools.payload import tool_result_event
from modus.tools.registry import ToolRegistry
from modus.runtime.state import RunState
from modus.types import Message
from modus.redact import redact_text
from modus.desktop.session_management import SessionDocument, export_sessions, session_skill_specs
from modus.desktop.run_config import build_run_config_snapshot
from modus.desktop.command_router import DesktopCommandRouter
from modus.desktop.workbench import build_workbench_run, build_workbench_snapshot
from modus.desktop.workspace import WorkspaceIdentity
from modus.desktop.directory_picker import (
    DirectoryPickerError, DirectoryPickerUnavailable, pick_directory,
)
from modus.modes import (
    AGI_MODE, COLLABORATION_MODES, DEFAULT_MODE, MOA_MODE, PERI_MODE, normalize_mode,
)
from modus.paths import data_dir

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)
DESKTOP_PROTOCOL_VERSION = 2


class _SerializedWebSocket:
    """Serialize every outbound packet for one ASGI WebSocket connection."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._send_lock = asyncio.Lock()

    async def send_json(self, packet: Any) -> None:
        async with self._send_lock:
            try:
                await self._websocket.send_json(packet)
            except RuntimeError as exc:
                # Starlette raises RuntimeError rather than WebSocketDisconnect
                # when a background sender observes a socket whose close frame
                # was already processed.  Normalize only that concrete state;
                # application/runtime errors must retain their real semantics.
                if (
                    getattr(self._websocket, "application_state", None)
                    is WebSocketState.DISCONNECTED
                ):
                    raise WebSocketDisconnect(code=1006) from exc
                raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._websocket, name)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    from modus.desktop.db import interrupt_nonterminal_runs

    init_db()
    if _desktop_default_workspace_root:
        upsert_workspace(WorkspaceIdentity.from_path(_desktop_default_workspace_root))
    interrupt_nonterminal_runs()
    await mcp_manager.connect_all()
    try:
        yield
    finally:
        await mcp_manager.disconnect_all()


app = FastAPI(title="Modus Desktop", lifespan=lifespan)

# The launcher may provide a project explicitly via ``modus serve --cwd``.
# Starting the Desktop from its source checkout must never select that checkout
# as an Agent workspace by accident.
_desktop_default_workspace_root: str | None = None

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
STATIC.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
command_router = DesktopCommandRouter()


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Disable heuristic caching for /static so the browser always revalidates.

    Modus serves a local SPA whose JS/CSS change often during development.  With
    no Cache-Control header the browser may serve a stale bundle from memory
    cache, so the UI silently diverges from what the server actually sends.
    ``no-cache`` keeps the ETag/Last-Modified conditional request (cheap 304s)
    while guaranteeing every edit is picked up on the next load.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

@app.get("/")
async def index():
    return FileResponse(str(STATIC / "index.html"))

# ── REST API ──
from pydantic import BaseModel

class ChatRequest(BaseModel):
    message: str
    session_id: str = ""

@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """Account data is unavailable outside an account-bound WebSocket."""
    return JSONResponse(
        {"error": "use the authenticated Desktop WebSocket"}, status_code=401,
    )

@app.get("/api/busy")
async def api_busy():
    """返回当前是否有活跃的任务在执行"""
    return {"busy": bool(manager.active_run_owners())}


@app.get("/api/health")
async def api_health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/workspaces")
async def api_workspaces():
    """Workspace data requires the account-bound WebSocket."""
    return JSONResponse(
        {"error": "use the authenticated Desktop WebSocket"}, status_code=401,
    )


@app.post("/api/workspace/open")
async def api_workspace_open(req: dict):
    """Open (or re-open) a project directory as a workspace.

    Validates the path is an existing directory, derives a canonical
    ``WorkspaceIdentity``, persists it, and returns the identity.
    """
    return JSONResponse(
        {"ok": False, "error": "use the authenticated Desktop WebSocket"},
        status_code=401,
    )


# URL metadata cache: {url: (ts, meta)}. Bounds repeated fetches from attached
# links and reply link cards; entries expire after _URL_META_TTL.
_url_meta_cache: dict[str, tuple[float, dict]] = {}
_URL_META_TTL = 600.0


@app.get("/api/url-meta")
async def api_url_meta(url: str = ""):
    """Fetch title / favicon / description for an attached URL.

    Reuses the web-fetch SSRF validation (public http(s), DNS-rebinding and
    private-IP rejection) so the preview never leaks internal hosts. On any
    failure the endpoint degrades to an empty meta object — the frontend shows
    the plain URL card either way.
    """
    import re as _re
    import time as _time
    from urllib.parse import urljoin

    import httpx

    from modus.web.fetch import _validate_public_url, extract_text_from_html

    url = str(url or "").strip()
    if not url:
        return JSONResponse({"error": "url parameter is required"}, status_code=400)
    try:
        _validate_public_url(url)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    now = _time.time()
    cached = _url_meta_cache.get(url)
    if cached and (now - cached[0]) < _URL_META_TTL:
        return cached[1]
    meta: dict = {"url": url, "title": "", "favicon": "", "description": ""}
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"user-agent": "Modus/0.1.0"})
            response.raise_for_status()
            raw = response.text
        title = ""
        title_match = _re.search(r"<title[^>]*>(.*?)</title>", raw, _re.I | _re.S)
        if title_match:
            title = _re.sub(r"\s+", " ", title_match.group(1)).strip()[:200]
        og_title = _re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            raw, _re.I,
        )
        if og_title and not title:
            title = og_title.group(1).strip()[:200]
        desc_match = _re.search(
            r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)["\']',
            raw, _re.I,
        )
        description = desc_match.group(1).strip()[:300] if desc_match else ""
        icon_match = _re.search(
            r'<link[^>]+rel=["\'](?:shortcut\s+)?icon["\'][^>]*>', raw, _re.I,
        )
        favicon = ""
        if icon_match:
            href = _re.search(r'href=["\']([^"\']+)["\']', icon_match.group(0), _re.I)
            if href:
                favicon = urljoin(url, href.group(1))
        text = extract_text_from_html(raw)
        meta = {
            "url": url, "title": title or url,
            "favicon": favicon,
            "description": description or text[:200],
        }
    except Exception:
        meta = {"url": url, "title": url, "favicon": "", "description": ""}
    _url_meta_cache[url] = (now, meta)
    return meta


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _require_loopback_preview(url: str) -> str:
    """Validate a built-in-browser target: loopback only, http/https only.

    The preview iframe proxies a dev server the Agent just started on this
    machine, so cross-origin isolation would block a direct iframe load.  The
    proxy is intentionally narrow: any non-loopback hostname is rejected
    regardless of DNS result, mirroring the public-web ``_validate_public_url``
    guard in reverse (loopback is the ONLY allowed target here).
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http/https URLs can be previewed")
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("preview is restricted to localhost")
    return url


@app.get("/api/preview")
async def api_preview(url: str = ""):
    """Proxy a localhost dev server for the built-in browser window.

    The Agent's ``tool_result`` may carry a running dev-server URL (for example
    ``http://localhost:5173``).  The right-panel browser window loads it through
    this endpoint so the iframe is same-origin and never blocked by CORS.
    """
    import httpx

    if not url.strip():
        return JSONResponse({"error": "url parameter is required"}, status_code=400)
    try:
        target = _require_loopback_preview(url)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        # Loopback-only targets never need a proxy; bypass environment proxies
        # so a configured http_proxy cannot intercept (or 502) local dev servers.
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, trust_env=False) as client:
            response = await client.get(target)
    except Exception as exc:  # connection refused / DNS / timeout
        return JSONResponse({"error": f"无法连接 {target}: {redact_text(str(exc))}"}, status_code=502)
    content_type = response.headers.get("content-type", "text/html").split(";")[0]
    if content_type in {"text/html", "application/json", "text/javascript", "text/css"}:
        return Response(content=response.content, media_type=content_type, status_code=response.status_code)
    return Response(content=response.content, media_type="application/octet-stream", status_code=response.status_code)


@app.post("/api/test-model")
async def api_test_model(req: dict):
    """Model credentials require an account-bound WebSocket."""
    return JSONResponse(
        {"success": False, "error": "use the authenticated Desktop WebSocket"},
        status_code=401,
    )


async def _test_model_connection(req: dict[str, Any]) -> dict[str, Any]:
    """Test unsaved editor values or a saved repository model without saving."""
    from modus.config import LlmConfig

    model_id = str(req.get("model_id") or req.get("id") or "")
    saved = model_repository.runtime_model(model_id) if model_id else None
    if model_id and saved is None:
        raise ValueError("unknown model_id")
    supplied_key = str(req.get("api_key") or "").strip()
    if saved is not None and not supplied_key:
        requested_identity = (
            str(req.get("provider") or saved.get("provider") or ""),
            str(req.get("model") or saved.get("model") or ""),
            str(req.get("base_url") or saved.get("base_url") or ""),
        )
        saved_identity = (
            str(saved.get("provider") or ""), str(saved.get("model") or ""),
            str(saved.get("base_url") or ""),
        )
        if requested_identity != saved_identity:
            raise ValueError("修改 Provider、模型 ID 或 Endpoint 后需要重新输入 API Key")
    provider = str(req.get("provider") or (saved or {}).get("provider") or "deepseek")
    model = str(req.get("model") or (saved or {}).get("model") or "")
    api_key = supplied_key or str((saved or {}).get("api_key") or "")
    base_url = req.get("base_url") or (saved or {}).get("base_url") or None
    if not model:
        raise ValueError("model is required")
    if not api_key:
        raise ValueError("API key is required")
    cfg = LlmConfig(
        provider=provider, model=model, api_key=api_key, base_url=base_url,
        max_tokens=int(req.get("max_output_tokens") or (saved or {}).get("max_output_tokens") or 8192),
        max_context_window=int(req.get("context_window") or (saved or {}).get("context_window") or 128000),
        supports_tools=bool(req.get("supports_tools", (saved or {}).get("supports_tools", True))),
        supports_images=bool(req.get("supports_images", (saved or {}).get("supports_images", False))),
        reasoning_effort=str(req.get("reasoning_effort") or (saved or {}).get("default_reasoning_effort") or "") or None,
    )
    client = create_llm_client(cfg)
    response = ""
    async for event in client.chat(
        [Message(role="user", content="回复 OK 即表示连接正常。")], [],
        system_prompt="You are a connection test. Reply with OK.",
    ):
        if event.get("type") == "text_delta":
            response += str(event.get("text") or "")
        elif event.get("type") == "error":
            return {"success": False, "error": redact_text(str(event.get("error") or "connection failed"))}
    return {"success": True, "response": response[:100]}

manager = SessionManager()

# ── 模型仓库与凭据持久化 ──
DESKTOP_DATA_DIR = data_dir()
MODELS_FILE = DESKTOP_DATA_DIR / "models.json"
model_repository = ModelRepository(MODELS_FILE)
skill_repository = SkillRepository(DESKTOP_DATA_DIR / "skills")
mcp_manager = McpManager(DESKTOP_DATA_DIR / "mcp_servers.json")
extension_registry = ExtensionRegistry(mcp_manager=mcp_manager)


@dataclass(slots=True)
class OwnerResources:
    models: ModelRepository
    skills: SkillRepository
    mcp: McpManager
    extensions: ExtensionRegistry


_owner_resources: dict[str, OwnerResources] = {}
_default_resources = OwnerResources(
    model_repository, skill_repository, mcp_manager, extension_registry,
)
_active_owner_id: ContextVar[str] = ContextVar("modus_active_owner_id", default="")


def _safe_owner_segment(owner_id: str) -> str:
    value = str(owner_id or "").strip()
    if not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in value):
        raise ValueError("invalid owner id")
    return value


def _resources_for(owner_id: str) -> OwnerResources:
    """Return the account-private model, Skill and MCP repositories."""
    from modus.desktop import accounts

    owner = _safe_owner_segment(owner_id)
    cached = _owner_resources.get(owner)
    if cached is not None:
        return cached
    default_owner = str(accounts.ensure_default_user()["user_id"])
    if owner == default_owner:
        resources = _default_resources
    else:
        root = DESKTOP_DATA_DIR / "users" / owner
        owner_mcp = McpManager(root / "mcp_servers.json")
        resources = OwnerResources(
            ModelRepository(
                root / "models.json", keychain_service=f"modus:{owner}",
            ),
            SkillRepository(root / "skills"), owner_mcp,
            ExtensionRegistry(mcp_manager=owner_mcp),
        )
        callback = globals().get("_handle_mcp_tools_changed")
        if callback is not None:
            async def owner_tools_changed(server_name: str) -> None:
                token = _active_owner_id.set(owner)
                try:
                    await callback(server_name)
                finally:
                    _active_owner_id.reset(token)
            owner_mcp.set_tools_changed_callback(owner_tools_changed)
        _owner_resources[owner] = resources
    return resources


def _session_resources(session: DaoSession) -> OwnerResources:
    # Tests and embedders may replace the legacy module-level repositories.
    # Keep that supported for the default account while real account routing
    # still uses the immutable bundle captured above.
    if isinstance(model_repository, ModelRepository):
        return OwnerResources(
            model_repository,
            skill_repository if isinstance(skill_repository, SkillRepository) else _default_resources.skills,
            mcp_manager if isinstance(mcp_manager, McpManager) else _default_resources.mcp,
            extension_registry if isinstance(extension_registry, ExtensionRegistry) else _default_resources.extensions,
        )
    return _resources_for(session.owner_id)


class _OwnerResourceProxy:
    def __init__(self, attribute: str) -> None:
        self.attribute = attribute

    def _target(self) -> Any:
        owner_id = _active_owner_id.get()
        resources = _resources_for(owner_id) if owner_id else _default_resources
        return getattr(resources, self.attribute)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)


# Existing call sites keep their concise repository names, while ContextVar
# routing makes every concurrent WebSocket resolve the active account's store.
model_repository = _OwnerResourceProxy("models")
skill_repository = _OwnerResourceProxy("skills")
mcp_manager = _OwnerResourceProxy("mcp")
extension_registry = _OwnerResourceProxy("extensions")


def _load_models() -> dict[str, Any]:
    """Build the canonical credential-bearing runtime view for both modes.

    The public WebSocket protocol never calls this helper: it deliberately
    contains credentials so runners can construct LLM clients. Browser-facing
    code must use ``model_repository.public_snapshot`` instead.
    """
    return {
        "moa_roles": model_repository.runtime_mode_configuration(MOA_MODE),
        "peri_roles": model_repository.runtime_mode_configuration(PERI_MODE),
    }


def _apply_runtime_model(config: Any, selected_model: dict[str, Any] | None) -> Any:
    """Apply one repository model without leaking its credential to clients."""
    if not selected_model:
        return config
    config.llm.provider = str(selected_model["provider"])
    config.llm.model = str(selected_model["model"])
    config.llm.api_key = str(selected_model.get("api_key") or "")
    config.llm.base_url = selected_model.get("base_url") or None
    config.llm.max_tokens = int(selected_model.get("max_output_tokens") or config.llm.max_tokens)
    config.llm.max_context_window = int(selected_model.get("context_window") or config.llm.max_context_window)
    config.llm.supports_tools = bool(selected_model.get("supports_tools", True))
    config.llm.supports_images = bool(selected_model.get("supports_images", False))
    config.llm.reasoning_effort = selected_model.get("default_reasoning_effort") or None
    return config


async def _build_session_engine(
    model_id: str | None = None, role_config: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    workspace_root: str | Path | None = None,
) -> Any:
    """Build the Host engine for the selected repository model."""
    config = _apply_runtime_model(load_config(), model_repository.runtime_model(model_id))
    if role_config:
        config.llm.temperature = float(role_config.get("temperature", config.llm.temperature))
        config.llm.reasoning_effort = role_config.get("reasoning_effort") or config.llm.reasoning_effort
        context_tokens = int(role_config.get("context_tokens") or config.llm.max_context_window)
        config.llm.max_context_window = min(config.llm.max_context_window, context_tokens)
        # Context compaction starts before the provider's hard limit, leaving
        # space for system prompt, tools and the next completion.
        config.features.compression.trigger_tokens = min(
            config.features.compression.trigger_tokens,
            max(1_024, int(context_tokens * 0.8)),
        )
    if reasoning_effort is not None:
        config.llm.reasoning_effort = str(reasoning_effort or "") or None
    test_workspace = _approval_e2e_workspace()
    if test_workspace is not None:
        return ApprovalE2EFixtureEngine(config=config, workspace=test_workspace)
    workspace = (
        WorkspaceIdentity.from_path(workspace_root)
        if workspace_root is not None and str(workspace_root).strip() else None
    )
    # The tool registry is always populated.  With no workspace the engine
    # anchors to the home directory so the Agent keeps its full capability;
    # the home-anchored PathGuard still enforces the boundary.
    engine_cwd = workspace.root if workspace else str(Path.home())
    registry = await build_tool_registry(
        config=config, cwd=engine_cwd,
        extension_registry=extension_registry,
    )
    return QueryEngine(
        llm_client=create_llm_client(config.llm), tool_registry=registry,
        config=config, cwd=engine_cwd,
    )


def _apply_session_prompt(engine: Any, prompt: str) -> Any:
    """Attach one session custom prompt without replacing the Modus base prompt."""
    custom = str(prompt or "").strip()
    if not custom or not hasattr(engine, "system_prompt"):
        return engine
    base = str(getattr(engine, "system_prompt", "") or "").strip()
    engine.system_prompt = custom if not base else custom + "\n\n" + base
    return engine


async def _rebuild_session_engine(session: DaoSession) -> None:
    """Rebuild the current Host from authoritative session and repository state."""
    session.engine = await _build_session_engine(
        session.model_id or None, session.mode_config.get("host"),
        session.reasoning_effort, session.workspace_root,
    )
    _apply_session_prompt(session.engine, session.system_prompt)


def _session_mode_snapshot(mode: str) -> dict[str, Any]:
    mode = normalize_mode(mode)
    if mode not in COLLABORATION_MODES:
        return {}
    return model_repository.public_snapshot()["selection"].get(f"{mode}_roles", {})


def _mode_roles_complete(mode: str, roles: dict[str, Any]) -> bool:
    """A collaboration mode needs one Host and at least one participant."""
    mode = normalize_mode(mode)
    if mode not in COLLABORATION_MODES or not isinstance(roles, dict):
        return mode == DEFAULT_MODE
    participants = (
        ("reference_1", "reference_2") if mode == MOA_MODE
        else ("worker_1", "worker_2")
    )
    host_id = str((roles.get("host") or {}).get("model_id") or "")
    return bool(host_id and any(str((roles.get(role) or {}).get("model_id") or "") for role in participants))


def _session_references_model(session: DaoSession, model_id: str) -> bool:
    if session.model_id == model_id:
        return True
    return any(
        isinstance(raw, dict) and str(raw.get("model_id") or "") == model_id
        for raw in session.mode_config.values()
    )


def _repair_mode_snapshot_after_delete(
    mode: str, snapshot: dict[str, Any], deleted_id: str,
    repository_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Remove one model from a session snapshot without importing global roles."""
    mode = normalize_mode(mode)
    if mode not in COLLABORATION_MODES:
        return {}
    known_ids = {
        str(model.get("id") or "") for model in repository_snapshot.get("models", [])
        if isinstance(model, dict)
    }
    default_id = str(
        (repository_snapshot.get("selection") or {}).get("default_model_id") or ""
    )
    role_names = (
        ("host", "reference_1", "reference_2") if mode == MOA_MODE
        else ("host", "worker_1", "worker_2")
    )
    source = snapshot if isinstance(snapshot, dict) else {}
    raw_host = source.get("host") if isinstance(source.get("host"), dict) else None
    host_id = str((raw_host or {}).get("model_id") or "")
    host = dict(raw_host) if host_id in known_ids and host_id != deleted_id else None
    if host is None and default_id in known_ids:
        host = {"model_id": default_id}
    seen = {str((host or {}).get("model_id") or "")}
    participants: list[dict[str, Any]] = []
    for role in role_names[1:]:
        raw = source.get(role)
        participant_id = str((raw or {}).get("model_id") or "") if isinstance(raw, dict) else ""
        if participant_id not in known_ids or participant_id == deleted_id or participant_id in seen:
            continue
        seen.add(participant_id)
        participants.append(dict(raw))
    repaired = {"host": host} if host else {}
    repaired.update({role: raw for role, raw in zip(role_names[1:], participants)})
    return repaired


def _reset_invalid_reasoning(session: DaoSession) -> None:
    model = model_repository.runtime_model(session.model_id or None)
    allowed = {str(item) for item in (model or {}).get("reasoning_efforts", [])}
    if session.reasoning_effort and session.reasoning_effort not in allowed:
        session.reasoning_effort = None


async def _repair_session_repository_binding(session: DaoSession) -> bool:
    """Repair stale model/role references and rebuild the Host when needed."""
    data = model_repository.public_snapshot()
    known_ids = {
        str(model.get("id") or "") for model in data.get("models", [])
        if isinstance(model, dict)
    }
    default_id = str((data.get("selection") or {}).get("default_model_id") or "")
    original = (
        session.mode, session.model_id, dict(session.mode_config),
        session.reasoning_effort,
    )
    if session.mode in COLLABORATION_MODES:
        repaired_roles = _repair_mode_snapshot_after_delete(
            session.mode, session.mode_config, "", data,
        )
        if _mode_roles_complete(session.mode, repaired_roles):
            session.mode_config = repaired_roles
            session.model_id = _host_model_id(repaired_roles, default_id)
        else:
            session.mode = DEFAULT_MODE
            session.mode_config = {}
            session.model_id = default_id
    elif session.model_id not in known_ids:
        session.mode = DEFAULT_MODE
        session.mode_config = {}
        session.model_id = default_id
    _reset_invalid_reasoning(session)
    current = (
        session.mode, session.model_id, dict(session.mode_config),
        session.reasoning_effort,
    )
    if current == original:
        return False
    await _rebuild_session_engine(session)
    if get_session(session.db_id, owner_id=session.owner_id) is not None:
        update_session(
            session.db_id, mode=session.mode, mode_config=session.mode_config,
            model_id=session.model_id,
            reasoning_effort=session.reasoning_effort or "",
        )
    return True


async def _reject_global_model_mutation_while_running(
    websocket: WebSocket, current: DaoSession, operation: str,
) -> bool:
    """A shared repository cannot mutate while any session is running."""
    active = next((
        item for item in manager.active_run_owners()
        if item.owner_id == current.owner_id
    ), None)
    if active is None:
        return False
    owned = active is current
    await websocket.send_json({
        "type": "error", "code": "repository_busy",
        "message": f"Agent 任务仍在运行，结束后才能{operation}。",
        "run_id": active.active_controller.run_id if active.active_controller else None,
        "run_owned_by_connection": owned,
        **_session_identity(current),
    })
    return True


async def _reject_shared_capability_mutation_while_running(
    websocket: WebSocket, current: DaoSession, operation: str,
) -> bool:
    """Freeze Skills and MCP for the entire duration of every active run."""
    active = next((
        item for item in manager.active_run_owners()
        if item.owner_id == current.owner_id
    ), None)
    if active is None:
        return False
    owned = active is current
    await websocket.send_json({
        "type": "error", "code": "capabilities_busy",
        "message": f"Agent 任务仍在运行，结束后才能{operation}。",
        "run_id": active.active_controller.run_id if active.active_controller else None,
        "run_owned_by_connection": owned,
        **_session_identity(current),
    })
    return True


async def _broadcast_model_repository(
    data: dict[str, Any], *, origin: DaoSession | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Publish one repository revision to every connected Desktop window.

    Repository data is shared, but session identity is connection-specific.
    Building each packet here prevents one window from adopting another
    window's active conversation while still refreshing model controls.
    """
    revision = manager.next_model_repository_revision()
    target_owner = origin.owner_id if origin else _active_owner_id.get()
    stale_runtime_ids: list[str] = []
    for connected, socket in manager.websocket_items():
        if target_owner and connected.owner_id != target_owner:
            continue
        packet: dict[str, Any] = {
            "type": "model_repository_updated", "data": data,
            "repository_revision": revision,
            "origin_runtime_session_id": origin.id if origin else None,
            **_session_identity(connected),
        }
        if connected is origin and extra:
            packet.update(extra)
        try:
            await socket.send_json(packet)
        except (RuntimeError, WebSocketDisconnect):
            stale_runtime_ids.append(connected.id)
    for runtime_id in stale_runtime_ids:
        stale = manager.get(runtime_id)
        if stale is not None:
            manager.discard(stale)


async def _broadcast_skills(
    skills: list[dict[str, str]], *, origin: DaoSession | None = None,
    extra: dict[str, Any] | None = None, include_origin: bool = True,
) -> None:
    """Publish the shared Skill inventory to every Desktop window."""
    revision = manager.next_skills_revision()
    target_owner = origin.owner_id if origin else _active_owner_id.get()
    stale_runtime_ids: list[str] = []
    for connected, socket in manager.websocket_items():
        if target_owner and connected.owner_id != target_owner:
            continue
        if connected is origin and not include_origin:
            continue
        packet: dict[str, Any] = {
            "type": "skills_updated", "skills": skills,
            "skills_revision": revision,
            "server_epoch": manager.server_epoch,
            "origin_runtime_session_id": origin.id if origin else None,
        }
        if connected is origin and extra:
            packet.update(extra)
        try:
            await socket.send_json(packet)
        except (RuntimeError, WebSocketDisconnect):
            stale_runtime_ids.append(connected.id)
    for runtime_id in stale_runtime_ids:
        stale = manager.get(runtime_id)
        if stale is not None:
            manager.discard(stale)


async def _broadcast_extensions(
    *, origin: DaoSession | None = None, extra: dict[str, Any] | None = None,
) -> None:
    """Atomically publish MCP connection state and effective extensions."""
    revision = manager.next_extensions_revision()
    target_owner = origin.owner_id if origin else _active_owner_id.get()
    servers = mcp_manager.list_configs()
    extensions = extension_registry.list_public()
    stale_runtime_ids: list[str] = []
    for connected, socket in manager.websocket_items():
        if target_owner and connected.owner_id != target_owner:
            continue
        if not _session_run_active(connected):
            connected.extensions_revision = revision
        packet: dict[str, Any] = {
            "type": "extensions_updated", "servers": servers,
            "extensions": extensions, "extensions_revision": revision,
            "server_epoch": manager.server_epoch,
            "origin_runtime_session_id": origin.id if origin else None,
        }
        if extra:
            packet.update(extra)
        try:
            await socket.send_json(packet)
        except (RuntimeError, WebSocketDisconnect):
            stale_runtime_ids.append(connected.id)
    for runtime_id in stale_runtime_ids:
        stale = manager.get(runtime_id)
        if stale is not None:
            manager.discard(stale)


async def _rebuild_idle_session_engines_for_extensions() -> list[str]:
    """Refresh the effective tool catalog after one shared MCP mutation."""
    rebuilt: list[str] = []
    owner_id = _active_owner_id.get()
    for connected in manager._sessions.values():
        if owner_id and connected.owner_id != owner_id:
            continue
        if _session_run_active(connected):
            continue
        await _rebuild_session_engine(connected)
        rebuilt.append(connected.id)
    return rebuilt


async def _handle_mcp_tools_changed(server_name: str) -> None:
    """Refresh idle Hosts now; busy Hosts refresh before their next run."""
    rebuilt = await _rebuild_idle_session_engines_for_extensions()
    await _broadcast_extensions(
        extra={
            "changed_server": server_name,
            "rebuilt_runtime_session_ids": rebuilt,
        },
    )


mcp_manager.set_tools_changed_callback(_handle_mcp_tools_changed)


def _normalized_session_ids(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


async def _require_sessions(
    websocket: WebSocket, session: DaoSession,
    session_ids: list[str], operation: str,
) -> list[dict[str, Any]] | None:
    if not session_ids:
        await websocket.send_json({
            "type": "error", "code": "invalid_session_ids",
            "message": f"至少选择一个会话才能{operation}。",
        })
        return None
    records = [
        get_session(session_id, owner_id=session.owner_id)
        for session_id in session_ids
    ]
    missing = [
        session_id for session_id, record in zip(session_ids, records)
        if record is None
    ]
    if missing:
        await websocket.send_json({
            "type": "error", "code": "session_not_found",
            "message": f"部分会话不存在，未执行{operation}。",
            "session_ids": missing,
        })
        return None
    return [record for record in records if record is not None]


def _session_catalog_cursor(value: Any) -> tuple[float, str] | None:
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid session catalog cursor")
    session_id = str(value.get("id") or "").strip()
    if not session_id:
        raise ValueError("invalid session catalog cursor")
    try:
        updated_at = float(value.get("updated_at"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid session catalog cursor") from exc
    return updated_at, session_id


def _sessions_list_packet(
    *, revision: int | None = None, request_id: str = "", query: str = "",
    include_archived: bool = False, cursor: tuple[float, str] | None = None,
    limit: int = 50, owner_id: str = "",
) -> dict[str, Any]:
    page = session_catalog_page(
        limit, include_archived=include_archived, query=query, cursor=cursor,
        owner_id=owner_id,
    )
    next_cursor = page["next_cursor"]
    return {
        "type": "sessions_list", "request_id": request_id,
        "query": query, "include_archived": include_archived,
        "sessions": page["sessions"], "total": page["total"],
        "active_total": page["active_total"],
        "archived_total": page["archived_total"],
        "has_more": page["has_more"],
        "next_cursor": (
            {"updated_at": next_cursor[0], "id": next_cursor[1]}
            if next_cursor is not None else None
        ),
        "server_epoch": manager.server_epoch,
        "desktop_protocol_version": DESKTOP_PROTOCOL_VERSION,
        "catalog_revision": (
            manager.session_catalog_revision if revision is None else revision
        ),
    }


async def _broadcast_sessions_list(
    *, origin: DaoSession | None = None,
    completed_runtime: DaoSession | None = None,
) -> None:
    """Invalidate catalogs and recover idle windows bound to stale records."""
    revision = manager.next_session_catalog_revision()
    packet = {
        "type": "sessions_changed", "server_epoch": manager.server_epoch,
        "catalog_revision": revision,
    }
    stale_runtime_ids: list[str] = []
    target_owner = origin.owner_id if origin else (
        completed_runtime.owner_id if completed_runtime else ""
    )
    for connected, socket in manager.websocket_items():
        if target_owner and connected.owner_id != target_owner:
            continue
        if connected is origin:
            continue
        # Runner packets and catalog packets share one WebSocket. Defer this
        # full snapshot until the observer's own run finishes rather than
        # introducing concurrent writers on a streaming transport.
        if _session_run_active(connected) and connected is not completed_runtime:
            continue
        try:
            bound_id = str(connected.db_id or "")
            # The sidebar snapshot is intentionally bounded, so absence from
            # ``packet["sessions"]`` is not deletion evidence. Query the bound
            # identity directly before invalidating an observer.
            bound_record = (
                get_session(bound_id, owner_id=connected.owner_id)
                if bound_id else None
            )
            if bound_id and (
                bound_record is None or bool(bound_record.get("archived"))
            ):
                # Successful delete/archive commands are already rejected while
                # this persisted conversation owns a Run.  At this point an idle
                # observer can be recovered without cancelling work or creating
                # another database row.
                archived = bound_record is not None
                reset = await _reset_to_transient_session(
                    connected, expected_db_id=bound_id, require_unavailable=True,
                )
                if not reset:
                    await socket.send_json(packet)
                    continue
                invalidation = {
                    "type": "session_archived" if archived else "session_deleted",
                    "active_reset": True,
                    "external_invalidation": True,
                    "invalidated_db_id": bound_id,
                    **_session_identity(connected),
                }
                if archived:
                    invalidation.update({"archived_db_id": bound_id, "archived": True})
                else:
                    invalidation["deleted_db_id"] = bound_id
                await socket.send_json(invalidation)
            await socket.send_json(packet)
        except (RuntimeError, WebSocketDisconnect):
            stale_runtime_ids.append(connected.id)
    for runtime_id in stale_runtime_ids:
        stale = manager.get(runtime_id)
        if stale is not None:
            manager.discard(stale)


async def _broadcast_usage() -> None:
    """Push the account usage summary to every connected window (best effort)."""
    from modus.desktop import accounts, billing

    for connected, socket in manager.websocket_items():
        owner_id = str(connected.owner_id or "")
        if not owner_id:
            owner_id = str(accounts.ensure_default_user()["user_id"])
        try:
            summary = billing.usage_summary(owner_id)
            await socket.send_json({
                "type": "usage_summary_updated", "summary": summary,
            })
        except (RuntimeError, WebSocketDisconnect):
            continue
        except Exception:
            continue


async def _rebuild_connected_hosts_for_model(model_id: str) -> list[str]:
    rebuilt: list[str] = []
    owner_id = _active_owner_id.get()
    for connected in manager._sessions.values():
        if owner_id and connected.owner_id != owner_id:
            continue
        if connected.model_id != model_id or _session_run_active(connected):
            continue
        _reset_invalid_reasoning(connected)
        await _rebuild_session_engine(connected)
        if get_session(connected.db_id, owner_id=connected.owner_id) is not None:
            update_session(
                connected.db_id,
                reasoning_effort=connected.reasoning_effort or "",
            )
        rebuilt.append(connected.id)
    return rebuilt


async def _bind_unconfigured_connected_sessions(
    repository_snapshot: dict[str, Any],
) -> list[str]:
    """Make the first repository model usable in every connected window."""
    default_id = str(
        (repository_snapshot.get("selection") or {}).get("default_model_id") or ""
    )
    if not default_id:
        return []
    rebuilt: list[str] = []
    owner_id = _active_owner_id.get()
    for connected in manager._sessions.values():
        if owner_id and connected.owner_id != owner_id:
            continue
        if connected.model_id or _session_run_active(connected):
            continue
        connected.mode = DEFAULT_MODE
        connected.mode_config = {}
        connected.model_id = default_id
        connected.reasoning_effort = None
        await _rebuild_session_engine(connected)
        if get_session(connected.db_id, owner_id=connected.owner_id) is not None:
            update_session(
                connected.db_id, mode=DEFAULT_MODE, mode_config={},
                model_id=default_id, reasoning_effort="",
            )
        rebuilt.append(connected.id)
    return rebuilt


async def _repair_connected_sessions_after_model_delete(
    deleted_id: str, repository_snapshot: dict[str, Any], *, skip_id: str = "",
) -> list[str]:
    repaired_ids: list[str] = []
    owner_id = _active_owner_id.get()
    default_id = str(
        (repository_snapshot.get("selection") or {}).get("default_model_id") or ""
    )
    for connected in manager._sessions.values():
        if owner_id and connected.owner_id != owner_id:
            continue
        if connected.id == skip_id or not _session_references_model(connected, deleted_id):
            continue
        repaired_roles = _repair_mode_snapshot_after_delete(
            connected.mode, connected.mode_config, deleted_id, repository_snapshot,
        )
        if connected.mode in COLLABORATION_MODES and _mode_roles_complete(connected.mode, repaired_roles):
            connected.mode_config = repaired_roles
            connected.model_id = _host_model_id(repaired_roles, default_id)
            _reset_invalid_reasoning(connected)
        else:
            connected.mode = DEFAULT_MODE
            connected.mode_config = {}
            connected.model_id = default_id
            connected.reasoning_effort = None
        await _rebuild_session_engine(connected)
        if get_session(connected.db_id, owner_id=connected.owner_id) is not None:
            update_session(
                connected.db_id, mode=connected.mode,
                mode_config=connected.mode_config, model_id=connected.model_id,
                reasoning_effort=connected.reasoning_effort or "",
            )
        repaired_ids.append(connected.id)
    return repaired_ids


def _repair_persisted_sessions_after_model_delete(
    deleted_id: str, repository_snapshot: dict[str, Any], *, skip_id: str = "",
) -> list[str]:
    """Repair every inactive session that still references a deleted model."""
    repaired_ids: list[str] = []
    selection = repository_snapshot.get("selection") or {}
    default_id = str(selection.get("default_model_id") or "")
    owner_id = ""
    connected_owner = next(
        (item.owner_id for item in manager._sessions.values() if item.id == skip_id),
        "",
    )
    if connected_owner:
        owner_id = connected_owner
    for summary in list_sessions(
        10_000, include_archived=True, owner_id=owner_id,
    ):
        session_id = str(summary.get("id") or "")
        if not session_id or session_id == skip_id:
            continue
        record = get_session(session_id, owner_id=owner_id)
        if record is None:
            continue
        mode = normalize_mode(record.get("mode"))
        snapshot = record.get("mode_config") if isinstance(record.get("mode_config"), dict) else {}
        references_deleted = str(record.get("model_id") or "") == deleted_id or any(
            isinstance(raw, dict) and str(raw.get("model_id") or "") == deleted_id
            for raw in snapshot.values()
        )
        if not references_deleted:
            continue
        repaired_roles = _repair_mode_snapshot_after_delete(
            mode, snapshot, deleted_id, repository_snapshot,
        )
        if mode in COLLABORATION_MODES and _mode_roles_complete(mode, repaired_roles):
            repaired_mode = mode
            repaired_model_id = _host_model_id(repaired_roles, default_id)
        else:
            repaired_mode = DEFAULT_MODE
            repaired_roles = {}
            repaired_model_id = default_id
        model = model_repository.runtime_model(repaired_model_id or None)
        allowed = {str(item) for item in (model or {}).get("reasoning_efforts", [])}
        reasoning = str(record.get("reasoning_effort") or "")
        if reasoning not in allowed:
            reasoning = ""
        update_session(
            session_id, mode=repaired_mode, mode_config=repaired_roles,
            model_id=repaired_model_id, reasoning_effort=reasoning,
        )
        repaired_ids.append(session_id)
    return repaired_ids


def _host_model_id(mode_config: dict[str, Any], fallback: str = "") -> str:
    host = mode_config.get("host") if isinstance(mode_config, dict) else None
    return str((host or {}).get("model_id") or fallback)


def _run_config_snapshot(
    session: DaoSession, controller: RunController, mode: str, *,
    verification_required: bool = False,
) -> dict[str, Any]:
    """Capture the effective, browser-safe settings before execution starts."""
    try:
        public = model_repository.public_snapshot()
    except (AttributeError, KeyError, TypeError, ValueError):
        # Lightweight repositories used by embedders may expose only read().
        # The snapshot builder has strict field allowlists, so this fallback
        # cannot copy credentials into the run ledger.
        data = model_repository.read()
        public = {
            "models": data.get("models") or data.get("all") or [],
            "selection": data.get("selection") or {},
        }
    selection = public.get("selection") or {}
    mode = normalize_mode(mode)
    roles = dict(session.mode_config or {}) if mode in COLLABORATION_MODES else {}
    if not roles and mode in COLLABORATION_MODES:
        roles = dict(selection.get(f"{mode}_roles") or {})
    host_model_id = _host_model_id(
        roles,
        session.model_id or str(selection.get("default_model_id") or ""),
    )
    host_role = roles.get("host") if isinstance(roles.get("host"), dict) else {}
    engine_llm = getattr(
        getattr(getattr(session, "engine", None), "config", None), "llm", None,
    )
    reasoning = (
        session.reasoning_effort
        or host_role.get("reasoning_effort")
        or getattr(engine_llm, "reasoning_effort", None)
    )
    return build_run_config_snapshot(
        mode=mode,
        host_model_id=host_model_id,
        reasoning_effort=str(reasoning or "") or None,
        roles=roles,
        models=public.get("models") or [],
        budget=controller.budget.snapshot(),
        verification_required=verification_required,
        has_custom_system_prompt=bool(str(session.system_prompt or "").strip()),
    )


def _persist_run_start(
    session: DaoSession, emitter: RunEventEmitter, controller: RunController,
    mode: str, *, verification_required: bool = False,
    client_request_id: str = "",
    client_request_fingerprint: str = "",
) -> dict[str, Any] | None:
    """Freeze a persisted run's configuration before its first event."""
    session.active_run_id = emitter.run_id
    if not session.db_id or get_session(
        session.db_id, owner_id=session.owner_id,
    ) is None:
        # In-memory embedders may carry a compatibility db_id without a
        # Desktop session row. ``None`` distinguishes that non-durable mode
        # from an attempted durable admission that failed and returns ``{}``.
        return None
    workspace = (
        WorkspaceIdentity.from_path(session.workspace_root)
        if session.workspace_root else None
    )
    if workspace:
        upsert_workspace(workspace, owner_id=session.owner_id)
    run = create_run_admission(
        emitter.run_id, session.db_id, mode,
        workspace_id=workspace.workspace_id if workspace else "",
        client_request_id=client_request_id,
        client_request_fingerprint=client_request_fingerprint,
        config_snapshot=_run_config_snapshot(
            session, controller, mode,
            verification_required=verification_required,
        ),
        root_title="用户任务", root_description="本次运行的根任务",
        root_actor_id="primary", root_actor_label="主持人",
        assigned_model_id=session.model_id,
    )
    if str(run.get("run_id") or "") != emitter.run_id:
        return {}
    root_task_id = f"task_{emitter.run_id}_root"
    emitter.bind_context(
        workspace_id=workspace.workspace_id if workspace else "",
        root_task_id=root_task_id,
    )
    return run


def _session_identity(session: DaoSession) -> dict[str, Any]:
    """Return the unambiguous browser-safe identity of the active conversation."""
    return {
        # ``session_id`` is retained as a compatibility alias for the runtime
        # owner. Persisted conversation references always use ``db_id``.
        "runtime_session_id": session.id,
        "session_id": session.id,
        "server_epoch": manager.server_epoch,
        "desktop_protocol_version": DESKTOP_PROTOCOL_VERSION,
        "db_id": session.db_id,
        "persisted": bool(session.db_id),
        "workspace": {
            "schema": "modus.workspace.v1",
            "workspace_id": session.workspace_id,
            "root": session.workspace_root,
            "name": session.workspace_name,
        },
        "mode": session.mode,
        "model_id": session.model_id,
        "mode_config": dict(session.mode_config),
        "reasoning_effort": session.reasoning_effort,
        "worldview": session.worldview,
        "owner_id": session.owner_id,
    }


async def _select_session_workspace(
    session: DaoSession, workspace_id: str,
) -> WorkspaceIdentity | None:
    """Bind one validated persisted workspace to the live session."""
    from modus.desktop.db import get_workspace

    target = get_workspace(
        str(workspace_id or "").strip(), owner_id=session.owner_id,
    )
    if target is None:
        return None
    workspace = WorkspaceIdentity.from_record(target)
    session.workspace_id = workspace.workspace_id
    session.workspace_root = workspace.root
    session.workspace_name = workspace.name
    await _rebuild_session_engine(session)
    return workspace


async def _persist_session_if_needed(websocket: WebSocket, session: DaoSession) -> bool:
    """Persist once and notify the browser before any run-side effects begin."""
    persisted = manager.persist_first(session)
    if persisted is None:
        return False
    await websocket.send_json({
        "type": "session_persisted",
        **_session_identity(session),
        "session": persisted,
    })
    return True


async def _handle_workbench_get(
    websocket: WebSocket, session: DaoSession, message: dict[str, Any],
) -> None:
    """Serve the task/artifact ledger independently from transcript replay."""
    request_id = str(message.get("request_id") or "")[:128]
    requested_session_id = str(message.get("session_id") or "")
    if not session.db_id or get_session(
        session.db_id, owner_id=session.owner_id,
    ) is None:
        workspace = (
            WorkspaceIdentity.from_path(session.workspace_root).to_wire()
            if session.workspace_root else {
                "schema": "modus.workspace.v1", "workspace_id": "",
                "root": "", "name": "",
            }
        )
        data = {
            "schema": "modus.workbench.v1", "session_id": "",
            "workspace": workspace, "runs": [],
        }
    else:
        data = build_workbench_snapshot(session.db_id)
    await websocket.send_json({
        "type": "workbench_snapshot", "operation": "workbench_get",
        "request_id": request_id,
        "session_id": str(data.get("session_id") or ""),
        "requested_session_id": requested_session_id,
        "data": data,
    })


command_router.register("workbench_get", _handle_workbench_get)


async def _handle_credential_migration_report(
    websocket: WebSocket, session: DaoSession, message: dict[str, Any],
) -> None:
    """Return a redacted report of models whose keys a Keychain migration would
    move.  Never includes key values; only presence and a trailing hint."""
    from modus.desktop.credential_backend import migration_report

    request_id = str(message.get("request_id") or "")[:128]
    try:
        report = migration_report(model_repository)
    except Exception as exc:
        await websocket.send_json({
            "type": "error", "code": "credential_migration_report_failed",
            "operation": "credential_migration_report", "request_id": request_id,
            "message": redact_text(str(exc)),
        })
        return
    await websocket.send_json({
        "type": "credential_migration_report",
        "operation": "credential_migration_report", "request_id": request_id,
        "report": report,
    })


command_router.register("credential_migration_report", _handle_credential_migration_report)


async def _handle_credential_migration_run(
    websocket: WebSocket, session: DaoSession, message: dict[str, Any],
) -> None:
    """Execute a confirmed Keychain migration.  This writes to the system
    credential store; the frontend must show the redacted report and obtain
    explicit user confirmation before sending this command."""
    from modus.desktop.credential_backend import migrate_credentials_to_keychain

    request_id = str(message.get("request_id") or "")[:128]
    try:
        result = migrate_credentials_to_keychain(model_repository)
    except Exception as exc:
        await websocket.send_json({
            "type": "error", "code": "credential_migration_failed",
            "operation": "credential_migration_run", "request_id": request_id,
            "message": redact_text(str(exc)),
        })
        return
    await websocket.send_json({
        "type": "credential_migration_done",
        "operation": "credential_migration_run", "request_id": request_id,
        "result": result,
    })
    await _broadcast_model_repository(model_repository.public_snapshot(), origin=session)


command_router.register("credential_migration_run", _handle_credential_migration_run)


async def _handle_workbench_run_get(
    websocket: WebSocket, session: DaoSession, message: dict[str, Any],
) -> None:
    """Return one session-scoped Run projection for explicit detail refresh."""
    request_id = str(message.get("request_id") or "")[:128]
    requested_session_id = str(message.get("session_id") or "")
    run_id = str(message.get("run_id") or "").strip()
    response_identity = {
        "operation": "workbench_run_get", "request_id": request_id,
        "session_id": str(session.db_id or ""),
        "requested_session_id": requested_session_id, "run_id": run_id,
    }
    run = get_run(run_id) if run_id else None
    if not session.db_id or run is None or str(run.get("session_id") or "") != session.db_id:
        await websocket.send_json({
            "type": "error", "code": "workbench_run_not_found",
            "message": "未找到当前会话中的这次运行。",
            **response_identity,
        })
        return
    projection = build_workbench_run(session.db_id, run_id)
    await websocket.send_json({
        "type": "workbench_run", "run": projection, **response_identity,
    })


command_router.register("workbench_run_get", _handle_workbench_run_get)


# ── Local user accounts / auth ──
from modus.desktop.auth_commands import register_auth_commands

register_auth_commands(command_router)


async def _reset_to_transient_session(
    session: DaoSession, *, expected_db_id: str | None = None,
    require_unavailable: bool = False,
) -> bool:
    """Clear one stale binding without manufacturing an empty database row.

    Build the replacement engine before committing the identity change. A
    catalog broadcast runs beside the target socket's receive loop; the final
    identity and availability checks prevent a delayed invalidation from
    clobbering a newer session switch or same-record archive restore.
    """
    model_id = session.model_id
    workspace_root = session.workspace_root
    # Always rebuild the engine (anchoring to the home directory when no
    # workspace is bound) so a stale observer is reset to a fully usable
    # transient session with the full tool catalog.
    engine = await _build_session_engine(
        model_id or None, None, None, workspace_root or str(Path.home()),
    )
    if expected_db_id is not None:
        if session.db_id != expected_db_id:
            return False
        if require_unavailable:
            current_record = get_session(
                expected_db_id, owner_id=session.owner_id,
            )
            if current_record is not None and not bool(current_record.get("archived")):
                return False
    session.db_id = ""
    session.main_history = []
    session.worldview = ""
    session.world_view_history = []
    session.system_prompt = ""
    session.mode = DEFAULT_MODE
    session.mode_config = {}
    session.reasoning_effort = None
    session.pending_session_create_key = None
    session.engine = engine
    return True


def _load_models_for_session(session: DaoSession, mode: str) -> dict[str, Any]:
    """Resolve one session's public role snapshot into runtime credentials."""
    mode = normalize_mode(mode)
    if mode not in COLLABORATION_MODES:
        raise ValueError("runtime role configuration is only valid for MOA or Peri")
    roles = model_repository.runtime_mode_configuration(mode, session.mode_config)
    return {f"{mode}_roles": roles}


def _pair_tool_call_ids(messages: list[Message]) -> None:
    """Backfill missing tool_message ``tool_call_id`` from the assistant turn.

    Tool rows persisted before the ``tool_call_id`` column existed carry no id;
    OpenAI-compatible providers reject an unanswered assistant tool_call with
    HTTP 400.  The durable log is ordered assistant(tool_calls) followed by its
    tool results (the runner writes them in that sequence), so a positional
    pairing restores the contract.  Rows that already have an id are untouched.
    """
    pending: list[str] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            pending.extend(
                str(tc.get("id") or "") for tc in message.tool_calls if tc.get("id")
            )
        elif message.role == "tool" and not message.tool_call_id:
            if pending:
                message.tool_call_id = pending.pop(0)

async def _bind_persisted_session(session: DaoSession, restored: dict[str, Any]) -> None:
    """Restore identity, model and mode from one authoritative DB record."""
    restored_owner = str(restored.get("owner_id") or "")
    if session.owner_id and restored_owner != session.owner_id:
        raise ValueError("session does not belong to the active account")
    if not session.owner_id:
        session.owner_id = restored_owner
    session.db_id = str(restored["id"])
    from modus.desktop.db import get_workspace

    workspace_record = get_workspace(
        str(restored.get("workspace_id") or ""), owner_id=session.owner_id,
    )
    if workspace_record:
        workspace = WorkspaceIdentity.from_record(workspace_record)
        session.workspace_id = workspace.workspace_id
        session.workspace_root = workspace.root
        session.workspace_name = workspace.name
    else:
        session.workspace_id = ""
        session.workspace_root = ""
        session.workspace_name = ""
    session.model_id = str(restored.get("model_id") or "")
    session.mode = normalize_mode(restored.get("mode"))
    session.mode_config = dict(restored.get("mode_config") or {})
    session.system_prompt = str(restored.get("system_prompt") or "")
    session.reasoning_effort = str(restored.get("reasoning_effort") or "") or None
    session.worldview = str(restored.get("worldview") or "")
    try:
        session.world_view_history = json.loads(restored.get("world_view_history") or "[]")
    except (TypeError, json.JSONDecodeError):
        session.world_view_history = []
    messages = (
        restored["context_messages"]
        if "context_messages" in restored
        else restored.get("messages") or []
    )
    restored_history = [
        Message(
            role=item["role"], content=item["content"],
            tool_call_id=item.get("tool_call_id") or None,
            tool_calls=list(item.get("tool_calls") or []),
        )
        for item in messages
    ]
    _pair_tool_call_ids(restored_history)
    compaction = restored.get("context_compaction")
    if isinstance(compaction, dict) and compaction:
        from modus.agent.compressor import SUMMARY_PREFIX, compress_messages

        cutoff_message_id = compaction.get("cutoff_message_id")
        if cutoff_message_id is not None:
            try:
                cutoff = int(cutoff_message_id)
            except (TypeError, ValueError):
                cutoff = -1
            head_row = (
                messages[0]
                if restored_history
                and restored_history[0].role == "system"
                and not str(restored_history[0].content or "").startswith(SUMMARY_PREFIX)
                else None
            )
            head_id = int(head_row.get("id") or 0) if head_row is not None else 0
            head = [restored_history[0]] if head_row is not None else []
            retained = [
                message for row, message in zip(messages, restored_history)
                if int(row.get("id") or 0) > cutoff
                and int(row.get("id") or 0) != head_id
            ]
            session.main_history = [
                *head,
                Message(
                    role="system",
                    content=(
                        f"{SUMMARY_PREFIX}\n\n"
                        f"{str(compaction.get('summary') or '')}"
                    ),
                ),
                *retained,
            ]
        else:
            # Compatibility for an early compaction record without a message
            # boundary.  New records always use the exact cutoff above.
            session.main_history = compress_messages(
                restored_history, summary=str(compaction.get("summary") or ""),
                tail_count=max(1, int(compaction.get("tail_count") or 1)),
            )
    else:
        session.main_history = restored_history
    await _rebuild_session_engine(session)
    _recompact_over_window(session, compaction if isinstance(compaction, dict) else None)


def _recompact_over_window(session: DaoSession, compaction: dict[str, Any] | None) -> None:
    """Compactly re-bound a restored context that would blow the model window.

    Restore is lossless by design: everything after the last compaction
    boundary is kept so the model resumes where it stopped.  The durable
    ``token_count`` (populated by ``add_message``) lets us check, against the
    model's hard context window rather than the soft compression threshold,
    whether that restored tail alone already exceeds what the next request can
    carry.  Only then do we compact *before* the request, preserving the stored
    summary that the client-side trim (which keeps head + last user turn)
    would otherwise drop.

    Deterministic only: restore must not block on a semantic LLM call.
    """
    config = getattr(getattr(session, "engine", None), "config", None)
    if config is None:
        return
    from modus.agent.compressor import compress_messages

    window = max(1_024, int(getattr(getattr(config, "llm", None), "max_context_window", 128_000)))
    budget = max(0, window - int(getattr(config.llm, "max_tokens", 8_192)))
    # The client trims toward the window at request time; reserve the summary
    # message so it survives that trim when we must compact here.
    if sum(_estimate_row_tokens(m) for m in session.main_history) <= budget:
        return
    tail_count = max(2, int(getattr(getattr(getattr(config, "features", None), "compression", None), "tail_messages", 8)))
    session.main_history = compress_messages(
        session.main_history,
        summary=str((compaction or {}).get("summary") or ""),
        tail_count=tail_count,
    )


def _estimate_row_tokens(message: Message) -> int:
    """Rough chars/4 estimate for one in-memory message (mirrors DB estimate)."""
    content = message.content
    total = len(content) if isinstance(content, str) else len(str(content))
    for tc in message.tool_calls or []:
        total += len(json.dumps(tc))
    return max(1, total // 4)


def _ensure_default_models() -> None:
    """Initialize only from an explicit environment credential on an empty repo."""
    data = model_repository.read()
    if data["models"]:
        return
    import os

    key = os.environ.get("MODUS_API_KEY", "")
    if key:
        model_repository.create(
            name="DeepSeek", provider="deepseek", model="deepseek-v4-flash", api_key=key,
        )


_ensure_default_models()


class ApprovalE2EFixtureEngine:
    """Explicit opt-in engine for browser approval E2E verification only.

    It intentionally bypasses an LLM provider but not the execution boundary:
    the same ToolExecutor, approval callback, PathGuard-backed builtin write_file,
    event stream, WebSocket broker, and browser card are exercised.  It is never
    selected from a client message and requires both a fixed mode value and an
    existing workspace path supplied by the process environment.
    """

    def __init__(self, *, config: Any, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace
        self.registry = ToolRegistry()
        from modus.tools.builtins import write_file

        self.registry.register(
            Tool(
                name="write_file",
                description="Write a UTF-8 text file inside the controlled approval E2E workspace.",
                parameters=object_schema(
                    {"path": {"type": "string"}, "content": {"type": "string"}},
                    ["path", "content"],
                ),
                required_keys=["path", "content"],
                handler=write_file,
                is_read_only=False,
                danger_level="high",
                requires_approval=True,
            )
        )

    async def ask(
        self,
        message: str,
        history: list[Message] | None = None,
        *,
        approval_callback: Any = None,
        cancel_event: asyncio.Event | None = None,
        budget: Any = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ):
        if cancel_event is not None and cancel_event.is_set():
            yield {"type": "error", "error": "Run cancelled before approval fixture started."}
            return
        payload = {
            "path": "approval-proof.txt",
            "content": f"Approved browser E2E write: {message}",
        }
        call = {
            "id": "approval_e2e_write_1",
            "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps(payload, ensure_ascii=False)},
        }
        yield {"type": "tool_call", "name": "write_file", "input": payload}
        executor = ToolExecutor(self.registry)
        results = await executor.execute_all(
            [call],
            ToolContext(
                cwd=str(self.workspace), config=self.config,
                approval_callback=approval_callback, cancel_event=cancel_event,
            ),
        )
        for result in results:
            event: dict[str, Any] = {"type": "tool_result", "name": "write_file"}
            event.update(tool_result_event(result))
            yield event
        if results and results[0].is_error:
            yield {"type": "error", "error": results[0].model_text()}
            return
        yield {"type": "text_delta", "text": "受控审批写入已完成。"}
        yield {
            "type": "done", "messages": list(history or []) + [Message(role="user", content=message)],
            "total_tokens": 0, "total_turns": 1,
        }


def _approval_e2e_workspace() -> Path | None:
    """Return a controlled test workspace only for an explicit process switch."""
    import os

    if os.environ.get("MODUS_DESKTOP_TEST_MODE") != "approval_write":
        return None
    raw_workspace = os.environ.get("MODUS_DESKTOP_TEST_WORKSPACE", "")
    if not raw_workspace:
        raise RuntimeError("approval E2E test mode requires MODUS_DESKTOP_TEST_WORKSPACE")
    workspace = Path(raw_workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise RuntimeError("approval E2E test workspace must already exist")
    logger.warning("TEST MODE ACTIVE — approval writes may succeed in controlled workspace: %s", workspace)
    return workspace


def start_server(
    *, host: str = "127.0.0.1", port: int = 3000,
    workspace_root: str | Path | None = None,
) -> None:
    """Start the Modus Desktop WebSocket and static-file service."""
    global _desktop_default_workspace_root
    _desktop_default_workspace_root = (
        WorkspaceIdentity.from_path(workspace_root).root
        if workspace_root is not None else None
    )
    logger.info("Modus Desktop starting on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")

def _attached_skill_message(skill_id: str) -> Message | None:
    """Resolve a user-attached skill into a deliberate system context message.

    Mirrors the ``load_skill`` tool source so the UI attachment and the tool
    read the same repository. Returns None when the skill is unknown or empty,
    rather than failing the run.
    """
    if not skill_id:
        return None
    try:
        skill = skill_repository.get(str(skill_id))
    except ValueError:
        return None
    if skill is None or not skill.prompt.strip():
        return None
    return Message(
        role="system",
        content=(
            f"[Attached skill: {skill.name} — follow these instructions for this run]\n\n"
            f"{skill.prompt.strip()}"
        ),
    )


MAX_ATTACHED_CONTEXT = 8
# Client-preloaded file contents are capped per file so one attachment cannot
# blow the context window (defensive; the client truncates too).
MAX_ATTACHED_FILE_CHARS = 200_000
MAX_ATTACHED_THUMB_CHARS = 1_500_000


def _clean_attached_context(raw: Any) -> list[dict[str, str]]:
    """Validate/limit the attached-context array from a run_message.

    Unknown fields on run_message are ignored by admission, so a context
    payload must be sanitised here before it can reach a runner: cap the count,
    whitelist kinds, bound label/value lengths, require an http(s) scheme for
    URLs, and dedupe. Invalid entries are dropped silently rather than failing
    the run.
    """
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ATTACHED_CONTEXT:
        return []
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in ("project", "file", "url", "folder", "image"):
            continue
        label = str(item.get("label") or "")[:200].strip()
        value = str(item.get("value") or "")[:2000].strip()
        if not label or not value:
            continue
        if kind == "url" and not value.lower().startswith(("http://", "https://")):
            continue
        key = (kind, value)
        if key in seen:
            continue
        seen.add(key)
        out: dict[str, str] = {"kind": kind, "label": label, "value": value}
        content = item.get("content")
        if kind == "file" and isinstance(content, str) and content.strip():
            out["content"] = content[:MAX_ATTACHED_FILE_CHARS]
        thumb = item.get("thumb")
        if (
            kind == "image" and isinstance(thumb, str)
            and thumb.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,"))
            and len(thumb) <= MAX_ATTACHED_THUMB_CHARS
        ):
            out["thumb"] = thumb
        cleaned.append(out)
    return cleaned


def _attached_context_messages(context: list[dict[str, str]]) -> list[Message]:
    """Turn cleaned attached context into deliberate system prompts.

    The Agent decides how to use each attachment — this deliberately does not
    switch the session workspace or fetch URLs. For client-picked files the
    contents are inlined (pre-loaded by the browser), so the Agent works
    directly from the real body; for manually-pasted paths the file is not
    pre-loaded and the Agent uses its own read_file tool. The path/URL is
    always advisory; the Agent reasons from the user's prompt.
    """
    out: list[Message] = []
    for item in context:
        kind, label, value = item["kind"], item["label"], item["value"]
        if kind == "project":
            text = (
                f"[Attached context — project]\n"
                f'The user attached a project: "{label}" (root: {value}). '
                f"Treat this directory as the working project for this run. "
                f"Explore and act inside it with your own file and shell tools; "
                f"use the absolute path where a tool requires one. "
                f"If the path is relative, locate it first."
            )
        elif kind == "file":
            content = item.get("content") or ""
            if content:
                text = (
                    f"[Attached context — file]\n"
                    f'The user attached a file: "{label}" ({value}). '
                    f"Its contents are inlined below (pre-loaded by the client). "
                    f"Work directly from them; do not ask the user to paste or "
                    f"describe the file."
                )
                out.append(Message(role="system", content=text))
                out.append(Message(role="user", content=content))
                continue
            text = (
                f"[Attached context — file]\n"
                f'The user attached a file: "{label}" ({value}). '
                f"Read it yourself with the read_file tool before working on it. "
                f"If the path is not absolute, locate it within the attached "
                f"project or working directory. Decide how to use it; its "
                f"contents were not pre-loaded."
            )
        elif kind == "folder":
            text = (
                f"[Attached context — folder]\n"
                f'The user attached a folder: "{label}" ({value}). '
                f"Treat it as a set of source files for this run. Explore it with "
                f"your own file and shell tools; read the relevant files before "
                f"working on them. If the path is not absolute, locate it within "
                f"the working directory."
            )
        elif kind == "image":
            text = (
                f"[Attached context — image]\n"
                f'The user attached an image: "{label}" ({value}). '
                f"The attachment is displayed with the user request. Use it as "
                f"visual context when the selected model supports image input; "
                f"otherwise explain that the image cannot be inspected."
            )
        else:  # url
            text = (
                f"[Attached context — url]\n"
                f'The user attached a URL: "{label}" ({value}). '
                f"Fetch it yourself with the web_fetch tool if relevant. "
                f"Decide whether and how to use it; its content was not pre-loaded."
            )
        out.append(Message(role="system", content=text))
    return out


async def _run_moa_session(
    websocket: WebSocket, session: DaoSession, content: str, skill_id: str = "",
    *, emitter: RunEventEmitter | None = None,
    controller: RunController | None = None, persisted_run: bool = False,
    context_messages: list[Message] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Own the complete MOA pipeline under one controller and background task."""
    emitter = emitter or RunEventEmitter(
        run_id=f"run_{uuid.uuid4().hex}", mode="moa", send_json=websocket.send_json,
        audit_event=_audit_event_for(session),
    )
    engine_config = getattr(getattr(session, "engine", None), "config", None)
    if controller is None:
        controller = RunController.from_config(
            run_id=emitter.run_id, mode="moa", config=engine_config,
        ) if engine_config is not None else RunController(run_id=emitter.run_id, mode="moa")
    if controller.state is RunState.CREATED:
        controller.transition(RunState.RUNNING)
    if not persisted_run:
        persisted = _persist_run_start(session, emitter, controller, "moa")
        if session.db_id and (
            str((persisted or {}).get("run_id") or "") != emitter.run_id
            or not emitter.root_task_id
        ):
            if not controller.is_terminal:
                controller.transition(RunState.FAILED)
            raise RuntimeError("Run admission could not persist its root task")
    session.active_controller = controller
    completed_normally = False
    guidance_message: Message | None = None
    try:
        await emitter.emit(
            EventType.RUN_STARTED, ChannelId.USER_HOST, Actor.system(),
            {"state": "running", "mode": "moa", "budget": controller.budget.snapshot()},
            status=EventStatus.STARTED,
        )
        await emitter.emit(
            EventType.USER_MESSAGE, ChannelId.USER_HOST, Actor.user(),
            {"markdown": content, **({"attachments": attachments} if attachments else {})},
        )
        from modus.agent.context import SessionContextProvider

        context_provider = SessionContextProvider()
        recent = list(context_provider.effective_history(session))[-20:]
        skill_message = _attached_skill_message(skill_id)
        if skill_message is not None:
            recent.append(skill_message)
        if context_messages:
            recent.extend(context_messages)
        recent.append(Message(role="user", content=content))
        guidance = await _run_moa_stream(
            websocket, session, recent, emitter=emitter, controller=controller,
        ) if not controller.cancel_event.is_set() else ""
        if controller.cancel_event.is_set():
            if session.db_id:
                add_message(session.db_id, "user", content, token_count=0)
            session.main_history.append(Message(role="user", content=content))
            controller.budget.finish(StopReason.CANCELLED)
            try:
                await emitter.emit(
                    EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
                    {
                        "code": "cancelled", "message": "用户中断", "retryable": False,
                        "stop_reason": "cancelled", "budget": controller.budget.snapshot(),
                    },
                    status=EventStatus.CANCELLED,
                )
                await websocket.send_json({
                    "type": "done", "stop_reason": "cancelled",
                    "budget": controller.budget.snapshot(),
                    "total_tokens": controller.budget.total_tokens,
                    "total_turns": controller.budget.turns,
                })
            except Exception:
                logger.debug("MOA cancellation event could not be delivered", exc_info=True)
            return
        if controller.is_terminal:
            await websocket.send_json({
                    "type": "done", "stop_reason": str(controller.budget.stop_reason or "failed"),
                    "budget": controller.budget.snapshot(),
                "total_tokens": controller.budget.total_tokens,
                "total_turns": controller.budget.turns,
            })
            return
        if guidance:
            guidance_message = Message(
                role="system",
                content=(
                    "[Mixture of Agents context — use this as private guidance. "
                    "Integrate useful insights, but own the response and every tool action.]\n\n"
                    f"{guidance}"
                ),
            )
        await _stream_to_ws(
            websocket, session, content, mode="moa", emitter=emitter,
            controller=controller, emit_user_message=False,
            transient_context=[guidance_message] if guidance_message is not None else None,
            broadcast_catalog=False, persisted_run=True,
        )
        completed_normally = controller.budget.stop_reason is StopReason.COMPLETED
    except WebSocketDisconnect as exc:
        parked = session.handle_disconnect(emitter, controller)
        if parked:
            logger.info("MOA run parked on WebSocket close")
        else:
            logger.warning("MOA run interrupted by WebSocket close")
        if not parked and not controller.is_terminal:
            controller.budget.finish(StopReason.CANCELLED)
            try:
                await emitter.emit(
                    EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
                    {
                        "code": "transport_disconnected",
                        "message": "连接已断开，运行已取消。",
                        "retryable": True, "stop_reason": "cancelled",
                        "disconnect_reason": str(exc) or "transport_disconnected",
                        "budget": controller.budget.snapshot(),
                    },
                    status=EventStatus.CANCELLED,
                )
            except Exception:
                logger.debug("MOA disconnect terminal could not be delivered", exc_info=True)
    except Exception as exc:
        if not controller.is_terminal:
            controller.transition(RunState.FAILED)
        controller.budget.finish(StopReason.FAILED)
        try:
            await emitter.emit(
                EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
                {
                    "code": "moa_failed", "message": str(exc), "retryable": True,
                    "stop_reason": "failed", "budget": controller.budget.snapshot(),
                },
                status=EventStatus.FAILED,
            )
            await websocket.send_json({
                "type": "done", "stop_reason": "failed",
                "budget": controller.budget.snapshot(),
                "total_tokens": controller.budget.total_tokens,
                "total_turns": controller.budget.turns,
            })
        except Exception:
            logger.debug("MOA failure terminal could not be delivered", exc_info=True)
    finally:
        # Aggregator guidance is a run-scoped advisory artifact, never durable
        # conversation history or an active instruction on the next user turn.
        if guidance_message is not None:
            prefix = "[Mixture of Agents context — use this as private guidance."
            session.main_history = [
                item for item in session.main_history
                if not (item.role == "system" and isinstance(item.content, str) and item.content.startswith(prefix))
            ]
        if controller.cancel_event.is_set():
            controller.cancel_complete()
        elif completed_normally and not controller.is_terminal:
            controller.transition(RunState.COMPLETED)
        else:
            if not controller.is_terminal:
                controller.transition(RunState.FAILED)
        terminal_event = emitter.terminal_event
        if session.db_id and terminal_event is not None:
            try:
                # Normally the audit callback already committed this envelope.
                # Reusing the exact same event is an idempotent atomic fallback
                # for embedders or a transport failure with a disabled audit.
                settle_run_event(session.db_id, terminal_event.to_wire())
            except Exception:
                logger.exception("MOA terminal fallback could not be persisted")
        if session.active_controller is controller:
            session.active_controller = None
        await _broadcast_sessions_list(completed_runtime=session)
        await _broadcast_usage()


async def _run_peri_session(
    websocket: WebSocket, session: DaoSession, content: str, skill_id: str = "",
    *, emitter: RunEventEmitter | None = None,
    controller: RunController | None = None, persisted_run: bool = False,
    context_messages: list[Message] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Run Peri in the default Agent's non-blocking session task contract."""
    resolved_emitter = emitter
    try:
        resolved_emitter = await _run_peri_stream(
            websocket, session, content, emitter=emitter, controller=controller,
            skill_message=_attached_skill_message(skill_id),
            context_messages=context_messages,
            persisted_run=persisted_run, manage_controller=True if persisted_run else None,
            attachments=attachments,
        )
    except WebSocketDisconnect:
        parked = session.handle_disconnect(resolved_emitter, session.active_controller)
        if parked:
            logger.info("Peri run parked on WebSocket close")
        else:
            logger.warning("Peri run interrupted by WebSocket close")
    except Exception:
        logger.exception("Peri run failed")
        try:
            controller = session.active_controller
            budget = controller.budget.snapshot() if controller is not None else {}
            await websocket.send_json({
                "type": "done", "stop_reason": "failed", "budget": budget,
                "total_tokens": budget.get("total_tokens", 0),
                "total_turns": budget.get("turns", 0),
            })
        except Exception:
            logger.debug("Peri failure terminal could not be delivered", exc_info=True)
    finally:
        terminal_event = (
            resolved_emitter.terminal_event
            if resolved_emitter is not None else None
        )
        if session.db_id and terminal_event is not None:
            try:
                settle_run_event(session.db_id, terminal_event.to_wire())
            except Exception:
                logger.exception("Peri terminal fallback could not be persisted")
        await _broadcast_sessions_list(completed_runtime=session)
        await _broadcast_usage()
def _audit_event_for(session: DaoSession):
    """Return a no-op-safe audit sink bound to the persisted session identity."""
    bound_session_id = session.db_id

    def audit_event(event: dict[str, Any]) -> bool:
        if not bound_session_id or get_session(
            bound_session_id, owner_id=session.owner_id,
        ) is None:
            # In-memory embedders and unit fixtures may deliberately use no
            # persisted conversation.  There is no durable Run to settle in
            # that case, so transport remains valid.  Persisted Desktop runs
            # always pass through ``_persist_run_start`` first.
            return True
        try:
            create_run(str(event["run_id"]), bound_session_id, normalize_mode(event.get("mode")))
            event_type = str(event.get("type") or "")
            if event_type in {"run_completed", "run_error"}:
                claimed = settle_run_event(bound_session_id, event)
                if not claimed:
                    return False
            else:
                upsert_run_event(bound_session_id, event)
            if event_type in {
                "run_started", "subtask_assignment", "reference_started",
                "reference_response", "subagent_progress", "subagent_response",
                "tool_result", "subagent_tool_result", "artifact",
                "run_completed", "run_error",
            }:
                projection = build_workbench_run(
                    bound_session_id, str(event["run_id"]),
                )
                if projection is not None:
                    event["workbench"] = projection
            return True
        except Exception:
            if str(event.get("type") or "") in {"run_completed", "run_error"}:
                # Terminal persistence is the ownership boundary.  Never tell
                # the browser a Run finished while SQLite still says running.
                logger.exception("terminal run settlement failed")
                raise
            # Non-terminal audit remains best effort so a stale/deleted session
            # cannot break an otherwise useful live stream.
            logger.exception("run event audit failed; continuing stream")
            return True
    return audit_event


def _session_run_active(session: DaoSession) -> bool:
    # A completed worker whose durable release guard failed intentionally stays
    # installed as an admission barrier until process-start ledger repair.
    return session.active_run_task is not None


def _run_release_guard(session: DaoSession):
    """Release an owner only after the complete terminal ledger fact exists.

    A terminal ``runs`` row alone is insufficient: fallback cleanup used to be
    able to update it independently while leaving the root task running or the
    transcript without a terminal Agent event.  Normal provider execution must
    therefore agree across the Run, its canonical root task, and exactly one
    audited terminal event.  Admission failures are the sole exception because
    the provider never started and intentionally emitted no Agent event.
    """
    bound_session_id = str(session.db_id or "")

    def release_guard(run_id: str) -> bool:
        durable_run_id = str(run_id or "")
        if not bound_session_id or not durable_run_id:
            return False
        try:
            run = get_run(durable_run_id)
            root = get_run_task(f"task_{durable_run_id}_root")
            events = get_run_events_since(durable_run_id, 0)
        except Exception:
            logger.exception(
                "durable Run settlement could not be read before ownership release: run=%s session=%s",
                durable_run_id, bound_session_id,
            )
            return False
        if (
            run is None
            or str(run.get("run_id") or "") != durable_run_id
            or str(run.get("session_id") or "") != bound_session_id
        ):
            return False
        run_state = str(run.get("state") or "")
        if run_state not in TERMINAL_RUN_STATES:
            return False

        stop_reason = str(run.get("stop_reason") or "")
        admission_failure = (
            run_state == "failed"
            and stop_reason in ADMISSION_FAILURE_STOP_REASONS
        )
        if root is None:
            # A root-creation failure is rejected before an owner is installed.
            # If an owned task ever reaches this guard without a root, retain
            # the barrier for repair instead of treating the partial admission
            # record as a complete settlement.
            return False
        if (
            str(root.get("task_id") or "") != f"task_{durable_run_id}_root"
            or str(root.get("run_id") or "") != durable_run_id
            or str(root.get("session_id") or "") != bound_session_id
            or str(root.get("task_kind") or "") != "root"
        ):
            return False
        root_state = str(root.get("status") or "")
        if admission_failure:
            # Provider execution never started, so any Agent event is evidence
            # that this was not actually an admission-only failure.
            return root_state == "failed" and not events

        expected_root_state = {
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            # Process-restart repair represents interruption with the terminal
            # task/event status available in their schemas.
            "interrupted": "cancelled",
        }.get(run_state)
        if root_state != expected_root_state:
            return False

        terminal_events = [
            event for event in events
            if str(event.get("type") or "") in {"run_completed", "run_error"}
        ]
        if len(terminal_events) != 1:
            return False
        terminal = terminal_events[0]
        if (
            str(terminal.get("run_id") or "") != durable_run_id
            or str(terminal.get("session_id") or "") != bound_session_id
            or str(terminal.get("task_id") or "") != str(root.get("task_id") or "")
        ):
            return False
        event_type = str(terminal.get("type") or "")
        event_status = str(terminal.get("status") or "")
        payload = terminal.get("payload")
        if not isinstance(payload, dict):
            return False
        terminal_reason = str(
            payload.get("stop_reason")
            or payload.get("code")
            or ("completed" if event_type == "run_completed" else "failed")
        )
        if terminal_reason != stop_reason:
            return False
        try:
            terminal_sequence = int(terminal.get("sequence") or 0)
            if terminal_sequence < 1 or any(
                int(event.get("sequence") or 0) >= terminal_sequence
                for event in events
                if str(event.get("event_id") or "")
                != str(terminal.get("event_id") or "")
            ):
                return False
        except (TypeError, ValueError):
            return False
        if run_state == "completed":
            return (
                stop_reason == "completed"
                and event_type == "run_completed"
                and event_status == "completed"
            )
        if event_type != "run_error":
            return False
        if run_state == "cancelled":
            return event_status == "cancelled"
        if run_state == "failed":
            return event_status == "failed"
        return (
            run_state == "interrupted"
            and stop_reason == "process_restart"
            and event_status == "cancelled"
        )

    return release_guard


def _run_submission_fingerprint(
    *, content: str, skill_id: str, requested_db_id: str,
    context: list[dict[str, str]] | None = None,
) -> str:
    """Hash retry-relevant input without persisting the user's prompt text."""
    canon_context: list[dict[str, str]] = []
    if context:
        seen: set[tuple[str, str]] = set()
        for item in sorted(context, key=lambda i: (i.get("kind", ""), i.get("value", ""))):
            key = (str(item.get("kind") or ""), str(item.get("value") or ""))
            if key in seen:
                continue
            seen.add(key)
            canon: dict[str, str] = {"kind": key[0], "value": key[1]}
            content = item.get("content")
            if isinstance(content, str) and content:
                canon["content"] = content
            canon_context.append(canon)
    canonical = json.dumps(
        {
            "content": content,
            "skill_id": skill_id,
            "requested_db_id": requested_db_id,
            "context": canon_context,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run_submission_context(
    session: DaoSession, *, request_id: str, requested_db_id: str,
) -> dict[str, Any]:
    return {
        "operation": "run_message",
        "request_id": request_id,
        "requested_db_id": requested_db_id,
        **_session_identity(session),
    }


def _correlated_run_message(
    session: DaoSession, message: dict[str, Any],
) -> dict[str, Any]:
    """Give compatibility submissions the same identity-bearing contract.

    Modern clients supply identities that the admission handler validates.
    Older Desktop clients sent only ``content`` (and optionally ``skill_id``),
    so their identity is the WebSocket-bound session itself.  Allocate a
    one-shot server request ID and materialize that authoritative identity;
    the request can then use the exact same durable admission path without a
    second runner implementation.
    """
    if "request_id" in message:
        return message
    return {
        **message,
        "request_id": f"server-run-{uuid.uuid4().hex}",
        "db_id": str(session.db_id or ""),
        "session_id": str(session.db_id or ""),
        "runtime_session_id": session.id,
    }


def _run_resources(
    websocket: WebSocket, session: DaoSession, mode: str,
) -> tuple[RunEventEmitter, RunController]:
    """Allocate the identities every runner must consume after admission."""
    run_id = f"run_{uuid.uuid4().hex}"
    emitter = RunEventEmitter(
        run_id=run_id, mode=mode, send_json=websocket.send_json,
        audit_event=_audit_event_for(session),
    )
    engine_config = getattr(getattr(session, "engine", None), "config", None)
    controller = RunController.from_config(
        run_id=run_id, mode=mode, config=engine_config,
    ) if engine_config is not None else RunController(run_id=run_id, mode=mode)
    return emitter, controller


def _canonical_run_admission(
    session: DaoSession, emitter: RunEventEmitter, controller: RunController,
    run: dict[str, Any],
) -> bool:
    """Verify the durable Run/root identity before provider execution."""
    run_id = emitter.run_id
    root_task_id = f"task_{run_id}_root"
    if (
        not session.db_id
        or controller.run_id != run_id
        or emitter.root_task_id != root_task_id
        or str(run.get("run_id") or "") != run_id
    ):
        return False
    try:
        durable_run = get_run(run_id)
        root = get_run_task(root_task_id)
    except Exception:
        logger.exception(
            "canonical Run/root could not be read before admission: run=%s",
            run_id,
        )
        return False
    return bool(
        durable_run is not None
        and str(durable_run.get("run_id") or "") == run_id
        and str(durable_run.get("session_id") or "") == session.db_id
        and str(durable_run.get("state") or "") == "running"
        and normalize_mode(durable_run.get("mode")) == normalize_mode(emitter.mode)
        and root is not None
        and str(root.get("task_id") or "") == root_task_id
        and str(root.get("run_id") or "") == run_id
        and str(root.get("session_id") or "") == session.db_id
        and str(root.get("task_kind") or "") == "root"
        and str(root.get("status") or "") == "running"
    )


def _fail_run_admission_checked(
    run_id: str, *, session_id: str, stop_reason: str,
) -> bool:
    """Attempt admission failure and confirm the resulting durable boundary.

    SQLite failure handling is itself fallible.  Callers use the false result
    to retain an in-memory owner, ensuring another provider cannot start over
    a Run whose admission state could not be made terminal.
    """
    try:
        before = get_run(run_id)
        if (
            before is None
            or str(before.get("run_id") or "") != run_id
            or str(before.get("session_id") or "") != session_id
        ):
            # A UUID collision must never let this admission fail a Run owned
            # by another conversation.
            return False
        fail_run_admission(
            run_id, stop_reason=stop_reason,
            expected_session_id=session_id,
        )
        run = get_run(run_id)
        root = get_run_task(f"task_{run_id}_root")
    except Exception:
        logger.exception(
            "Run admission failure could not be durably verified: run=%s",
            run_id,
        )
        return False
    if (
        run is None
        or str(run.get("session_id") or "") != session_id
        or str(run.get("state") or "") != "failed"
        or str(run.get("stop_reason") or "") != stop_reason
    ):
        return False
    # Root creation may be the operation that failed.  When a root exists,
    # however, it must agree with the admission-only failed Run.
    return root is None or (
        str(root.get("run_id") or "") == run_id
        and str(root.get("task_kind") or "") == "root"
        and str(root.get("status") or "") == "failed"
    )


def _retain_admission_recovery_barrier(
    websocket: WebSocket, session: DaoSession,
    emitter: RunEventEmitter, controller: RunController,
) -> None:
    """Install a completed, fail-closed owner for an indeterminate ledger."""
    if session.active_run_task is not None or active_run_owner(session.db_id) is not None:
        return

    async def blocked_admission() -> RunEventEmitter:
        return emitter

    session.active_controller = controller
    session.active_run_id = emitter.run_id
    start_session_run(
        session, blocked_admission(),
        release_guard=_run_release_guard(session),
        on_settled=_run_settlement_callback(websocket, session),
    )


def _admission_failure_requires_barrier(run_id: str, session_id: str) -> bool:
    """Conservatively identify a possibly non-terminal Run owned here."""
    try:
        run = get_run(run_id)
    except Exception:
        return True
    return bool(
        run is not None
        and str(run.get("session_id") or "") == session_id
        and str(run.get("state") or "") not in TERMINAL_RUN_STATES
    )


def _unrecovered_admission_run(session: DaoSession) -> dict[str, Any] | None:
    """Return a durable orphan that must block another provider admission."""
    if not session.db_id:
        return None
    try:
        run = latest_run_for_session(session.db_id)
    except Exception:
        # Read failures are handled later by canonical persistence checks; an
        # in-memory owner remains the primary barrier for live execution.
        return None
    if run is None or str(run.get("state") or "") in TERMINAL_RUN_STATES:
        return None
    return run


async def _run_preallocated_submission(
    websocket: WebSocket, session: DaoSession, content: str, skill_id: str,
    *, mode: str, emitter: RunEventEmitter, controller: RunController,
    context_messages: list[Message] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> Any:
    if mode == MOA_MODE:
        if context_messages:
            return await _run_moa_session(
                websocket, session, content, skill_id,
                emitter=emitter, controller=controller, persisted_run=True,
                context_messages=context_messages,
                attachments=attachments,
            )
        return await _run_moa_session(
            websocket, session, content, skill_id,
            emitter=emitter, controller=controller, persisted_run=True,
            attachments=attachments,
        )
    if mode == PERI_MODE:
        if context_messages:
            return await _run_peri_session(
                websocket, session, content, skill_id,
                emitter=emitter, controller=controller, persisted_run=True,
                context_messages=context_messages,
                attachments=attachments,
            )
        return await _run_peri_session(
            websocket, session, content, skill_id,
            emitter=emitter, controller=controller, persisted_run=True,
            attachments=attachments,
        )
    skill_message = _attached_skill_message(skill_id)
    transient = []
    if skill_message is not None:
        transient.append(skill_message)
    if context_messages:
        transient.extend(context_messages)
    return await _stream_to_ws(
        websocket, session, content, mode=DEFAULT_MODE,
        emitter=emitter, controller=controller,
        transient_context=transient or None,
        persisted_run=True, manage_controller=True,
        attachments=attachments,
    )


async def _send_duplicate_run_admission(
    websocket: WebSocket, session: DaoSession, run: dict[str, Any], *,
    request_id: str, requested_db_id: str,
) -> None:
    owner = active_run_owner(str(run.get("session_id") or ""))
    state = str(run.get("state") or "running")
    await websocket.send_json({
        "type": "run_accepted",
        **_run_submission_context(
            session, request_id=request_id, requested_db_id=requested_db_id,
        ),
        "run_id": str(run.get("run_id") or ""),
        "duplicate": True,
        "state": state,
        "status": state,
        "stop_reason": run.get("stop_reason"),
        "owned": owner is not None,
        "run_owned_by_connection": owner is session,
    })


async def _handle_explicit_run_message(
    websocket: WebSocket, session: DaoSession, message: dict[str, Any],
) -> bool:
    """Admit one modern or compatibility submission before provider code."""
    message = _correlated_run_message(session, message)
    raw_request_id = message.get("request_id")
    request_id = raw_request_id if isinstance(raw_request_id, str) else ""
    requested_db_id = str((
        message.get("db_id")
        if "db_id" in message
        else message.get("session_id") or ""
    ) or "")
    context = _run_submission_context(
        session, request_id=request_id[:128], requested_db_id=requested_db_id,
    )
    if not request_id.strip() or len(request_id) > 128:
        await websocket.send_json({
            "type": "error", "code": "invalid_request_id",
            "message": "任务请求 ID 必须是 1 到 128 个字符。", **context,
        })
        return True
    if "db_id" in message and "session_id" in message:
        alias = str(message.get("session_id") or "")
        if alias != requested_db_id:
            await websocket.send_json({
                "type": "error", "code": "session_mismatch",
                "message": "任务请求的会话标识不一致。", **context,
            })
            return True
    requested_runtime_id = str(message.get("runtime_session_id") or "")
    if requested_runtime_id != session.id:
        await websocket.send_json({
            "type": "error", "code": "runtime_session_mismatch",
            "message": "任务请求不属于当前连接。", **context,
        })
        return True
    raw_content = message.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    skill_id = str(message.get("skill_id") or "")
    attached_context = _clean_attached_context(message.get("context"))
    context_messages = _attached_context_messages(attached_context)
    if not content.strip():
        await websocket.send_json({
            "type": "error", "code": "invalid_run_message",
            "message": "任务内容不能为空。", **context,
        })
        return True
    fingerprint = _run_submission_fingerprint(
        content=content, skill_id=skill_id, requested_db_id=requested_db_id,
        context=attached_context,
    )

    # Local billing gate: surface an explicit insufficient-balance error before
    # the admission machinery builds a run, so the user gets a clear message.
    try:
        from modus.config import load_config
        from modus.desktop import billing

        if load_config().features.billing:
            from modus.desktop import accounts as _accounts

            owner_id = str(session.owner_id or "") or str(
                _accounts.ensure_default_user()["user_id"]
            )
            if not billing.sufficient_balance(owner_id):
                await websocket.send_json({
                    "type": "error", "code": "insufficient_balance",
                    "message": "账户余额不足，请先在「设置 → 账户」中充值。", **context,
                })
                return True
    except Exception:
        pass

    async with manager.run_admission_lock:
        existing = get_run_by_client_request_id(
            request_id, owner_id=session.owner_id,
        )
        if existing is not None:
            if (
                str(existing.get("client_request_fingerprint") or "") != fingerprint
                or (
                    requested_db_id
                    and requested_db_id != str(existing.get("session_id") or "")
                )
            ):
                await websocket.send_json({
                    "type": "error", "code": "request_id_conflict",
                    "message": "该任务请求 ID 已用于不同的任务或会话。",
                    "run_id": str(existing.get("run_id") or ""), **context,
                })
                return True
            existing_session_id = str(existing.get("session_id") or "")
            if session.db_id and session.db_id != existing_session_id:
                await websocket.send_json({
                    "type": "error", "code": "request_id_conflict",
                    "message": "该任务请求属于另一个会话。",
                    "run_id": str(existing.get("run_id") or ""), **context,
                })
                return True
            if not session.db_id:
                restored = restore_session(
                    existing_session_id, owner_id=session.owner_id,
                )
                if restored is None:
                    await websocket.send_json({
                        "type": "error", "code": "session_not_found",
                        "message": "已接收任务所属的会话已不存在。", **context,
                    })
                    return True
                await _bind_persisted_session(session, restored)
                await websocket.send_json({
                    "type": "session_persisted", **_session_identity(session),
                    "session": get_session(
                        existing_session_id, owner_id=session.owner_id,
                    ) or {},
                })
            if (
                str(existing.get("state") or "") == "failed"
                and str(existing.get("stop_reason") or "")
                in ADMISSION_FAILURE_STOP_REASONS
            ):
                await websocket.send_json({
                    "type": "error", "code": "run_admission_failed",
                    "message": "任务接纳失败，请使用新的请求 ID 重试。",
                    "run_id": str(existing.get("run_id") or ""),
                    **_run_submission_context(
                        session, request_id=request_id,
                        requested_db_id=requested_db_id,
                    ),
                })
                return True
            await _send_duplicate_run_admission(
                websocket, session, existing,
                request_id=request_id, requested_db_id=requested_db_id,
            )
            return True

        if requested_db_id != str(session.db_id or ""):
            await websocket.send_json({
                "type": "error", "code": "session_mismatch",
                "message": "只能向当前会话提交任务。", **context,
            })
            return True
        mode = normalize_mode(session.mode)
        if session.engine is None:
            await _rebuild_session_engine(session)
        if session.db_id:
            current_record = get_session(
                session.db_id, owner_id=session.owner_id,
            )
            if current_record is None or bool(current_record.get("archived")):
                unavailable_id = session.db_id
                unavailable_code = (
                    "session_archived" if current_record is not None
                    else "session_not_found"
                )
                await _reset_to_transient_session(session)
                reset_context = _run_submission_context(
                    session, request_id=request_id,
                    requested_db_id=unavailable_id,
                )
                await websocket.send_json({
                    "type": "error", "code": unavailable_code,
                    "active_reset": True, "run_owned_by_connection": False,
                    "message": (
                        "该会话已归档，请先取消归档。"
                        if current_record is not None
                        else "当前会话已不存在，已进入新对话。"
                    ),
                    **reset_context,
                })
                return True
        owner = active_run_owner(session.db_id) if session.db_id else None
        if _session_run_active(session) or owner is not None:
            active = session.active_controller or getattr(owner, "active_controller", None)
            await websocket.send_json({
                "type": "error", "code": "session_busy",
                "message": "已有任务正在运行，请先停止或等待完成。",
                "run_id": active.run_id if active is not None else None,
                "run_owned_by_connection": _session_run_active(session),
                **context,
            })
            return True
        orphan = _unrecovered_admission_run(session)
        if orphan is not None:
            await websocket.send_json({
                "type": "error", "code": "run_admission_recovery_required",
                "message": "上一任务的运行记录尚未完成恢复，模型不会启动。请重启 Desktop 后重试。",
                "run_id": str(orphan.get("run_id") or ""),
                "run_owned_by_connection": False, **context,
            })
            return True

        if session.extensions_revision < manager.extensions_revision:
            await _rebuild_session_engine(session)
            session.extensions_revision = manager.extensions_revision
        persisted = manager.persist_first(session)
        emitter, controller = _run_resources(websocket, session, mode)
        controller.transition(RunState.RUNNING)
        try:
            run = _persist_run_start(
                session, emitter, controller, mode,
                client_request_id=request_id,
                client_request_fingerprint=fingerprint,
            )
        except Exception:
            logger.exception("Run admission persistence failed")
            run = {}
        if not _canonical_run_admission(session, emitter, controller, run):
            duplicate = get_run_by_client_request_id(
                request_id, owner_id=session.owner_id,
            )
            controller.transition(RunState.FAILED)
            recovered = _fail_run_admission_checked(
                emitter.run_id, session_id=str(session.db_id or ""),
                stop_reason="admission_persistence_failed",
            )
            retain_barrier = (
                not recovered
                and _admission_failure_requires_barrier(
                    emitter.run_id, str(session.db_id or ""),
                )
            )
            if retain_barrier:
                _retain_admission_recovery_barrier(
                    websocket, session, emitter, controller,
                )
            elif session.active_run_id == emitter.run_id:
                session.active_run_id = None
            if (
                duplicate is not None
                and str(duplicate.get("run_id") or "") != emitter.run_id
                and str(duplicate.get("session_id") or "") == session.db_id
                and str(duplicate.get("client_request_fingerprint") or "") == fingerprint
            ):
                if persisted is not None:
                    await websocket.send_json({
                        "type": "session_persisted", **_session_identity(session),
                        "session": persisted,
                    })
                await _send_duplicate_run_admission(
                    websocket, session, duplicate,
                    request_id=request_id, requested_db_id=requested_db_id,
                )
            else:
                if persisted is not None:
                    await websocket.send_json({
                        "type": "session_persisted", **_session_identity(session),
                        "session": persisted,
                    })
                await websocket.send_json({
                    "type": "error", "code": "run_admission_failed",
                    "message": "任务接纳失败，请重试。",
                    "run_id": emitter.run_id,
                    **_run_submission_context(
                        session, request_id=request_id,
                        requested_db_id=requested_db_id,
                    ),
                })
            return True

        admitted = False
        gate = asyncio.Event()

        async def admitted_run() -> Any:
            await gate.wait()
            if not admitted:
                if not controller.is_terminal:
                    controller.transition(RunState.FAILED)
                _fail_run_admission_checked(
                    emitter.run_id, session_id=str(session.db_id or ""),
                    stop_reason="admission_transport_failed",
                )
                if session.active_controller is controller:
                    session.active_controller = None
                return emitter
            if context_messages:
                return await _run_preallocated_submission(
                    websocket, session, content, skill_id, mode=mode,
                    emitter=emitter, controller=controller,
                    context_messages=context_messages,
                    **({"attachments": attached_context} if attached_context else {}),
                )
            return await _run_preallocated_submission(
                websocket, session, content, skill_id, mode=mode,
                emitter=emitter, controller=controller,
                **({"attachments": attached_context} if attached_context else {}),
            )

        session.active_controller = controller
        started = start_session_run(
            session, admitted_run(),
            release_guard=_run_release_guard(session),
            on_settled=_run_settlement_callback(websocket, session),
        )
        if not started:
            if not controller.is_terminal:
                controller.transition(RunState.FAILED)
            recovered = _fail_run_admission_checked(
                emitter.run_id, session_id=str(session.db_id or ""),
                stop_reason="admission_conflict",
            )
            if session.active_controller is controller:
                session.active_controller = None
            retain_barrier = (
                not recovered
                and _admission_failure_requires_barrier(
                    emitter.run_id, str(session.db_id or ""),
                )
            )
            if retain_barrier:
                _retain_admission_recovery_barrier(
                    websocket, session, emitter, controller,
                )
            elif session.active_run_id == emitter.run_id:
                session.active_run_id = None
            await websocket.send_json({
                "type": "error", "code": "session_busy",
                "message": "已有任务正在运行，请先停止或等待完成。",
                "run_id": emitter.run_id,
                "run_owned_by_connection": _session_run_active(session),
                **_run_submission_context(
                    session, request_id=request_id,
                    requested_db_id=requested_db_id,
                ),
            })
            return True
        try:
            if persisted is not None:
                await websocket.send_json({
                    "type": "session_persisted", **_session_identity(session),
                    "session": persisted,
                })
            await websocket.send_json({
                "type": "run_accepted",
                **_run_submission_context(
                    session, request_id=request_id,
                    requested_db_id=requested_db_id,
                ),
                "run_id": emitter.run_id,
                "duplicate": False,
                "state": "running", "status": "running",
                "stop_reason": None, "owned": True,
                "run_owned_by_connection": True,
            })
            admitted = True
        finally:
            gate.set()
    return True


async def _handle_verification_retry(
    websocket: WebSocket, session: DaoSession, message: dict[str, Any],
) -> None:
    """Start a verification repair only after its new ledger is canonical."""
    prior_run_id = str(message.get("run_id") or "")
    request_id = str(message.get("request_id") or "")[:128]
    context = {
        "operation": "retry_verification", "request_id": request_id,
        "prior_run_id": prior_run_id, **_session_identity(session),
    }
    prior_run = get_run(prior_run_id) if prior_run_id else None
    if (
        not session.db_id
        or prior_run is None
        or str(prior_run.get("session_id") or "") != session.db_id
    ):
        await websocket.send_json({
            "type": "error", "code": "verification_retry_not_found",
            "message": "未找到可重试的验证运行。", **context,
        })
        return

    retry_reason = str(prior_run.get("stop_reason") or "")
    if retry_reason not in {"verification_required", "verification_retry_limit"}:
        await websocket.send_json({
            "type": "error", "code": "verification_retry_not_allowed",
            "message": "该运行没有待处理的验证失败。", **context,
        })
        return

    retry_message = (
        "继续处理上一轮代码任务。上一轮修改尚未通过验证，请先检查最近一次验证结果，"
        "必要时修复代码，然后必须调用 run_tests 并确认 status=passed；不要只口头说明已完成。"
    )
    async with manager.run_admission_lock:
        owner = active_run_owner(session.db_id)
        if _session_run_active(session) or owner is not None:
            active = session.active_controller or getattr(owner, "active_controller", None)
            await websocket.send_json({
                "type": "error", "code": "session_busy",
                "message": "当前任务仍在收尾，请稍后再重试验证。",
                "run_id": active.run_id if active is not None else None,
                "run_owned_by_connection": _session_run_active(session),
                **context,
            })
            return
        orphan = _unrecovered_admission_run(session)
        if orphan is not None:
            await websocket.send_json({
                "type": "error", "code": "run_admission_recovery_required",
                "message": "上一任务的运行记录尚未完成恢复，模型不会启动。请重启 Desktop 后重试。",
                "run_id": str(orphan.get("run_id") or ""),
                "run_owned_by_connection": False, **context,
            })
            return
        if session.extensions_revision < manager.extensions_revision:
            await _rebuild_session_engine(session)
            session.extensions_revision = manager.extensions_revision

        emitter, controller = _run_resources(websocket, session, DEFAULT_MODE)
        controller.transition(RunState.RUNNING)
        try:
            run = _persist_run_start(
                session, emitter, controller, DEFAULT_MODE,
                verification_required=True,
            )
        except Exception:
            logger.exception("verification retry admission persistence failed")
            run = {}
        if not _canonical_run_admission(session, emitter, controller, run):
            if not controller.is_terminal:
                controller.transition(RunState.FAILED)
            recovered = _fail_run_admission_checked(
                emitter.run_id, session_id=str(session.db_id or ""),
                stop_reason="admission_persistence_failed",
            )
            retain_barrier = (
                not recovered
                and _admission_failure_requires_barrier(
                    emitter.run_id, str(session.db_id or ""),
                )
            )
            if retain_barrier:
                _retain_admission_recovery_barrier(
                    websocket, session, emitter, controller,
                )
            elif session.active_run_id == emitter.run_id:
                session.active_run_id = None
            await websocket.send_json({
                "type": "error", "code": "verification_retry_admission_failed",
                "message": "验证重试未能建立完整运行记录，模型尚未启动，请重试。",
                "run_id": emitter.run_id, **context,
            })
            return

        admitted = False
        gate = asyncio.Event()

        async def admitted_retry() -> Any:
            await gate.wait()
            if not admitted:
                if not controller.is_terminal:
                    controller.transition(RunState.FAILED)
                _fail_run_admission_checked(
                    emitter.run_id, session_id=str(session.db_id or ""),
                    stop_reason="admission_transport_failed",
                )
                if session.active_controller is controller:
                    session.active_controller = None
                return emitter
            return await _stream_to_ws(
                websocket, session, retry_message, mode=DEFAULT_MODE,
                emitter=emitter, controller=controller,
                verification_required=True, persisted_run=True,
                manage_controller=True,
            )

        session.active_controller = controller
        started = start_session_run(
            session, admitted_retry(),
            release_guard=_run_release_guard(session),
            on_settled=_run_settlement_callback(websocket, session),
        )
        if not started:
            if not controller.is_terminal:
                controller.transition(RunState.FAILED)
            recovered = _fail_run_admission_checked(
                emitter.run_id, session_id=str(session.db_id or ""),
                stop_reason="admission_conflict",
            )
            if session.active_controller is controller:
                session.active_controller = None
            retain_barrier = (
                not recovered
                and _admission_failure_requires_barrier(
                    emitter.run_id, str(session.db_id or ""),
                )
            )
            if retain_barrier:
                _retain_admission_recovery_barrier(
                    websocket, session, emitter, controller,
                )
            elif session.active_run_id == emitter.run_id:
                session.active_run_id = None
            await websocket.send_json({
                "type": "error", "code": "session_busy",
                "message": "已有任务正在运行，请稍后重试。",
                "run_id": emitter.run_id,
                "run_owned_by_connection": _session_run_active(session),
                **context,
            })
            return
        try:
            await websocket.send_json({
                "type": "verification_retry_started",
                "run_id": emitter.run_id,
                "message": "已开始新的修复与验证运行。",
                **context,
            })
            admitted = True
        finally:
            gate.set()


async def _reject_session_mutation_while_running(
    websocket: WebSocket, session: DaoSession, operation: str,
    *, target_db_id: str | None = None,
    error_context: dict[str, Any] | None = None,
) -> bool:
    """Keep a run's persistence identity immutable until it reaches terminal state."""
    target_id = session.db_id if target_db_id is None else str(target_db_id or "")
    owner = active_run_owner(target_id) if target_id else None
    own_run_active = _session_run_active(session)
    own_run_target = str(session.active_run_session_id or session.db_id or "")
    own_run_blocks_target = own_run_active and (
        not target_id or target_id == own_run_target
    )
    if not own_run_blocks_target and owner is None:
        return False
    controller = session.active_controller if own_run_blocks_target else getattr(owner, "active_controller", None)
    packet = {
        "type": "error",
        "code": "session_busy",
        "message": f"当前任务仍在运行，停止或等待完成后才能{operation}。",
        "run_id": controller.run_id if controller else None,
        "run_owned_by_connection": own_run_blocks_target,
        **_session_identity(session),
    }
    packet.update(error_context or {})
    await websocket.send_json(packet)
    return True


def _run_settlement_callback(websocket: WebSocket, session: DaoSession):
    """Notify every live view of the conversation after ownership is clear.

    A reconnect may retry an already-running submission on a new WebSocket.
    The original transport still owns fail-closed cancellation, but settlement
    must also reach the replacement view or its composer could remain locked.
    """
    async def settled(run_id: str) -> None:
        recipients = [
            (runtime, socket)
            for runtime, socket in manager.websocket_items()
            if runtime.db_id and runtime.db_id == session.db_id
        ]
        if not recipients:
            recipients = [(session, websocket)]
        for runtime, socket in recipients:
            try:
                await socket.send_json({
                    "type": "run_settled", "run_id": run_id,
                    "run_owned_by_connection": runtime is session,
                    **_session_identity(runtime),
                })
            except Exception:
                logger.debug(
                    "run settlement notification failed for runtime=%s",
                    runtime.id, exc_info=True,
                )

    return settled


_TRANSCRIPT_EVENT_KEYS = (
    "event_id", "run_id", "channel_id", "parent_event_id", "sequence",
    "timestamp", "mode", "actor", "type", "status", "payload", "part_id",
    "revision", "workspace_id", "task_id", "artifact_ids", "schema",
)


def _transcript_event(
    event: dict[str, Any], *, workbench: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projected = {
        key: event.get(key, [] if key == "artifact_ids" else None)
        for key in _TRANSCRIPT_EVENT_KEYS
    }
    if workbench is not None and str(event.get("type") or "") in {
        "run_completed", "run_error",
    }:
        projected["workbench"] = workbench
    return projected


def _transcript_cursor_map(value: Any) -> dict[str, int] | None:
    """Validate the small, untrusted cursor map supplied by the browser."""
    if not isinstance(value, dict) or len(value) > 100:
        return None
    cursors: dict[str, int] = {}
    for run_id, sequence in value.items():
        if not isinstance(run_id, str) or not run_id or isinstance(sequence, bool):
            return None
        try:
            parsed = int(sequence)
        except (TypeError, ValueError):
            return None
        if parsed < 0:
            return None
        cursors[run_id] = parsed
    return cursors


async def _send_transcript_reset(
    websocket: WebSocket, session_id: str, history: list[dict[str, Any]],
) -> int:
    workbench = build_workbench_snapshot(session_id)
    workbench_runs = {
        str(run.get("run_id") or ""): run for run in workbench.get("runs", [])
    }
    events = [
        _transcript_event(
            event,
            workbench=workbench_runs.get(str(item["run"].get("run_id") or "")),
        )
        for item in history
        for event in item["events"]
    ]
    await websocket.send_json({
        "type": "transcript_reset", "session_id": session_id,
        "runs": [item["run"] for item in history], "events": events,
        "cursors": {
            str(item["run"]["run_id"]): max(
                (int(event["sequence"]) for event in item["events"]), default=0,
            )
            for item in history
        },
    })
    return len(events)


async def _send_transcript_ops(
    websocket: WebSocket, session_id: str, history: list[dict[str, Any]],
    cursors: dict[str, int], *, request_id: str = "",
) -> int | None:
    """Send idempotent run suffixes, or signal that a full reset is required."""
    session_run_ids = {str(item["run"]["run_id"]) for item in history}
    if any(run_id not in session_run_ids for run_id in cursors):
        return None
    event_count = 0
    workbench = build_workbench_snapshot(session_id)
    workbench_runs = {
        str(run.get("run_id") or ""): run for run in workbench.get("runs", [])
    }
    for item in history:
        run_id = str(item["run"]["run_id"])
        latest = max((int(event["sequence"]) for event in item["events"]), default=0)
        since_sequence = cursors.get(run_id, 0)
        if since_sequence > latest:
            return None
        events = [
            _transcript_event(event, workbench=workbench_runs.get(run_id))
            for event in get_run_events_since(run_id, since_sequence)
        ]
        event_count += len(events)
        await websocket.send_json({
            "type": "transcript_ops", "session_id": session_id,
            "run_id": run_id, "since_sequence": since_sequence,
            "cursor": latest, "events": events,
            **({"request_id": request_id} if request_id else {}),
        })
    return event_count


async def _send_session_replay(
    websocket: WebSocket, session_id: str, *, owner_id: str,
    cursors: dict[str, int] | None = None,
) -> bool:
    """Synchronize bounded typed history, falling back to legacy rows once."""
    restored = restore_session(session_id, owner_id=owner_id)
    if not restored:
        return False
    history = get_session_run_history(session_id, limit=50)
    legacy_messages = get_legacy_messages(session_id)
    await websocket.send_json({
        "type": "session_history_start", "session_id": session_id,
        "runs": [item["run"] for item in history],
        "incremental": cursors is not None,
        "legacy_messages": [
            {"role": item["role"], "content": item["content"]}
            for item in legacy_messages
        ],
    })
    typed_count = None
    if cursors is not None:
        typed_count = await _send_transcript_ops(websocket, session_id, history, cursors)
    if typed_count is None:
        typed_count = await _send_transcript_reset(websocket, session_id, history)
    total_event_count = sum(len(item["events"]) for item in history)
    message_count = len(restored.get("messages", []))
    # A migrated conversation may have a legacy message prefix followed by
    # typed Runs. Send that prefix as one ordered snapshot before Run events;
    # modern message rows are excluded to avoid duplicating typed content.
    # Restore the authoritative Run/Task/Artifact projection after transcript
    # reset/ops.  Sending it earlier would let ``transcript_reset`` clear the
    # freshly loaded workbench, while relying on a later client request leaves
    # session-switch restores with a populated timeline and an empty Run rail.
    await websocket.send_json({
        "type": "workbench_snapshot",
        "data": build_workbench_snapshot(session_id),
    })
    await websocket.send_json({
        "type": "session_history_end", "session_id": session_id,
        "run_count": len(history), "event_count": total_event_count,
        "message_count": message_count,
    })
    return True


def _session_is_reusable_blank(session_id: str, *, owner_id: str = "") -> bool:
    """A deliberate empty conversation may be reused instead of duplicated."""
    if not session_id:
        return False
    record = restore_session(session_id, owner_id=owner_id)
    if record is None or record.get("messages"):
        return False
    if get_session_run_history(session_id, limit=1):
        return False
    from modus.desktop.memory import get_memories

    return not get_memories(session_id)


async def _stream_to_ws(
    websocket: WebSocket,
    session: DaoSession,
    message: str,
    *,
    mode: str = DEFAULT_MODE,
    emitter: RunEventEmitter | None = None,
    controller: RunController | None = None,
    user_visible_message: str | None = None,
    emit_user_message: bool = True,
    transient_context: list[Message] | None = None,
    verification_required: bool = False,
    broadcast_catalog: bool = True,
    persisted_run: bool = False,
    manage_controller: bool | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> RunEventEmitter:
    """Connect server dependencies to the default Agent runner."""
    try:
        return await stream_to_ws(
            websocket, session, message, mode=mode, emitter=emitter, controller=controller,
            user_visible_message=user_visible_message, emit_user_message=emit_user_message,
            transient_context=transient_context, verification_required=verification_required,
            audit_event_for=_audit_event_for, wait_for_user_approval_callback=wait_for_user_approval,
            extract_worldview=_extract_worldview, compress_history=_maybe_compress_history,
            persist_run_start=None if persisted_run else _persist_run_start,
            manage_controller=manage_controller,
            attachments=attachments,
        )
    finally:
        if broadcast_catalog:
            await _broadcast_sessions_list(completed_runtime=session)

def _extract_worldview(user_message: str, done_event: dict) -> str:
    """从首次对话中提取世界观简述"""
    messages = done_event.get("messages") or []
    for m in reversed(messages):
        if m.role == "assistant" and m.content:
            text = m.content if isinstance(m.content, str) else str(m.content)
            return f"用户最初任务：{user_message[:80]}\n道的初判：{text[:120]}..."
    return f"用户意图：{user_message[:80]}"

def _maybe_compress_history(
    session: DaoSession, *, run_id: str | None = None,
) -> dict[str, Any] | None:
    """Apply configured, instruction-safe context compaction to model context only."""
    from modus.agent.compressor import compress_messages, should_compress

    history = session.main_history
    config = getattr(getattr(session, "engine", None), "config", None)
    compression = getattr(getattr(config, "features", None), "compression", None)
    if compression is None or not bool(getattr(compression, "enabled", True)):
        return None
    threshold = max(1, int(getattr(compression, "trigger_tokens", 80_000)))
    if not should_compress(history, threshold=threshold):
        return None
    tail_count = max(2, int(getattr(compression, "tail_messages", 8)))
    boundary: dict[str, int] | None = None
    if session.db_id:
        from modus.desktop.db import get_context_compaction_boundary

        boundary = get_context_compaction_boundary(session.db_id, tail_count)
    omitted = (
        boundary["omitted_count"]
        if boundary is not None
        else max(0, len(history) - tail_count)
    )
    fallback_summary = (
        f"{omitted} earlier messages were omitted to keep this run within its context budget. "
        "Their text is not an active instruction. Ask the user to restate any detail that is "
        "required but absent from the recent messages."
    )
    summary = fallback_summary
    semantic_used = False
    semantic = bool(getattr(compression, "semantic", False))
    if semantic:
        summary = _try_semantic_summary(
            history, tail_count, config, fallback_summary,
        )
        semantic_used = summary is not fallback_summary
        if semantic_used:
            # A real summary is shorter and precise; keep the count framing so
            # the reference-only marker still tells the model what was dropped.
            summary = (
                f"[{omitted} earlier messages omitted — condensed below]\n"
                f"{summary}"
            )
    session.main_history = compress_messages(history, summary=summary, tail_count=tail_count)
    payload = {
        "summary": summary,
        "omitted_count": omitted,
        "tail_count": tail_count,
        "reference_only": True,
    }
    if semantic_used:
        payload["semantic"] = True
    if session.db_id:
        from modus.desktop.db import create_context_compaction

        cutoff_message_id = boundary["cutoff_message_id"] if boundary is not None else 0
        record = create_context_compaction(
            session_id=session.db_id, run_id=run_id, summary=summary,
            omitted_count=omitted, tail_count=tail_count,
            cutoff_message_id=cutoff_message_id,
        )
        payload["compaction_id"] = record.get("compaction_id")
        payload["cutoff_message_id"] = cutoff_message_id
    return payload


def _try_semantic_summary(
    history: list[Message],
    tail_count: int,
    config: Any,
    fallback: str,
) -> str:
    """Ask the configured LLM to summarise the omitted middle turns.

    Returns ``fallback`` on any failure so compaction never blocks on the
    model.  Only the omitted prefix (messages before the retained tail) is
    summarised; the active tail is never touched.
    """
    try:
        llm = getattr(config, "llm", None)
        api_key = getattr(llm, "api_key", "") or ""
        if not api_key:
            return fallback
        omitted_prefix = history[: max(0, len(history) - tail_count)]
        if not omitted_prefix:
            return fallback
        compression = getattr(getattr(config, "features", None), "compression", None)
        input_chars = int(getattr(compression, "semantic_input_chars", 24_000))
        import asyncio

        from modus.desktop.summarizer import summarize_omitted

        coroutine = summarize_omitted(
            messages=omitted_prefix,
            provider=getattr(llm, "provider", "") or "deepseek",
            model=getattr(llm, "model", "") or "",
            api_key=api_key,
            base_url=getattr(llm, "base_url", None) or None,
            max_input_chars=input_chars,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        # Running inside the server loop: run to completion without touching
        # the shared loop.  A fresh thread keeps the summarizer's own HTTP
        # client from stealing the loop's transport.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coroutine).result()
    except Exception:
        return fallback


async def _run_moa_stream(
    websocket: WebSocket,
    session: DaoSession,
    messages: list[Message],
    *,
    emitter: RunEventEmitter | None = None,
    controller: RunController | None = None,
) -> str:
    """Connect server dependencies to the MOA runner."""
    guidance = await run_moa_stream(
        websocket, session, messages, emitter=emitter, controller=controller,
        audit_event_for=_audit_event_for,
        load_models=lambda: _load_models_for_session(session, MOA_MODE),
    )
    return guidance


async def _run_peri_stream(
    websocket: WebSocket,
    session: DaoSession,
    message: str,
    *,
    emitter: RunEventEmitter | None = None,
    controller: RunController | None = None,
    skill_message: Message | None = None,
    context_messages: list[Message] | None = None,
    persisted_run: bool = False,
    manage_controller: bool | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> RunEventEmitter:
    """Connect server dependencies to the Peri runner."""
    return await run_peri_stream(
        websocket, session, message, emitter=emitter, controller=controller,
        audit_event_for=_audit_event_for,
        load_models=lambda: _load_models_for_session(session, PERI_MODE),
        reset_cancel=session._ensure_cancel,
        persist_run_start=None if persisted_run else _persist_run_start,
        skill_message=skill_message,
        context_messages=context_messages,
        manage_controller=manage_controller,
        wait_for_user_approval_callback=wait_for_user_approval,
        attachments=attachments,
    )
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    websocket = _SerializedWebSocket(websocket)
    logger.info("WebSocket connected")

    selected_model = model_repository.runtime_model()
    config = _apply_runtime_model(load_config(), selected_model)
    has_api_key = bool(config.llm.api_key)
    if not has_api_key:
        logger.warning("WebSocket started without a primary API key; settings remain available")

    engine = (
        await _build_session_engine(
            str((selected_model or {}).get("id") or "") or None,
            workspace_root=_desktop_default_workspace_root,
        )
        if _desktop_default_workspace_root else None
    )
    session = manager.create(engine, workspace_root=_desktop_default_workspace_root)
    owner_context = _active_owner_id.set(session.owner_id)
    if _desktop_default_workspace_root:
        upsert_workspace(
            WorkspaceIdentity.from_path(_desktop_default_workspace_root),
            owner_id=session.owner_id,
        )
    manager.attach_websocket(session, websocket)
    session.model_id = str((selected_model or {}).get("id") or "")
    await websocket.send_json({"type": "session_ready", **_session_identity(session)})

    try:
        while True:
            raw = await websocket.receive_text()
            _active_owner_id.set(session.owner_id)
            msg = json.loads(raw)
            msg_type = msg.get("type", "")

            owner_before_dispatch = session.owner_id
            if await command_router.dispatch(websocket, session, msg):
                if session.owner_id != owner_before_dispatch:
                    _active_owner_id.set(session.owner_id)
                    await _session_resources(session).mcp.connect_all()
                continue

            if msg_type == "resume_session":
                db_id = str(msg.get("db_id") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                resume_context = {
                    "operation": "resume_session",
                    "requested_db_id": db_id,
                    **({"request_id": request_id} if request_id else {}),
                }
                # A run parked on a previous disconnect can be resumed onto this
                # socket instead of being reported busy.  The parked session
                # keeps its emitter/controller in the process-wide ownership
                # registry, so re-point its transport and replay the ledger.
                parked_owner = active_run_owner(db_id) if db_id else None
                if parked_owner is not None and getattr(parked_owner, "parked", False):
                    parked_owner.resume_parked(websocket.send_json)
                    cursors = _transcript_cursor_map(msg.get("cursors"))
                    await _send_session_replay(
                        websocket, db_id, owner_id=session.owner_id, cursors=cursors,
                    )
                    await websocket.send_json({
                        "type": "session_restored",
                        **_session_identity(session),
                        **resume_context,
                        "resumed_parked_run": True,
                        "last_run": latest_run_for_session(db_id),
                    })
                    continue
                if await _reject_session_mutation_while_running(
                    websocket, session, "恢复其他会话",
                    error_context=resume_context,
                ):
                    continue
                if await _reject_session_mutation_while_running(
                    websocket, session, "恢复其他会话", target_db_id=db_id,
                    error_context=resume_context,
                ):
                    continue
                if db_id:
                    restored = restore_session(db_id, owner_id=session.owner_id)
                    if restored:
                        if bool(restored.get("archived")):
                            await websocket.send_json({
                                "type": "error", "code": "session_archived",
                                **_session_identity(session), **resume_context,
                                "message": "该会话已归档，请先取消归档。",
                            })
                            continue
                        await _bind_persisted_session(session, restored)
                        await _repair_session_repository_binding(session)
                        cursors = _transcript_cursor_map(msg.get("cursors"))
                        await _send_session_replay(
                            websocket, db_id, owner_id=session.owner_id, cursors=cursors,
                        )
                        await websocket.send_json({
                            "type": "session_restored",
                            **_session_identity(session),
                            **resume_context,
                            "last_run": latest_run_for_session(db_id),
                        })
                        continue
                await websocket.send_json({
                    "type": "error", "code": "session_not_found",
                    **_session_identity(session), **resume_context,
                    "message": "未找到要恢复的会话。",
                })
                continue

            if msg_type == "approval_response":
                run_id = str(msg.get("run_id") or "")
                approval_id = str(msg.get("approval_id") or "")
                decision = str(msg.get("decision") or "deny")
                if not resolve_pending_approval(session, run_id, approval_id, decision):
                    logger.warning(
                        "approval response ignored for unknown/stale run=%s id=%s",
                        run_id,
                        approval_id,
                    )
            elif msg_type == "retry_verification":
                await _handle_verification_retry(websocket, session, msg)
            elif msg_type == "transcript_sync":
                run_id = str(msg.get("run_id") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                run = get_run(run_id) if run_id else None
                if not session.db_id or run is None or run.get("session_id") != session.db_id:
                    await websocket.send_json({
                        "type": "error", "code": "transcript_not_found",
                        "operation": "transcript_sync", "run_id": run_id,
                        "request_id": request_id,
                        "message": "未找到要同步的运行记录。",
                    })
                    continue
                cursors = _transcript_cursor_map({run_id: msg.get("since_sequence", 0)})
                if cursors is None:
                    await websocket.send_json({
                        "type": "error", "code": "invalid_transcript_cursor",
                        "operation": "transcript_sync", "run_id": run_id,
                        "request_id": request_id,
                        "message": "消息同步游标无效。",
                    })
                    continue
                history = [{"run": run, "events": get_run_events_since(run_id, 0)}]
                synced = await _send_transcript_ops(
                    websocket, session.db_id, history, cursors,
                    request_id=request_id,
                )
                if synced is None:
                    await _send_session_replay(
                        websocket, session.db_id, owner_id=session.owner_id,
                    )
            elif msg_type == "run_message":
                await _handle_explicit_run_message(websocket, session, msg)
            elif msg_type == "cancel":
                session.cancel_stream()
                await websocket.send_json({
                    "type": "cancel_requested",
                    "run_id": session.active_controller.run_id if session.active_controller else None,
                })
            elif msg_type == "sessions_list":
                request_id = str(msg.get("request_id") or "")[:128]
                query = str(msg.get("query") or "").strip()[:500]
                include_archived = bool(msg.get("include_archived", False))
                try:
                    limit = max(1, min(int(msg.get("limit", 50)), 100))
                    cursor = _session_catalog_cursor(msg.get("cursor"))
                except (TypeError, ValueError):
                    await websocket.send_json({
                        "type": "error", "code": "invalid_session_catalog_request",
                        "operation": "sessions_list", "request_id": request_id,
                        "message": "会话目录分页参数无效。",
                    })
                    continue
                await websocket.send_json(_sessions_list_packet(
                    request_id=request_id, query=query,
                    include_archived=include_archived, cursor=cursor, limit=limit,
                    owner_id=session.owner_id,
                ))
            elif msg_type == "workspaces_list":
                from modus.desktop import db as desktop_db

                await websocket.send_json({
                    "type": "workspaces_list",
                    "request_id": str(msg.get("request_id") or "")[:128],
                    "workspaces": desktop_db.list_workspaces(
                        owner_id=session.owner_id,
                    ),
                })
            elif msg_type == "workspace_set_default":
                from modus.desktop import db as desktop_db

                workspace_id = str(msg.get("workspace_id") or "").strip()
                request_id = str(msg.get("request_id") or "")[:128]
                workspace = desktop_db.set_default_workspace(
                    session.owner_id, workspace_id,
                )
                if workspace is None:
                    await websocket.send_json({
                        "type": "error", "code": "workspace_not_found",
                        "operation": "workspace_set_default",
                        "request_id": request_id,
                        "message": "未找到该工作区，无法设为默认。",
                    })
                    continue
                await websocket.send_json({
                    "type": "workspace_default_updated",
                    "operation": "workspace_set_default",
                    "request_id": request_id,
                    "workspace": workspace,
                })
            elif msg_type == "workspace_forget":
                from modus.desktop import db as desktop_db

                workspace_id = str(msg.get("workspace_id") or "").strip()
                request_id = str(msg.get("request_id") or "")[:128]
                if not desktop_db.forget_workspace(session.owner_id, workspace_id):
                    await websocket.send_json({
                        "type": "error", "code": "workspace_not_found",
                        "operation": "workspace_forget", "request_id": request_id,
                        "message": "未找到要移除的工作区记录。",
                    })
                    continue
                active_cleared = workspace_id == session.workspace_id
                if active_cleared:
                    if session.db_id:
                        update_session(session.db_id, workspace_id=None)
                    session.workspace_id = ""
                    session.workspace_root = ""
                    session.workspace_name = ""
                    await _rebuild_session_engine(session)
                await websocket.send_json({
                    "type": "workspace_forgotten", "operation": "workspace_forget",
                    "request_id": request_id, "workspace_id": workspace_id,
                    "active_cleared": active_cleared,
                    **(_session_identity(session) if active_cleared else {}),
                })
                if active_cleared:
                    await _broadcast_sessions_list(origin=session)
            elif msg_type == "workspace_pick":
                request_id = str(msg.get("request_id") or "")[:128]
                try:
                    path = await pick_directory()
                    if not path:
                        await websocket.send_json({
                            "type": "workspace_pick_cancelled",
                            "operation": "workspace_pick",
                            "request_id": request_id,
                        })
                        continue
                    identity = WorkspaceIdentity.from_path(path)
                    workspace = upsert_workspace(
                        identity, owner_id=session.owner_id,
                    )
                    await websocket.send_json({
                        "type": "workspace_opened",
                        "operation": "workspace_pick",
                        "request_id": request_id,
                        "workspace": workspace,
                    })
                except (DirectoryPickerUnavailable, DirectoryPickerError, ValueError) as exc:
                    await websocket.send_json({
                        "type": "error", "code": "workspace_pick_failed",
                        "operation": "workspace_pick", "request_id": request_id,
                        "message": str(exc),
                    })
            elif msg_type == "workspace_open":
                path = str(msg.get("path") or "").strip()
                request_id = str(msg.get("request_id") or "")[:128]
                try:
                    if not path:
                        raise ValueError("path is required")
                    identity = WorkspaceIdentity.from_path(path)
                    workspace = upsert_workspace(
                        identity, owner_id=session.owner_id,
                    )
                    await websocket.send_json({
                        "type": "workspace_opened", "request_id": request_id,
                        "workspace": workspace,
                    })
                except ValueError as exc:
                    await websocket.send_json({
                        "type": "error", "code": "workspace_open_failed",
                        "operation": "workspace_open", "request_id": request_id,
                        "message": str(exc),
                    })
            elif msg_type == "session_create":
                request_key = str(msg.get("request_key") or "").strip()
                if await _reject_session_mutation_while_running(
                    websocket, session, "创建并切换会话",
                    error_context={
                        "operation": "session_create", "request_key": request_key,
                    },
                ):
                    continue
                title = msg.get("title", "新对话")
                requested_workspace_id = str(msg.get("workspace_id") or "").strip()
                if requested_workspace_id:
                    selected_workspace = await _select_session_workspace(
                        session, requested_workspace_id,
                    )
                    if selected_workspace is None:
                        await websocket.send_json({
                            "type": "error", "code": "workspace_not_found",
                            "operation": "session_create", "request_key": request_key,
                            "message": "未找到所选工作区，请重新选择项目。",
                        })
                        continue
                elif _desktop_default_workspace_root:
                    default_workspace = WorkspaceIdentity.from_path(
                        _desktop_default_workspace_root,
                    )
                    upsert_workspace(
                        default_workspace, owner_id=session.owner_id,
                    )
                    await _select_session_workspace(
                        session, default_workspace.workspace_id,
                    )
                else:
                    # A user-selected account default is explicit application
                    # state, unlike the server process directory. New sessions
                    # may inherit it while remaining isolated per account.
                    from modus.desktop import db as desktop_db

                    account_default = desktop_db.get_default_workspace(
                        session.owner_id,
                    )
                    if account_default:
                        await _select_session_workspace(
                            session, str(account_default["workspace_id"]),
                        )
                    else:
                        session.workspace_id = ""
                        session.workspace_root = ""
                        session.workspace_name = ""
                        await _rebuild_session_engine(session)
                mode = normalize_mode(msg.get("mode"))
                mode_config = _session_mode_snapshot(mode)
                if mode in COLLABORATION_MODES and not _mode_roles_complete(mode, mode_config):
                    await websocket.send_json({
                        "type": "error", "code": "mode_not_configured",
                        "operation": "session_create", "request_key": request_key,
                        "message": f"{mode.upper() if mode == MOA_MODE else 'Peri'} 尚未配置完整的 Host 与协作模型。",
                    })
                    continue
                default_model_id = str(
                    model_repository.public_snapshot()["selection"].get("default_model_id") or ""
                )
                session_model_id = _host_model_id(mode_config, default_model_id)
                reusable_blank = _session_is_reusable_blank(
                    session.db_id, owner_id=session.owner_id,
                )
                if reusable_blank:
                    update_session(
                        session.db_id, title=str(title or "新对话"), mode=mode,
                        model_id=session_model_id, mode_config=mode_config,
                        reasoning_effort=str(msg.get("reasoning_effort") or ""),
                        system_prompt="", worldview="", world_view_history="[]",
                        workspace_id=session.workspace_id or None,
                    )
                    db_sess = get_session(
                        session.db_id, owner_id=session.owner_id,
                    )
                    created = False
                elif request_key and session.pending_session_create_key == request_key and session.db_id:
                    db_sess = get_session(
                        session.db_id, owner_id=session.owner_id,
                    )
                    created = False
                else:
                    db_sess, created = manager.create_persisted_once(
                        session, request_key=request_key, title=str(title or "新对话"), mode=mode,
                        model_id=session_model_id, mode_config=mode_config,
                        reasoning_effort=str(msg.get("reasoning_effort") or ""),
                    )
                if not db_sess:
                    await websocket.send_json({
                        "type": "error", "code": "session_create_failed",
                        "operation": "session_create", "request_key": request_key,
                        "message": "无法创建会话。",
                    })
                    continue
                # Switch the backend session to the new DB record
                session.db_id = db_sess["id"]
                session.main_history = []
                session.worldview = ""
                session.world_view_history = []
                session.system_prompt = ""
                session.model_id = session_model_id
                session.mode = normalize_mode(mode)
                session.mode_config = mode_config
                session.reasoning_effort = str(msg.get("reasoning_effort") or "") or None
                await _rebuild_session_engine(session)
                await websocket.send_json({
                    "type": "session_created", **_session_identity(session), "created": created,
                    "session": db_sess,
                })
                await _broadcast_sessions_list(origin=session)
            elif msg_type == "session_delete":
                sid = str(msg.get("session_id") or "")
                if await _require_sessions(
                    websocket, session, [sid] if sid else [], "删除",
                ) is None:
                    continue
                if await _reject_session_mutation_while_running(
                    websocket, session, "删除会话", target_db_id=sid,
                ):
                    continue
                delete_session(sid)
                active_reset = False
                invalidated_db_id = ""
                if sid == session.db_id:
                    invalidated_db_id = sid
                    await _reset_to_transient_session(session)
                    active_reset = True
                await websocket.send_json({
                    "type": "session_deleted", "deleted_db_id": sid,
                    "active_reset": active_reset,
                    "invalidated_db_id": invalidated_db_id,
                    **_session_identity(session),
                })
                await _broadcast_sessions_list(origin=session)
            elif msg_type == "session_delete_batch":
                ids = _normalized_session_ids(msg.get("session_ids"))
                if ids is None:
                    await websocket.send_json({
                        "type": "error", "code": "invalid_session_ids",
                        "message": "批量删除需要提供会话 ID 列表。",
                    })
                    continue
                if await _require_sessions(websocket, session, ids, "批量删除") is None:
                    continue
                busy_target = next((sid for sid in ids if active_run_owner(sid)), "")
                if busy_target and await _reject_session_mutation_while_running(
                    websocket, session, "删除会话", target_db_id=busy_target,
                ):
                    continue
                active_reset = False
                invalidated_db_id = ""
                for sid in ids:
                    delete_session(sid)
                    if sid == session.db_id:
                        invalidated_db_id = sid
                        await _reset_to_transient_session(session)
                        active_reset = True
                await websocket.send_json({
                    "type": "session_deleted", "deleted_db_ids": ids,
                    "active_reset": active_reset,
                    "invalidated_db_id": invalidated_db_id,
                    **_session_identity(session),
                })
                await _broadcast_sessions_list(origin=session)
            elif msg_type in {"session_archive", "session_restore_archive"}:
                sid = str(msg.get("session_id") or "")
                if not sid or get_session(sid, owner_id=session.owner_id) is None:
                    await websocket.send_json({"type": "error", "code": "session_not_found", "message": "未找到会话。"})
                    continue
                archived = msg_type == "session_archive"
                if archived and await _reject_session_mutation_while_running(
                    websocket, session, "归档会话", target_db_id=sid,
                ):
                    continue
                update_session(sid, archived=1 if archived else 0)
                active_reset = False
                invalidated_db_id = ""
                if archived and sid == session.db_id:
                    invalidated_db_id = sid
                    await _reset_to_transient_session(session)
                    active_reset = True
                await websocket.send_json({
                    "type": "session_archived", "session_id": sid, "archived": archived,
                    "active_reset": active_reset,
                    "invalidated_db_id": invalidated_db_id,
                    **_session_identity(session),
                })
                await _broadcast_sessions_list(origin=session)
            elif msg_type in {"session_archive_batch", "session_restore_archive_batch"}:
                normalized_ids = _normalized_session_ids(msg.get("session_ids"))
                if normalized_ids is None:
                    await websocket.send_json({
                        "type": "error", "code": "invalid_session_ids",
                        "message": "批量归档需要提供会话 ID 列表。",
                    })
                    continue
                if await _require_sessions(
                    websocket, session, normalized_ids, "批量归档",
                ) is None:
                    continue
                archived = msg_type == "session_archive_batch"
                if archived:
                    busy_target = next(
                        (sid for sid in normalized_ids if active_run_owner(sid)), "",
                    )
                    if busy_target and await _reject_session_mutation_while_running(
                        websocket, session, "归档会话", target_db_id=busy_target,
                    ):
                        continue
                for sid in normalized_ids:
                    update_session(sid, archived=1 if archived else 0)
                invalidated_db_id = (
                    session.db_id if archived and session.db_id in normalized_ids else ""
                )
                active_reset = bool(invalidated_db_id)
                if active_reset:
                    await _reset_to_transient_session(session)
                await websocket.send_json({
                    "type": "session_archived", "session_ids": normalized_ids,
                    "archived": archived, "active_reset": active_reset,
                    "invalidated_db_id": invalidated_db_id,
                    **_session_identity(session),
                })
                await _broadcast_sessions_list(origin=session)
            elif msg_type == "session_export":
                raw_ids = msg.get("session_ids") or ([msg.get("session_id")] if msg.get("session_id") else [])
                ids = _normalized_session_ids(raw_ids)
                if ids is None:
                    await websocket.send_json({"type": "error", "code": "invalid_session_ids", "message": "导出需要提供会话 ID 列表。"})
                    continue
                records = await _require_sessions(websocket, session, ids, "导出")
                if records is None:
                    continue
                documents = [SessionDocument.from_record(restore_session(
                    str(record["id"]), owner_id=session.owner_id,
                ) or record) for record in records]
                try:
                    filename, mime, content = export_sessions(documents, export_format=str(msg.get("format") or "markdown"))
                    await websocket.send_json({"type": "session_export_ready", "filename": filename, "mime": mime, "content": content, "session_ids": ids})
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "code": "session_export_failed", "message": str(exc)})
            elif msg_type == "session_to_skill":
                raw_ids = msg.get("session_ids") or ([msg.get("session_id")] if msg.get("session_id") else [])
                ids = _normalized_session_ids(raw_ids)
                if ids is None:
                    await websocket.send_json({"type": "error", "code": "invalid_session_ids", "message": "转换 Skill 需要提供会话 ID 列表。"})
                    continue
                records = await _require_sessions(websocket, session, ids, "转换 Skill")
                if records is None:
                    continue
                if await _reject_shared_capability_mutation_while_running(
                    websocket, session, "从会话生成 Skill",
                ):
                    continue
                busy_target = next((sid for sid in ids if active_run_owner(sid)), "")
                if busy_target and await _reject_session_mutation_while_running(
                    websocket, session, "转换 Skill", target_db_id=busy_target,
                ):
                    continue
                documents = [SessionDocument.from_record(restore_session(
                    str(record["id"]), owner_id=session.owner_id,
                ) or record) for record in records]
                try:
                    specs = session_skill_specs(documents, conversion=str(msg.get("conversion") or "individual"), merged_name=str(msg.get("name") or ""))
                    existing_names = {str(item.get("name") or "") for item in skill_repository.list_public()}
                    saved = []
                    for index, spec in enumerate(specs):
                        candidate = str(spec.get("name") or "session-collection")
                        if candidate in existing_names:
                            suffix_source = str(ids[index]) if str(msg.get("conversion") or "individual") == "individual" and index < len(ids) else uuid.uuid4().hex
                            base = candidate[:57].rstrip("-_") or "session"
                            candidate = f"{base}-{suffix_source[:6]}"
                            while candidate in existing_names:
                                candidate = f"{base[:50]}-{uuid.uuid4().hex[:12]}"
                        saved_skill = skill_repository.save(**{**spec, "name": candidate})
                        existing_names.add(candidate)
                        saved.append(saved_skill.to_wire())
                    all_skills = skill_repository.list_public()
                    await websocket.send_json({
                        "type": "session_skills_created", "skills": saved,
                        "all_skills": all_skills,
                    })
                    await _broadcast_skills(
                        all_skills, origin=session, include_origin=False,
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "code": "session_skill_failed", "message": str(exc)})
            elif msg_type == "session_get":
                sid = str(msg.get("session_id") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                sess = get_session(sid, owner_id=session.owner_id)
                if sess is None:
                    await websocket.send_json({
                        "type": "error", "code": "session_not_found",
                        "operation": "session_get", "request_id": request_id,
                        "message": "未找到会话。", "session_id": sid,
                    })
                    continue
                await websocket.send_json({
                    "type": "session_data", "operation": "session_get",
                    "request_id": request_id, "session_id": sid,
                    "system_prompt": sess.get("system_prompt", ""),
                    "reasoning_effort": sess.get("reasoning_effort") or None,
                })
            elif msg_type == "session_switch":
                sid = str(msg.get("session_id") or "")
                if await _reject_session_mutation_while_running(
                    websocket, session, "切换会话",
                ):
                    continue
                if await _reject_session_mutation_while_running(
                    websocket, session, "切换会话", target_db_id=sid,
                ):
                    continue
                if sid:
                    restored = restore_session(sid, owner_id=session.owner_id)
                    if not restored:
                        await websocket.send_json({"type": "error", "code": "session_not_found", "message": "未找到要切换的会话。"})
                        continue
                    if bool(restored.get("archived")):
                        await websocket.send_json({
                            "type": "error", "code": "session_archived",
                            "operation": "session_switch",
                            "requested_db_id": str(sid),
                            "message": "该会话已归档，请先取消归档。",
                        })
                        continue
                    await _bind_persisted_session(session, restored)
                    await _repair_session_repository_binding(session)
                    await websocket.send_json({
                        "type": "session_history_reset", "session_id": sid,
                    })
                    await _send_session_replay(
                        websocket, sid, owner_id=session.owner_id,
                    )
                    await websocket.send_json({
                        "type": "session_switched", **_session_identity(session),
                        "last_run": latest_run_for_session(sid),
                    })
            elif msg_type == "session_update":
                sid = str(msg.get("session_id") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                error_context = {
                    "operation": "session_update", "request_id": request_id,
                    "session_id": sid,
                }
                if not sid or get_session(sid, owner_id=session.owner_id) is None:
                    await websocket.send_json({
                        "type": "error", "code": "session_not_found",
                        "message": "未找到要更新的会话。", **error_context,
                    })
                    continue
                if sid != session.db_id:
                    await websocket.send_json({
                        "type": "error", "code": "session_mismatch",
                        "message": "只能更新当前会话的执行设置。",
                        **error_context,
                    })
                    continue
                if await _reject_session_mutation_while_running(
                    websocket, session, "更新会话设置", target_db_id=sid,
                    error_context=error_context,
                ):
                    continue
                prompt = str(msg.get("system_prompt") or "")
                if msg.get("system_prompt") is not None:
                    update_session(sid, system_prompt=prompt)
                    if sid == session.db_id:
                        session.system_prompt = prompt
                        await _rebuild_session_engine(session)
                await websocket.send_json({
                    "type": "session_updated", "operation": "session_update",
                    "request_id": request_id,
                    "session_id": sid,
                    "system_prompt": prompt if msg.get("system_prompt") is not None else None,
                })
            elif msg_type == "session_set_reasoning":
                sid = str(msg.get("session_id") or msg.get("db_id") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                error_context = {
                    "operation": "session_set_reasoning",
                    "request_id": request_id, "requested_db_id": sid,
                    **_session_identity(session),
                }
                targets_current = not sid or sid == session.db_id
                if sid and get_session(sid, owner_id=session.owner_id) is None:
                    await websocket.send_json({
                        "type": "error", "code": "session_not_found",
                        "message": "未找到要更新的会话。", **error_context,
                    })
                    continue
                if sid and not targets_current:
                    await websocket.send_json({
                        "type": "error", "code": "session_mismatch",
                        "message": "只能更新当前会话的思考深度。",
                        **error_context,
                    })
                    continue
                if targets_current and await _reject_session_mutation_while_running(
                    websocket, session, "更新思考深度", target_db_id=sid or session.db_id,
                    error_context=error_context,
                ):
                    continue
                value = str(msg.get("reasoning_effort") or "").strip()
                model = model_repository.runtime_model(session.model_id or None)
                allowed = {str(item) for item in (model or {}).get("reasoning_efforts", [])}
                if value and value not in allowed:
                    await websocket.send_json({
                        "type": "error", "code": "invalid_reasoning_effort",
                        "message": "当前模型不支持该思考深度。",
                        **error_context,
                    })
                    continue
                if sid:
                    update_session(sid, reasoning_effort=value)
                if targets_current:
                    session.reasoning_effort = value or None
                    await _rebuild_session_engine(session)
                await websocket.send_json({
                    "type": "session_reasoning_updated",
                    "reasoning_effort": value or None,
                    **error_context, **_session_identity(session),
                })
            elif msg_type == "session_rename":
                sid = str(msg.get("session_id") or "")
                title = str(msg.get("title") or "").strip()
                if get_session(sid, owner_id=session.owner_id) is None:
                    await websocket.send_json({"type": "error", "code": "session_not_found", "message": "未找到要重命名的会话。"})
                    continue
                if not title:
                    await websocket.send_json({"type": "error", "code": "invalid_session_title", "message": "会话名称不能为空。"})
                    continue
                if await _reject_session_mutation_while_running(websocket, session, "重命名会话", target_db_id=sid):
                    continue
                update_session(sid, title=title)
                await websocket.send_json({"type": "session_renamed", "session_id": sid, "title": title})
            elif msg_type == "session_set_workspace":
                from modus.desktop.db import get_workspace

                sid = str(msg.get("session_id") or "")
                workspace_id = str(msg.get("workspace_id") or "").strip()
                error_context = {"operation": "session_set_workspace", "requested_workspace_id": workspace_id}
                if not workspace_id:
                    await websocket.send_json({"type": "error", "code": "invalid_workspace", "message": "workspace_id 不能为空。", **error_context})
                    continue
                if sid and get_session(sid, owner_id=session.owner_id) is None:
                    await websocket.send_json({"type": "error", "code": "session_not_found", "message": "未找到要切换的会话。", **error_context})
                    continue
                target = get_workspace(workspace_id, owner_id=session.owner_id)
                if target is None:
                    await websocket.send_json({"type": "error", "code": "workspace_not_found", "message": "未找到该工作区，请先打开项目。", **error_context})
                    continue
                workspace = WorkspaceIdentity.from_record(target)
                if sid:
                    update_session(sid, workspace_id=workspace.workspace_id)
                if session.db_id == sid or not sid:
                    await _select_session_workspace(session, workspace.workspace_id)
                await websocket.send_json({
                    "type": "session_workspace_updated",
                    "workspace": workspace.to_wire(),
                    **error_context, **_session_identity(session),
                })
                await _broadcast_sessions_list(origin=session)
            elif msg_type == "session_set_mode":
                sid = str(msg.get("db_id") or msg.get("session_id") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                error_context = {
                    "operation": "session_set_mode",
                    "request_id": request_id, "requested_db_id": sid,
                    **_session_identity(session),
                }
                try:
                    mode = normalize_mode(msg.get("mode"), strict=True)
                except ValueError as exc:
                    await websocket.send_json({
                        "type": "error", "code": "invalid_mode", "message": str(exc),
                        **error_context,
                    })
                    continue
                if mode in {DEFAULT_MODE, MOA_MODE, PERI_MODE, AGI_MODE}:
                    targets_current = not sid or sid == session.db_id
                    if sid and get_session(sid, owner_id=session.owner_id) is None:
                        await websocket.send_json({
                            "type": "error", "code": "session_not_found",
                            "message": "未找到要更新的会话。", **error_context,
                        })
                        continue
                    if sid and not targets_current:
                        await websocket.send_json({
                            "type": "error", "code": "session_mismatch",
                            "message": "只能更新当前会话的协作模式。",
                            **error_context,
                        })
                        continue
                    if await _reject_session_mutation_while_running(
                        websocket, session, "切换会话模式",
                        target_db_id=sid or session.db_id,
                        error_context=error_context,
                    ):
                        continue
                    mode_config = _session_mode_snapshot(mode)
                    if mode in COLLABORATION_MODES and not _mode_roles_complete(mode, mode_config):
                        await websocket.send_json({
                            "type": "error", "code": "mode_not_configured",
                            "message": f"{mode.upper() if mode == MOA_MODE else 'Peri'} 尚未配置完整的 Host 与协作模型。",
                            **error_context,
                        })
                        continue
                    session_model_id = _host_model_id(mode_config, session.model_id)
                    if sid:
                        update_session(
                            sid, mode=mode, model_id=session_model_id,
                            mode_config=mode_config,
                        )
                    if targets_current:
                        session.mode = mode
                        session.mode_config = mode_config
                        session.model_id = session_model_id
                        await _rebuild_session_engine(session)
                    await websocket.send_json({
                        "type": "mode_updated", "updated_db_id": sid,
                        **error_context, **_session_identity(session),
                    })
                    await _broadcast_sessions_list(origin=session)
            elif msg_type == "memory_get":
                from modus.desktop.memory import get_memories
                mems = get_memories(session.db_id) if session.db_id else []
                await websocket.send_json({
                    "type": "memory_list", "session_id": session.db_id,
                    "memories": mems,
                })
            elif msg_type == "memory_add":
                from modus.desktop.memory import add_memory, get_memories
                fact = str(msg.get("fact") or "").strip()
                category = str(msg.get("category") or "general")
                scope = str(msg.get("scope") or "session")
                try:
                    if not fact:
                        raise ValueError("memory content is required")
                    if category not in {"general", "constraint", "preference", "fact", "reference"}:
                        raise ValueError("invalid memory category")
                    if scope not in {"session", "project"}:
                        raise ValueError("invalid memory scope")
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "code": "invalid_memory", "message": str(exc)})
                    continue
                await _persist_session_if_needed(websocket, session)
                add_memory(session.db_id, fact, category, scope=scope)
                await websocket.send_json({
                    "type": "memory_added", "session_id": session.db_id,
                    "memories": get_memories(session.db_id),
                })
            elif msg_type == "session_reference_add":
                from modus.desktop.memory import add_session_reference, get_memories

                source_id = str(msg.get("source_session_id") or "").strip()
                source_record = restore_session(
                    source_id, owner_id=session.owner_id,
                ) if source_id else None
                if source_record is None:
                    await websocket.send_json({
                        "type": "error", "code": "session_reference_not_found",
                        "message": "未找到要引用的会话。",
                    })
                    continue
                if source_id == session.db_id:
                    await websocket.send_json({
                        "type": "error", "code": "session_reference_self",
                        "message": "不能把当前会话引用到自身。",
                    })
                    continue
                if await _reject_session_mutation_while_running(
                    websocket, session, "引用会话记录",
                ):
                    continue
                if active_run_owner(source_id) is not None:
                    await websocket.send_json({
                        "type": "error", "code": "session_reference_busy",
                        "message": "来源会话仍在运行，请等待完成后再引用。",
                    })
                    continue
                if not session.db_id and not await _persist_session_if_needed(websocket, session):
                    await websocket.send_json({
                        "type": "error", "code": "session_reference_target_failed",
                        "message": "无法保存当前会话以添加引用。",
                    })
                    continue
                memories = get_memories(session.db_id)
                existing = next(
                    (
                        item for item in memories
                        if item.get("category") == "reference"
                        and source_id in (item.get("source_ids") or [])
                    ),
                    None,
                )
                if existing is None:
                    try:
                        existing = add_session_reference(
                            session.db_id, SessionDocument.from_record(source_record),
                        )
                    except ValueError as exc:
                        await websocket.send_json({
                            "type": "error", "code": "session_reference_failed",
                            "message": str(exc),
                        })
                        continue
                    created = True
                    memories = get_memories(session.db_id)
                else:
                    created = False
                await websocket.send_json({
                    "type": "session_reference_added", "created": created,
                    "session_id": session.db_id, "source_session_id": source_id,
                    "source_title": str(source_record.get("title") or "会话"),
                    "memory": existing, "memories": memories,
                })
            elif msg_type == "memory_archive":
                from modus.desktop.memory import archive_memory, get_memories
                memory_id = str(msg.get("memory_id") or "")
                if not session.db_id or not archive_memory(session.db_id, memory_id):
                    await websocket.send_json({
                        "type": "error", "code": "memory_not_found",
                        "message": "未找到当前会话中的这条记忆。",
                    })
                    continue
                await websocket.send_json({
                    "type": "memory_updated", "session_id": session.db_id,
                    "memories": get_memories(session.db_id),
                })
            elif msg_type == "memory_clear":
                from modus.desktop.memory import clear_memories
                if session.db_id:
                    clear_memories(session.db_id)
                await websocket.send_json({
                    "type": "memory_updated", "session_id": session.db_id,
                    "memories": [],
                })
            elif msg_type == "agent_config_get":
                memory = load_config().memory
                await websocket.send_json({
                    "type": "agent_config",
                    "memory": {
                        "auto_memorize": bool(memory.auto_memorize),
                        "retrieval_enabled": bool(memory.retrieval_enabled),
                        "max_retrieval_results": int(memory.max_retrieval_results),
                    },
                })
            elif msg_type == "agent_config_set":
                from modus.config import save_config_section
                memory_patch: dict[str, Any] = {}
                if "auto_memorize" in msg:
                    memory_patch["auto_memorize"] = bool(msg["auto_memorize"])
                if "retrieval_enabled" in msg:
                    memory_patch["retrieval_enabled"] = bool(msg["retrieval_enabled"])
                if "max_retrieval_results" in msg:
                    value = int(msg["max_retrieval_results"])
                    if not 1 <= value <= 50:
                        await websocket.send_json({
                            "type": "error", "code": "invalid_agent_config",
                            "message": "检索结果数量需在 1–50 之间。",
                        })
                        continue
                    memory_patch["max_retrieval_results"] = value
                if not memory_patch:
                    await websocket.send_json({
                        "type": "error", "code": "invalid_agent_config",
                        "message": "没有可保存的配置项。",
                    })
                    continue
                saved = save_config_section("memory", memory_patch)
                refreshed = load_config().memory
                await websocket.send_json({
                    "type": "agent_config_saved",
                    "memory": {
                        "auto_memorize": bool(refreshed.auto_memorize),
                        "retrieval_enabled": bool(refreshed.retrieval_enabled),
                        "max_retrieval_results": int(refreshed.max_retrieval_results),
                    },
                    "saved": saved,
                })
            elif msg_type == "artifact_get":
                from modus.desktop.artifacts import read_artifact_public

                artifact_id = str(msg.get("artifact_id") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                requested_session_id = str(msg.get("session_id") or "")
                response_identity = {
                    "operation": "artifact_get", "request_id": request_id,
                    "session_id": str(session.db_id or ""),
                    "requested_session_id": requested_session_id,
                    "artifact_id": artifact_id,
                }
                if not session.db_id:
                    await websocket.send_json({
                        "type": "error", **response_identity,
                        "message": "当前会话尚未持久化。",
                    })
                    continue
                try:
                    artifact = read_artifact_public(
                        artifact_id, session_id=session.db_id,
                    )
                    await websocket.send_json({
                        "type": "artifact_content", "artifact": artifact,
                        **response_identity,
                    })
                except ValueError as exc:
                    await websocket.send_json({
                        "type": "error", "message": str(exc),
                        **response_identity,
                    })
                except Exception as exc:
                    logger.exception("artifact read failed")
                    await websocket.send_json({
                        "type": "error",
                        "message": redact_text(str(exc)) or "artifact read failed",
                        **response_identity,
                    })
            elif msg_type == "peri_git_readiness":
                from modus.desktop import db
                from modus.desktop.git_readiness import inspect_git_readiness

                request_id = str(msg.get("request_id") or "")[:128]
                try:
                    workspace_root = (
                        session.workspace_root
                        or getattr(getattr(session, "engine", None), "cwd", None)
                        or str(Path.home())
                    )
                    requested = int(msg.get("worker_count") or 2)
                    configured_roles = session.mode_config if session.mode == PERI_MODE else _session_mode_snapshot(PERI_MODE)
                    configured_count = len([
                        role for role in ("worker_1", "worker_2") if role in configured_roles
                    ])
                    worker_count = configured_count or requested
                    workspace = Path(workspace_root)
                    readiness = await inspect_git_readiness(
                        workspace, worker_count=worker_count,
                        plan_id=f"preview-{session.id}", data_root=db.DB_DIR,
                    )
                    await websocket.send_json({
                        "type": "peri_git_readiness",
                        "operation": "peri_git_readiness", "request_id": request_id,
                        "readiness": readiness,
                    })
                except ValueError as exc:
                    await websocket.send_json({
                        "type": "error", "operation": "peri_git_readiness",
                        "request_id": request_id, "message": str(exc),
                    })
            elif msg_type == "skills_list":
                await websocket.send_json({"type": "skills_list", "skills": skill_repository.list_public()})
            elif msg_type == "skill_create":
                try:
                    if await _reject_shared_capability_mutation_while_running(
                        websocket, session, "保存 Skill",
                    ):
                        continue
                    skill = skill_repository.save(
                        name=str(msg.get("name") or ""),
                        description=str(msg.get("description") or ""),
                        prompt=str(msg.get("prompt") or ""),
                    )
                    await _broadcast_skills(
                        skill_repository.list_public(), origin=session,
                        extra={"skill": skill.to_wire()},
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            elif msg_type == "skill_delete":
                try:
                    if await _reject_shared_capability_mutation_while_running(
                        websocket, session, "删除 Skill",
                    ):
                        continue
                    skill_repository.delete(str(msg.get("name") or ""))
                    await _broadcast_skills(
                        skill_repository.list_public(), origin=session,
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            elif msg_type == "skill_fetch_url":
                url = str(msg.get("url") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                if not url.startswith(("http://", "https://")):
                    await websocket.send_json({
                        "type": "error", "operation": "skill_fetch_url",
                        "request_id": request_id,
                        "message": "URL 必须以 http:// 或 https:// 开头",
                    })
                else:
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=15) as client:
                            resp = await client.get(url, follow_redirects=True)
                            resp.raise_for_status()
                            text = resp.text[:100000]
                            name = url.rstrip("/").rsplit("/", 1)[-1].replace(".", "-")[:40] or "imported"
                            await websocket.send_json({
                                "type": "skill_fetched", "operation": "skill_fetch_url",
                                "request_id": request_id,
                                "content": text, "name": name, "source": url,
                            })
                    except Exception as exc:
                        await websocket.send_json({
                            "type": "error", "operation": "skill_fetch_url",
                            "request_id": request_id,
                            "message": f"获取失败: {redact_text(str(exc))}",
                        })
            elif msg_type == "extensions_list":
                await websocket.send_json({"type": "extensions_list", "extensions": extension_registry.list_public()})
            elif msg_type == "mcp_servers_list":
                await websocket.send_json({"type": "mcp_servers_list", "servers": mcp_manager.list_configs()})
            elif msg_type == "mcp_server_add":
                cfg = msg.get("config", {})
                try:
                    if await _reject_shared_capability_mutation_while_running(
                        websocket, session, "修改 MCP 配置",
                    ):
                        continue
                    if not isinstance(cfg, dict):
                        raise ValueError("MCP config must be an object")
                    mcp_config = McpServerConfig(
                        name=str(cfg.get("name", "")),
                        transport=str(cfg.get("transport", "stdio")),
                        command=str(cfg.get("command", "")),
                        args=list(cfg.get("args", [])),
                        url=str(cfg.get("url", "")),
                        env=dict(cfg.get("env", {})),
                        enabled=bool(cfg.get("enabled", True)),
                    )
                    mcp_manager.add_config(mcp_config)
                    rebuilt = await _rebuild_idle_session_engines_for_extensions()
                    await _broadcast_extensions(
                        origin=session, extra={"rebuilt_runtime_session_ids": rebuilt},
                    )
                except (TypeError, ValueError) as exc:
                    await websocket.send_json({
                        "type": "error", "code": "invalid_mcp_config",
                        "message": str(exc),
                    })
            elif msg_type == "mcp_server_remove":
                if await _reject_shared_capability_mutation_while_running(
                    websocket, session, "移除 MCP 服务器",
                ):
                    continue
                name = str(msg.get("name") or "")
                if not mcp_manager.remove_config(name):
                    await websocket.send_json({
                        "type": "error", "code": "mcp_server_not_found",
                        "message": "未找到要移除的 MCP 服务器。",
                    })
                    continue
                rebuilt = await _rebuild_idle_session_engines_for_extensions()
                await _broadcast_extensions(
                    origin=session, extra={"rebuilt_runtime_session_ids": rebuilt},
                )
            elif msg_type == "mcp_server_connect":
                if await _reject_shared_capability_mutation_while_running(
                    websocket, session, "连接 MCP 服务器",
                ):
                    continue
                name = str(msg.get("name") or "")
                if mcp_manager.get_config(name) is None:
                    await websocket.send_json({
                        "type": "error", "code": "mcp_server_not_found",
                        "message": "未找到要连接的 MCP 服务器。",
                    })
                    continue
                ok = await mcp_manager.connect_one(name)
                if not ok:
                    await websocket.send_json({
                        "type": "error", "code": "mcp_connect_failed",
                        "message": f"MCP 服务器「{name}」连接失败，请检查命令、地址和环境变量。",
                    })
                    continue
                rebuilt = await _rebuild_idle_session_engines_for_extensions()
                await _broadcast_extensions(
                    origin=session, extra={"rebuilt_runtime_session_ids": rebuilt},
                )
            elif msg_type == "mcp_server_disconnect":
                if await _reject_shared_capability_mutation_while_running(
                    websocket, session, "断开 MCP 服务器",
                ):
                    continue
                name = str(msg.get("name") or "")
                if mcp_manager.get_config(name) is None:
                    await websocket.send_json({
                        "type": "error", "code": "mcp_server_not_found",
                        "message": "未找到要断开的 MCP 服务器。",
                    })
                    continue
                await mcp_manager.disconnect_one(name)
                rebuilt = await _rebuild_idle_session_engines_for_extensions()
                await _broadcast_extensions(
                    origin=session, extra={"rebuilt_runtime_session_ids": rebuilt},
                )
            elif msg_type in {"models_list", "model_repository_get"}:
                # Browser-safe DTO: credentials never cross the WebSocket.
                await websocket.send_json({"type": "models_list", "data": model_repository.public_snapshot()})
            elif msg_type == "model_create":
                try:
                    if await _reject_session_mutation_while_running(websocket, session, "添加模型"):
                        continue
                    if await _reject_global_model_mutation_while_running(websocket, session, "添加模型"):
                        continue
                    created = model_repository.create(
                        name=str(msg.get("name") or ""), provider=str(msg.get("provider") or ""),
                        model=str(msg.get("model") or ""), api_key=str(msg.get("api_key") or ""),
                        base_url=msg.get("base_url") or None,
                        context_window=msg.get("context_window", 128_000),
                        max_output_tokens=msg.get("max_output_tokens", 8_192),
                        supports_tools=msg.get("supports_tools", True),
                        supports_images=msg.get("supports_images", False),
                        reasoning_efforts=msg.get("reasoning_efforts") or [],
                        default_reasoning_effort=msg.get("default_reasoning_effort"),
                    )
                    data = model_repository.public_snapshot()
                    rebuilt_sessions = await _bind_unconfigured_connected_sessions(data)
                    await _broadcast_model_repository(
                        data, origin=session, extra={
                            "model": created.to_wire(),
                            "rebuilt_runtime_session_ids": rebuilt_sessions,
                        },
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            elif msg_type == "model_update":
                try:
                    if await _reject_session_mutation_while_running(websocket, session, "更新模型配置"):
                        continue
                    if await _reject_global_model_mutation_while_running(websocket, session, "更新模型配置"):
                        continue
                    model_id = str(msg.get("id") or "")
                    changes = {key: msg[key] for key in (
                        "name", "provider", "model", "base_url", "api_key",
                        "context_window", "max_output_tokens", "supports_tools",
                        "supports_images", "reasoning_efforts", "default_reasoning_effort",
                    ) if key in msg}
                    updated = model_repository.update(model_id, **changes)
                    rebuilt_sessions = await _rebuild_connected_hosts_for_model(model_id)
                    await _broadcast_model_repository(
                        model_repository.public_snapshot(), origin=session,
                        extra={"model": updated.to_wire(), "rebuilt_runtime_session_ids": rebuilt_sessions},
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            elif msg_type == "model_test_connection":
                request_id = str(msg.get("request_id") or "")[:128]
                try:
                    result = await _test_model_connection(msg)
                    await websocket.send_json({
                        "type": "model_test_result", "request_id": request_id,
                        **result,
                    })
                except Exception as exc:
                    await websocket.send_json({
                        "type": "model_test_result", "request_id": request_id,
                        "success": False,
                        "error": redact_text(str(exc)),
                    })
            elif msg_type == "model_discover":
                model_id = str(msg.get("model_id") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                runtime_model = model_repository.runtime_model(model_id)
                if runtime_model is None:
                    await websocket.send_json({
                        "type": "error", "operation": "model_discover",
                        "request_id": request_id, "source_model_id": model_id,
                        "message": "unknown model_id",
                    })
                    continue
                try:
                    result = await discover_models(runtime_model)
                    session.model_discovery[model_id] = {
                        str(item["id"]): dict(item) for item in result["models"]
                    }
                    await websocket.send_json({
                        "type": "model_discovery_result", "operation": "model_discover",
                        "request_id": request_id,
                        **result,
                    })
                except ValueError as exc:
                    await websocket.send_json({
                        "type": "error", "operation": "model_discover",
                        "request_id": request_id, "source_model_id": model_id,
                        "message": str(exc),
                    })
            elif msg_type == "model_create_discovered":
                source_id = str(msg.get("source_model_id") or "")
                discovered_id = str(msg.get("discovered_model_id") or "")
                source = model_repository.runtime_model(source_id)
                discovered = session.model_discovery.get(source_id, {}).get(discovered_id)
                if source is None or discovered is None:
                    await websocket.send_json({
                        "type": "error",
                        "message": "发现结果已失效，请重新发现后再添加模型。",
                    })
                    continue
                try:
                    if await _reject_session_mutation_while_running(websocket, session, "添加模型"):
                        continue
                    if await _reject_global_model_mutation_while_running(websocket, session, "添加模型"):
                        continue
                    existing = model_repository.read()["models"]
                    if any(
                        item.get("provider") == source.get("provider")
                        and item.get("base_url") == source.get("base_url")
                        and item.get("model") == discovered_id
                        for item in existing
                    ):
                        raise ValueError("该 Endpoint 下的模型已在仓库中")
                    capability_values = {
                        "context_window": msg.get("context_window", 128_000),
                        "max_output_tokens": msg.get("max_output_tokens", 8_192),
                        "supports_tools": msg.get("supports_tools", True),
                        "supports_images": msg.get("supports_images", False),
                        "reasoning_efforts": msg.get("reasoning_efforts") or [],
                    }
                    created = model_repository.create(
                        name=str(msg.get("name") or discovered.get("name") or discovered_id),
                        provider=str(source["provider"]), model=discovered_id,
                        api_key=str(source.get("api_key") or ""),
                        base_url=source.get("base_url"),
                        default_reasoning_effort=msg.get("default_reasoning_effort"),
                        capability_sources={
                            field: "user_configuration" for field in CAPABILITY_FIELDS
                        },
                        **capability_values,
                    )
                    data = model_repository.public_snapshot()
                    rebuilt_sessions = await _bind_unconfigured_connected_sessions(data)
                    await _broadcast_model_repository(
                        data, origin=session, extra={
                            "model": created.to_wire(),
                            "rebuilt_runtime_session_ids": rebuilt_sessions,
                        },
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            elif msg_type == "model_delete":
                try:
                    if await _reject_session_mutation_while_running(websocket, session, "删除模型"):
                        continue
                    if await _reject_global_model_mutation_while_running(websocket, session, "删除模型"):
                        continue
                    deleted_id = str(msg.get("id") or "")
                    repairs_current = _session_references_model(session, deleted_id)
                    data = model_repository.delete(deleted_id)
                    if repairs_current:
                        repaired_roles = _repair_mode_snapshot_after_delete(
                            session.mode, session.mode_config, deleted_id, data,
                        )
                        if session.mode in COLLABORATION_MODES and _mode_roles_complete(session.mode, repaired_roles):
                            session.mode_config = repaired_roles
                            session.model_id = _host_model_id(repaired_roles, session.model_id)
                            _reset_invalid_reasoning(session)
                        else:
                            session.mode = DEFAULT_MODE
                            session.mode_config = {}
                            session.model_id = str(data["selection"].get("default_model_id") or "")
                            session.reasoning_effort = None
                        await _rebuild_session_engine(session)
                        if get_session(
                            session.db_id, owner_id=session.owner_id,
                        ) is not None:
                            update_session(
                                session.db_id, mode=session.mode, mode_config=session.mode_config,
                                model_id=session.model_id,
                                reasoning_effort=session.reasoning_effort or "",
                            )
                    repaired_session_ids = _repair_persisted_sessions_after_model_delete(
                        deleted_id, data, skip_id=session.db_id,
                    )
                    repaired_runtime_session_ids = await _repair_connected_sessions_after_model_delete(
                        deleted_id, data, skip_id=session.id,
                    )
                    await _broadcast_model_repository(
                        data, origin=session, extra={
                            "repaired_session_ids": repaired_session_ids,
                            "repaired_runtime_session_ids": repaired_runtime_session_ids,
                        },
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            elif msg_type == "model_select_default":
                try:
                    model_id = str(msg.get("model_id") or "")
                    if await _reject_global_model_mutation_while_running(websocket, session, "切换默认模型"):
                        continue
                    data = model_repository.set_default(model_id)
                    await _broadcast_model_repository(data, origin=session)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            elif msg_type == "session_set_model":
                sid = str(msg.get("session_id") or msg.get("db_id") or "")
                request_id = str(msg.get("request_id") or "")[:128]
                error_context = {
                    "operation": "session_set_model",
                    "request_id": request_id, "requested_db_id": sid,
                    **_session_identity(session),
                }
                try:
                    model_id = str(msg.get("model_id") or "")
                    targets_current = not sid or sid == session.db_id
                    if sid and get_session(sid, owner_id=session.owner_id) is None:
                        await websocket.send_json({
                            "type": "error", "code": "session_not_found",
                            "message": "未找到要更新的会话。", **error_context,
                        })
                        continue
                    if sid and not targets_current:
                        await websocket.send_json({
                            "type": "error", "code": "session_mismatch",
                            "message": "只能更新当前会话的模型。", **error_context,
                        })
                        continue
                    if model_repository.runtime_model(model_id) is None:
                        raise ValueError("unknown model id")
                    if await _reject_session_mutation_while_running(
                        websocket, session, "切换模型",
                        target_db_id=sid or session.db_id,
                        error_context=error_context,
                    ):
                        continue
                    session.mode = DEFAULT_MODE
                    session.mode_config = {}
                    session.model_id = model_id
                    session.reasoning_effort = None
                    await _rebuild_session_engine(session)
                    if get_session(
                        session.db_id, owner_id=session.owner_id,
                    ) is not None:
                        update_session(
                            session.db_id, mode=DEFAULT_MODE, mode_config={}, model_id=model_id,
                            reasoning_effort="",
                        )
                    await websocket.send_json({
                        "type": "session_model_updated", **error_context,
                        **_session_identity(session),
                    })
                    await _broadcast_sessions_list(origin=session)
                except ValueError as exc:
                    await websocket.send_json({
                        "type": "error", "code": "invalid_model",
                        "message": str(exc), **error_context,
                    })
            elif msg_type == "mode_models_set":
                try:
                    mode = normalize_mode(msg.get("mode"), strict=True)
                    roles = msg.get("roles")
                    if not isinstance(roles, dict):
                        raise ValueError("mode_models_set requires canonical role configuration")
                    if await _reject_session_mutation_while_running(
                        websocket, session, "更新协作模式模型配置",
                    ):
                        continue
                    if await _reject_global_model_mutation_while_running(
                        websocket, session, "更新协作模式模型配置",
                    ):
                        continue
                    data = model_repository.set_mode_configuration(mode, roles)
                    await _broadcast_model_repository(data, origin=session)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            elif msg_type == "interrupt":
                logger.info("interrupt requested")
                session.cancel_stream()
                break
            else:
                await websocket.send_json({"type": "error", "message": f"unknown message type: {msg_type}"})
    except WebSocketDisconnect:
        # When park_on_disconnect is enabled, keep the run executing detached
        # from the socket so a reconnect can resume from the durable ledger.
        # Otherwise cancel fail-closed and wait for the terminal settlement.
        parked = session.handle_disconnect(
            session.parked_emitter, session.active_controller,
        )
        if parked:
            logger.info("WebSocket disconnected; run parked for resume")
        else:
            task = session.active_run_task
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except (TimeoutError, asyncio.CancelledError):
                    logger.warning("background run did not stop promptly after disconnect")
                except Exception:
                    logger.debug("background run failed while closing disconnected socket", exc_info=True)
            logger.info("WebSocket disconnected; pending approvals denied")
    except Exception as exc:
        session.cancel_stream()
        logger.exception("websocket error")
        try:
            await websocket.send_json({"type": "error", "message": redact_text(str(exc))})
        except Exception:
            pass
    finally:
        manager.discard(session)
        _active_owner_id.reset(owner_context)
