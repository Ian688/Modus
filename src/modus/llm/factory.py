from __future__ import annotations

from modus.config import LlmConfig
from modus.llm.openai_compatible import OpenAICompatibleClient

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

def create_llm_client(config: LlmConfig) -> OpenAICompatibleClient:
    provider = config.provider.lower()
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
            prompt_cache=True,
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
        supports_images=config.supports_images,
        supports_tools=config.supports_tools,
        reasoning_effort=config.reasoning_effort,
    )
