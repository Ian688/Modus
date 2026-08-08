from __future__ import annotations

from modus.config import LlmConfig
from modus.llm.cache import PROMPT_CACHE_FULL, PROMPT_CACHE_OFF
from modus.llm.openai_compatible import OpenAICompatibleClient

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

# Prompt-cache mode for clients built through ``create_llm_client``.
# ``off|basic|full`` (full = breakpoint edition: static system block + first/
# last user-message cache breakpoints).  This is a module constant for now;
# once the config surface is stabilized the value moves to ``LlmConfig`` and
# this lookup is replaced by ``config.llm.prompt_cache``.
DEFAULT_PROMPT_CACHE = PROMPT_CACHE_FULL


def _prompt_cache_mode(config: LlmConfig) -> str:
    """Resolve the prompt-cache mode, preferring an explicit config field."""
    configured = getattr(config, "prompt_cache", None)
    if configured is None:
        return DEFAULT_PROMPT_CACHE
    if configured is True:
        return PROMPT_CACHE_FULL
    if configured is False:
        return PROMPT_CACHE_OFF
    return str(configured).lower()


def _prompt_cache_enabled(mode: str) -> bool:
    return str(mode or "").lower() in {"basic", "full"}


def create_llm_client(config: LlmConfig) -> OpenAICompatibleClient:
    provider = config.provider.lower()
    cache_mode = _prompt_cache_mode(config)
    if provider == "deepseek":
        base_url = config.base_url or DEEPSEEK_BASE_URL
        return OpenAICompatibleClient(
            provider_name="deepseek",
            model=config.model,
            api_key=config.api_key,
            base_url=base_url,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
            max_context_window=config.max_context_window,
            prompt_cache=_prompt_cache_enabled(cache_mode),
            enable_prompt_cache=_prompt_cache_enabled(cache_mode),
            supports_images=config.supports_images,
            supports_tools=config.supports_tools,
            reasoning_effort=config.reasoning_effort,
        )
    return OpenAICompatibleClient(
        provider_name=provider,
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url or OPENAI_BASE_URL,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        timeout=config.timeout,
        max_context_window=config.max_context_window,
        prompt_cache=False,
        enable_prompt_cache=False,
        supports_images=config.supports_images,
        supports_tools=config.supports_tools,
        reasoning_effort=config.reasoning_effort,
    )
