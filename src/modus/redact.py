from __future__ import annotations

import re
from typing import Any

# 已知 API key 前缀模式 —— 识别常见平台的 token 格式
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter
    r"sk-ant-[A-Za-z0-9_-]{10,}",        # Anthropic
    r"github_pat_[A-Za-z0-9_]{10,}",     # GitHub PAT (fine-grained)
    r"ghp_[A-Za-z0-9]{10,}",             # GitHub PAT (classic)
    r"xox[baprs]-[A-Za-z0-9-]{10,}",     # Slack tokens
]

# 敏感 URL 参数名（精确匹配，不是子串匹配）
_SENSITIVE_QUERY_PARAMS = frozenset({
    "access_token", "refresh_token", "id_token", "token",
    "api_key", "apikey", "client_secret", "password",
    "auth", "jwt", "secret", "key", "code", "signature",
})

# 敏感 body key 名
_SENSITIVE_BODY_KEYS = frozenset({
    "access_token", "refresh_token", "id_token", "token",
    "api_key", "apikey", "client_secret", "password",
    "auth", "jwt", "secret", "private_key", "authorization", "key",
})

# 短 token 完全掩码，长 token 保留前 6 后 4
_SHORT_TOKEN_THRESHOLD = 18

_SENSITIVE_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:" + "|".join(re.escape(name) for name in sorted(_SENSITIVE_QUERY_PARAMS)) + r")=)([^&#\s]*)"
)

_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(\b(?:"
    + "|".join(re.escape(name) for name in sorted(_SENSITIVE_BODY_KEYS))
    + r")\b\s*[:=]\s*)([^\s,;&#]+)"
)

def _mask_token(token: str) -> str:
    if len(token) < _SHORT_TOKEN_THRESHOLD:
        return "***"
    return token[:6] + "***" + token[-4:]

def redact_text(text: str) -> str:
    """Mask known credential formats and secret-bearing URL parameters."""
    for pattern in _PREFIX_PATTERNS:
        text = re.sub(pattern, lambda m: _mask_token(m.group(0)), text)
    text = _SENSITIVE_QUERY_PATTERN.sub(lambda match: match.group(1) + "***", text)
    text = _SENSITIVE_ASSIGNMENT_PATTERN.sub(lambda match: match.group(1) + "***", text)
    return text

def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value

def redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive fields and strings in nested containers."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in _SENSITIVE_BODY_KEYS:
            result[key] = "***"
        else:
            result[key] = _redact_value(value)
    return result
