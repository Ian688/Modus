from __future__ import annotations  # 让所有类型注解变成惰性求值的字符串，这样类可以引用后面定义的类型，不会报循环导入。使用 Literal 时，强烈建议使用

from dataclasses import dataclass, field    # 如果不引用导入的函数，函数下方会出现波浪线〰️
from typing import Any, Literal     # Any 来自 typing 模块。它的意思是 “关闭类型检查”。

Role = Literal["system","user","assistant","tool"]  # Role 是 Literal 类型——告诉编辑器 role 字段只能取这 4 个值，和 OpenAI Chat API 的角色一一对应
StopReason = Literal["end_turn","tool+use","max_tokens","stop_squence"] # StopReason 定义了一次 LLM 调用结束的原因

@dataclass(slots=True)  # 自动生成 __init__、__repr__ 等方法，slots=True 限制实例动态添加属性，更省内存
class Message:
    role: Role
    content: str | list[dict[str, Any]]
    name: str | None = None
    tool_call_id: str | None = None     # 可以是 str,可以是 None。不传参数时，默认是 None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # 默认值是列表，调用函数时（在实例化对象时，即执行 类名() 的时候），生成一个新列表

@dataclass(slots=True)
class Usage:
    input_tokens: int = 0   # “显式优于隐式”和“处理数值而非状态” 可以设为 0，这样可以保证未查询成功时的逻辑，避免 NoneType 错误/null 错误
    output_tokens:int = 0

@dataclass(slots=True)
class QueryResult:
    text: str
    total_tokens: int   # 如果要设置默认值 0，就不可以放在未设置默认值的参数后面。其次，强制 fetch/解析数据，是业务逻辑上的保护措施，避免使用默认值 0，造成灾难
    turns: int
    # Future-proofing: a future AGI may produce structured or multimodal
    # content.  ``content`` mirrors the OpenAI-style content array (text/image/
    # audio parts); ``artifacts`` carries structured output produced alongside.
    content: str | list[dict[str, Any]] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)




