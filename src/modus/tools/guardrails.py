from __future__ import annotations

from typing import Any

# 幂等工具 —— 反复调用产生相同结果，不会产生副作用
_IDEMPOTENT_TOOL_NAMES = frozenset({
    "read_file", "glob", "grep", "list_dir",
    "web_search", "web_fetch", "search_code",
})

# 突变工具 —— 会修改外部状态
_MUTATING_TOOL_NAMES = frozenset({
    "write_file", "edit_file", "bash", "run_tests", "execute_command",
    "save_memory", "revert_turn",
})

def tool_may_have_side_effect(tool_name: str) -> bool:
    """工具是否可能有副作用"""
    return tool_name not in _IDEMPOTENT_TOOL_NAMES
