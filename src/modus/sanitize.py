import re
from typing import Any

# 非法字符 UTF-8 代理对：\ud800-\udfff
# 这些字符在 json.dumps 时会崩溃，必须先替换掉
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

# 字符替换
def sanitize_surrogates(text: str) -> str:
    """将非法代理对替换为 U+FFFD(替换字符)"""
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("\ufffd", text)
    
    return text

# 加嵌套结构清洗：
def sanitize_structure_surrogates(payload: Any) -> bool:
    """递归清洗嵌套 dict/list 中的代理对，返回是否替换过"""
    found = False

    def _walk(node: Any) -> None:
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[key] = _SURROGATE_RE.sub("\ufffd", value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[idx] = _SURROGATE_RE.sub("\ufffd", value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)

    _walk(payload)
    return found

def sanitize_messages_surrogates(messages: list[dict[str, Any]]) -> bool:
    """清洗 messages 列表中所有字符串字段的代理对"""
    found = False
    for msg in messages:
        if sanitize_structure_surrogates(msg):
            found = True
        # 特别处理 content 字段可能是 list[dict] 的情况
        content = msg.get("content")
        if isinstance(content, list):
            if sanitize_structure_surrogates(content):
                found = True
        # 处理 tool_calls 里的 arguments 字符串
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                fn = tc.get("function", {})
                args = fn.get("arguments")
                if isinstance(args, str) and _SURROGATE_RE.search(args):
                    fn["arguments"] = _SURROGATE_RE.sub("\ufffd", args)
                    found = True
    return found

