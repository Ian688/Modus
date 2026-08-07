from __future__ import annotations

import json
import os
from contextlib import suppress
from copy import deepcopy       # 后面做配置合并时，需要深拷贝避免意外修改原始数据
from dataclasses import asdict, dataclass,field     #dataclass 转字典，后面做配置序列化时要用
from pathlib import Path
from typing import Any

from modus.paths import data_path

@dataclass(slots=True)
class LlmConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    api_key: str = ""
    base_url:str | None = None
    max_tokens:int = 8192
    temperature: float = 0.7
    timeout: float = 120.0
    supports_images: bool = False
    supports_tools: bool = True
    max_context_window: int = 128_000
    reasoning_effort: str | None = None
    # Retry transient provider failures (connect/timeout before any content
    # delta) once, budget-aware.  Content-bearing streams are never replayed.
    retry_transient: bool = True

@dataclass(slots=True)  #ToolsConfig — 哪些工具启用/禁用、超时时间、并发读上限
class ToolsConfig:
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    timeout: float = 60.0
    batch_timeout: float = 90.0
    max_concurrent_read: int = 4
    # Tool results longer than this are persisted as local artifacts and the
    # model receives a bounded head/tail payload instead of raw full text.
    tool_result_artifact_chars: int = 20_000
    # Hard cap on files a recursive scan (grep/search_code/glob) will walk.
    # Overrides the builtin default so large source trees are fully searchable.
    max_scan_files: int = 20_000

@dataclass(slots=True)  #长期记忆的开关和存储位置
class MemoryConfig:
    max_conversation_history: int = 100
    auto_memorize: bool = False
    retrieval_enabled: bool = True
    max_retrieval_results: int = 8

def _default_blacklist() -> list[str]:
    return [
        "sudo", "rm -rf /", "rm -rf ~", "mkfs",
        "dd if=/dev/zero", "shutdown", "reboot",
    ]

@dataclass(slots=True)  #命令黑名单（防误操作）、审计日志路径
class PolicyConfig:
    hitl_mode: str = "auto"
    path_guard_enabled: bool = True
    command_blacklist: list[str] = field(default_factory=_default_blacklist)
    audit_log_path: str = field(default_factory=lambda: str(data_path("audit.jsonl")))
    # Active capability grant for a run.  ``None`` (the default) grants every
    # declared capability — unrestricted, unchanged behavior.  An explicit list
    # (e.g. ["filesystem"] for a read-only lens) makes the executor deny every
    # tool whose declared capability is outside the set, before approval.
    capability_grant: list[str] | None = None


@dataclass(slots=True)
class SandboxConfig:
    """OS-level resource limits applied to shell subprocesses (RLIMIT)."""
    enabled: bool = False
    cpu_seconds: int = 60
    fsize_bytes: int = 10 * 1024 * 1024
    nofile: int = 1024

@dataclass(slots=True)      #PromptConfig 的 agent_mode 默认 react，以后可以扩展 plan 模式
class PromptConfig:
    personality: str = "default"
    agent_mode: str = "react"
    custom_prompt_paths:list[str] = field(default_factory=list)


@dataclass(slots=True)      #是为第 MOA 准备的——"用什么模型做聚合，调哪些模型做参考"现在就占好位，后面直接用
class MoaConfig:
    enabled: bool = False
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    references:list[dict[str,str]] = field(default_factory=list)

@dataclass(slots=True)      # context_compression 提成独立配置类。后面再定义 strategy 枚举和 trigger_tokens
class CompressionConfig:
    enabled: bool = True
    trigger_tokens: int = 80_000
    tail_messages: int = 8
    # When true, compaction asks the configured LLM to produce a real summary
    # of the omitted middle turns instead of a generic count message.  Falls
    # back to the count message when the model call fails or no API key is set.
    semantic: bool = True
    # Cap for the omitted-message text sent to the summarizer, in characters.
    semantic_input_chars: int = 24_000


@dataclass(slots=True)
class ConvergenceConfig:
    """Bounded, deterministic Peri review-loop convergence control."""
    enabled: bool = True
    max_revision_rounds: int = 3
    semantic_threshold: float = 0.90
    sprt_alpha: float = 0.10
    sprt_beta: float = 0.10
    min_sprt_samples: int = 4
    criteria_verification: bool = True
    sprt_min_ratio: float = 0.8
    max_recursion_depth: int = 0

@dataclass(slots=True)
class RuntimeConfig:
    max_turns: int = 20
    max_tokens: int = 200_000
    max_wall_seconds: float = 600.0
    max_verification_attempts: int = 3
    # Self-aware stall detection: stop when this many consecutive turns made
    # no progress (no text, no successful tool). 0 disables the check.
    no_progress_threshold: int = 4

@dataclass(slots=True)
class FeatureConfig:
    mcp: bool = True
    skill:bool = True
    memory: bool = True
    audit: bool = True
    compression: CompressionConfig = field(default_factory=CompressionConfig)
    convergence: ConvergenceConfig = field(default_factory=ConvergenceConfig)
    writable_workers: bool = False
    park_on_disconnect: bool = False
    billing: bool = False
    # ast-based diagnostics injected after editing Python files.
    lsp_diagnostics: bool = True
    # Self-adaptive loop: the reasoner reads turn_records trends and injects
    # bounded corrective hints (e.g. a repeated tool-error hotspot).  Off by
    # default so existing run behavior is unchanged.
    self_adapt: bool = False

@dataclass(slots=True)
class ModusConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)
    render_mode: str = "inline"
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    moa: MoaConfig = field(default_factory=MoaConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

# 配置系统的灵魂——load_config() 函数

# 读 JSON 配置文件，不存在或格式不对就返回 None，不抛异常
def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None

#手写 .env 解析器，不依赖 python-dotenv 库。支持 # 注释、支持引号去掉
def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for raw_lines in lines:
        line = raw_lines.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        result[key] = value
    return result

#  _deep_merge——配置合并的核心算法：
def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(target)
    for key, value in source.items():
        if value is None:
            continue
        old = result.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            result[key] = _deep_merge(old, value)
        else:
            result[key] = deepcopy(value)
    return result

#  _config_to_dict 和 _dict_to_config——在 dataclass 和字典之间转换

def _config_to_dict(config: ModusConfig) -> dict[str, Any]:
    return asdict(config)

def _dict_to_config(data: dict[str, Any]) -> ModusConfig:
    feature_data = dict(data.get("features", {}))
    compression_data = feature_data.get("compression", {})
    feature_data["compression"] = CompressionConfig(
        **compression_data if isinstance(compression_data, dict) else {}
    )
    convergence_data = feature_data.get("convergence", {})
    feature_data["convergence"] = ConvergenceConfig(
        **convergence_data if isinstance(convergence_data, dict) else {}
    )
    return ModusConfig(
        llm=LlmConfig(**data.get("llm", {})),
        render_mode=data.get("render_mode", "inline"),
        tools=ToolsConfig(**data.get("tools", {})),
        memory=MemoryConfig(**data.get("memory", {})),
        policy=PolicyConfig(**data.get("policy", {})),
        sandbox=SandboxConfig(**data.get("sandbox", {})),
        prompt=PromptConfig(**data.get("prompt", {})),
        features=FeatureConfig(**feature_data),
        moa=MoaConfig(**data.get("moa", {})),
        runtime=RuntimeConfig(**data.get("runtime", {})),
    )

# 最重要的函数——load_config() 主逻辑：
def load_config(
    project_root: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str | None] | None = None,
) -> ModusConfig:
    env_map = env if env is not None else os.environ
    data = _config_to_dict(ModusConfig())
    data["policy"]["audit_log_path"] = str(data_path("audit.jsonl", env_map))

    user_config = _read_json(data_path("config.json", env_map))
    if user_config:
        data = _deep_merge(data, user_config)

    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    project_config = _read_json(root / ".modus" / "config.json")
    if project_config:
        data = _deep_merge(data, project_config)
    project_env = _read_env(root / ".env")
    if project_env:
        data = _apply_env(data, project_env)

    if overrides:
        data = _deep_merge(data, overrides)

    data = _apply_env(data, env_map)
    config = _dict_to_config(data)
    config.policy.audit_log_path = _expand_home(config.policy.audit_log_path)
    return config

# _apply_env()——把环境变量映射到配置字段的函数
def _apply_env(data: dict[str, Any], env: dict[str, str | None]) -> dict[str, Any]:
    result = deepcopy(data)

    mappings: list[tuple[str, str, Any]] = [
        ("API_KEY", "llm.api_key", str),
        ("PROVIDER", "llm.provider", str),
        ("MODEL", "llm.model", str),
        ("BASE_URL", "llm.base_url", str),
        ("MAX_TOKENS", "llm.max_tokens", int),
        ("TEMPERATURE", "llm.temperature", float),
        ("TIMEOUT", "llm.timeout", float),
        ("SUPPORTS_IMAGES", "llm.supports_images", lambda v: v.lower() == "true"),
        ("SUPPORTS_TOOLS", "llm.supports_tools", lambda v: v.lower() == "true"),
        ("MAX_CONTEXT_WINDOW", "llm.max_context_window", int),
        ("REASONING_EFFORT", "llm.reasoning_effort", str),
        ("RETRY_TRANSIENT", "llm.retry_transient", lambda v: v.lower() == "true"),
        ("RENDER_MODE", "render_mode", str),
        ("AGENT_MODE", "prompt.agent_mode", str),
        ("MCP", "features.mcp", lambda v: v.lower() == "true"),
        ("SKILL", "features.skill", lambda v: v.lower() == "true"),
        ("MEMORY", "features.memory", lambda v: v.lower() == "true"),
        ("COMPRESSION", "features.compression.enabled", lambda v: v.lower() == "true"),
        ("COMPRESSION_TRIGGER_TOKENS", "features.compression.trigger_tokens", int),
        ("COMPRESSION_TAIL_MESSAGES", "features.compression.tail_messages", int),
        ("COMPRESSION_SEMANTIC", "features.compression.semantic", lambda v: v.lower() == "true"),
        ("COMPRESSION_SEMANTIC_INPUT_CHARS", "features.compression.semantic_input_chars", int),
        ("WRITABLE_WORKERS", "features.writable_workers", lambda v: v.lower() == "true"),
        ("PARK_ON_DISCONNECT", "features.park_on_disconnect", lambda v: v.lower() == "true"),
        ("BILLING", "features.billing", lambda v: v.lower() == "true"),
        ("LSP_DIAGNOSTICS", "features.lsp_diagnostics", lambda v: v.lower() == "true"),
        ("SELF_ADAPT", "features.self_adapt", lambda v: v.lower() == "true"),
        ("CONVERGENCE_ENABLED", "features.convergence.enabled", lambda v: v.lower() == "true"),
        ("MAX_REVISION_ROUNDS", "features.convergence.max_revision_rounds", int),
        ("CONVERGENCE_SEMANTIC_THRESHOLD", "features.convergence.semantic_threshold", float),
        ("CONVERGENCE_CRITERIA_VERIFICATION", "features.convergence.criteria_verification", lambda v: v.lower() == "true"),
        ("CONVERGENCE_SPRT_MIN_RATIO", "features.convergence.sprt_min_ratio", float),
        ("MAX_RECURSION_DEPTH", "features.convergence.max_recursion_depth", int),
        ("TOOLS_ENABLED", "tools.enabled", lambda v: v.split(",") if v else []),
        ("TOOLS_DISABLED", "tools.disabled", lambda v: v.split(",") if v else []),
        ("TOOLS_TIMEOUT", "tools.timeout", float),
        ("TOOLS_BATCH_TIMEOUT", "tools.batch_timeout", float),
        ("TOOLS_MAX_CONCURRENT_READ", "tools.max_concurrent_read", int),
        ("TOOLS_TOOL_RESULT_ARTIFACT_CHARS", "tools.tool_result_artifact_chars", int),
        ("TOOLS_MAX_SCAN_FILES", "tools.max_scan_files", int),
        ("MEMORY_MAX_CONVERSATION_HISTORY", "memory.max_conversation_history", int),
        ("MEMORY_AUTO_MEMORIZE", "memory.auto_memorize", lambda v: v.lower() == "true"),
        ("MEMORY_RETRIEVAL_ENABLED", "memory.retrieval_enabled", lambda v: v.lower() == "true"),
        ("MEMORY_MAX_RETRIEVAL_RESULTS", "memory.max_retrieval_results", int),
        ("POLICY_HITL_MODE", "policy.hitl_mode", str),
        ("POLICY_PATH_GUARD_ENABLED", "policy.path_guard_enabled", lambda v: v.lower() == "true"),
        ("POLICY_COMMAND_BLACKLIST", "policy.command_blacklist", lambda v: v.split(",") if v else []),
        ("POLICY_AUDIT_LOG_PATH", "policy.audit_log_path", str),
        ("POLICY_CAPABILITY_GRANT", "policy.capability_grant", lambda v: v.split(",") if v else None),
        ("SANDBOX_ENABLED", "sandbox.enabled", lambda v: v.lower() == "true"),
        ("SANDBOX_CPU_SECONDS", "sandbox.cpu_seconds", int),
        ("SANDBOX_FSIZE_BYTES", "sandbox.fsize_bytes", int),
        ("SANDBOX_NOFILE", "sandbox.nofile", int),
        ("RUN_MAX_TURNS", "runtime.max_turns", int),
        ("RUN_MAX_TOKENS", "runtime.max_tokens", int),
        ("RUN_MAX_WALL_SECONDS", "runtime.max_wall_seconds", float),
        ("RUN_MAX_VERIFICATION_ATTEMPTS", "runtime.max_verification_attempts", int),
        ("RUN_NO_PROGRESS_THRESHOLD", "runtime.no_progress_threshold", int),
    ]

    for suffix, config_path, caster in mappings:
        raw = env.get(f"MODUS_{suffix}")
        if raw in (None, ""):
            continue
        parts = config_path.split(".")
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        try:
            target[parts[-1]] = caster(raw)
        except (TypeError, ValueError):
            pass

    # 环境变量覆盖命令黑名单（JSON 格式，比逗号分隔更灵活）
    raw = env.get("MODUS_COMMAND_BLACKLIST_JSON")
    if raw:
        try:
            result.setdefault("policy", {})["command_blacklist"] = json.loads(raw)
        except json.JSONDecodeError:
            pass

    return result

def _expand_home(path: str) -> str:
    return str(Path(path).expanduser())

def get_config_paths(project_root: str | Path | None = None) -> list[Path]:
    paths = [data_path("config.json")]
    if project_root:
        paths.append(Path(project_root).resolve() / ".modus" / "config.json")
    return paths


def save_config_section(
    section: str,
    patch: dict[str, Any],
    *,
    env: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Deep-merge ``patch`` into one user-config section and persist to disk.

    Only the user-level ``~/.modus/config.json`` is written (never the project
    ``.modus/config.json``).  Returns the merged section that was persisted.
    """
    env_map = env if env is not None else os.environ
    path = data_path("config.json", env_map)
    data = _read_json(path) or {}
    merged = _deep_merge(data.get(section, {}) if isinstance(data.get(section), dict) else {}, patch)
    data[section] = merged
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return merged

