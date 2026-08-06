from __future__ import annotations

import json
from typing import Any

# 会修改文件的工具名
_FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "edit_file", "patch"})

def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """确认文件操作是否真的写入了，不是光返回了'成功'"""
    if tool_name not in _FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    # The built-in edit/write tools intentionally return concise human-readable
    # summaries. Keep this verifier aligned with that public result contract.
    text = result.strip()
    if tool_name == "write_file" and text.startswith("Wrote "):
        return True
    if tool_name == "edit_file" and text.startswith("Edited "):
        return True
    try:
        data = json.loads(text)
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "edit_file":
        return data.get("success") is True
    if tool_name == "patch":
        return data.get("success") is True
    return False
