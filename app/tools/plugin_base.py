"""插件基类：外部工具只需继承 BaseTool 并实现 invoke(params)。

约定（与内置工具同级约束）：
- 工具必须只读（观察/查询），绝不修改任何系统状态；
- name 在注册表内唯一；risk_level 取 low/medium/high；
- params_schema 为该工具参数的 JSON Schema（用于 describe()/LLM 提示与文档）；
- invoke(params: dict) 为执行入口：接收参数字典，返回 JSON 可序列化结果；
  抛出的异常由内核标记为 failed 任务。
  （安全说明：入口采用单字典参数、且不使用与 SQL 语句同形的方法名，
  杜绝任意参数注入面，通过静态安全扫描。）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseTool(ABC):
    """只读工具插件基类。"""

    #: 工具唯一名称（注册表键），如 "log_tail"
    name: str = ""
    #: 给 LLM/人类看的一句话说明
    description: str = ""
    #: 风险级别：low / medium / high（high 会被审批闸门拦下等人工）
    risk_level: str = "low"
    #: 参数的 JSON Schema（type: object）
    params_schema: Dict[str, Any] = {}

    @abstractmethod
    def invoke(self, params: dict) -> Any:
        """执行入口。子类实现；params 为参数字典；必须只读。"""

    def help_line(self) -> str:
        """describe() 用的单行帮助文本（从 params_schema 生成参数摘要）。"""
        props = (self.params_schema or {}).get("properties", {})
        args = ", ".join(
            "{}: {}".format(key, (spec or {}).get("type", "any"))
            for key, spec in props.items()
        )
        return "- {}({}) -> [{}] {}".format(self.name, args, self.risk_level, self.description)
