from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

class FailoverReason(Enum):
    """API 调用失败的原因 —— 决定恢复策略"""
    auth = "auth"
    auth_permanent = "auth_permanent"
    billing = "billing"
    rate_limit = "rate_limit"
    overloaded = "overloaded"
    server_error = "server_error"
    timeout = "timeout"
    context_overflow = "context_overflow"
    model_not_found = "model_not_found"
    format_error = "format_error"
    unknown = "unknown"

@dataclass
class ClassifiedError:
    """错误分类结果，附带恢复建议"""
    reason: FailoverReason
    retryable: bool
    message: str = ""
    status_code: int = 0

def classify_api_error(error: Exception, status_code: int = 0) -> ClassifiedError:
    """根据异常类型和 HTTP 状态码分类 API 错误"""
    message = str(error).lower()

    # Auth / 权限
    if status_code in (401, 403):
        # 403 也可能是内容安全策略
        if "content" in message or "safety" in message or "policy" in message:
            return ClassifiedError(FailoverReason.format_error, False, str(error), status_code)
        return ClassifiedError(FailoverReason.auth, True, str(error), status_code)

    # 计费 / 配额
    if status_code == 402 or "insufficient_quota" in message or "billing" in message:
        return ClassifiedError(FailoverReason.billing, False, str(error), status_code)
    if status_code == 429 or "rate_limit" in message or "too many" in message:
        return ClassifiedError(FailoverReason.rate_limit, True, str(error), status_code)

    # 服务端
    if status_code in (503, 529):
        return ClassifiedError(FailoverReason.overloaded, True, str(error), status_code)
    if status_code in (500, 502):
        return ClassifiedError(FailoverReason.server_error, True, str(error), status_code)

    # 上下文溢出
    if status_code == 413 or "context_length" in message or "too large" in message or "maximum context" in message:
        return ClassifiedError(FailoverReason.context_overflow, False, str(error), status_code)

    # 模型不存在
    if status_code == 404 or "model_not_found" in message or "not found" in message:
        return ClassifiedError(FailoverReason.model_not_found, False, str(error), status_code)

    # 请求格式
    if status_code == 400:
        return ClassifiedError(FailoverReason.format_error, False, str(error), status_code)

    # 超时
    if isinstance(error, TimeoutError) or "timeout" in message:
        return ClassifiedError(FailoverReason.timeout, True, str(error), status_code)

    return ClassifiedError(FailoverReason.unknown, True, str(error), status_code)