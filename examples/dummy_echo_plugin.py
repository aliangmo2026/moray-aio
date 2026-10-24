"""示例插件：dummy_echo —— 演示外部如何编写并加载 BaseTool 插件。

用法（在仓库根目录）：
    set MORAY_AIO_PLUGIN_DIRS=examples
    python cli.py run --trigger recent_errors     # 或启动服务
工具 dummy_echo 会出现在工具注册表中，可被 planner/LLM 选用。

注意：插件与内置工具同级约束——必须只读、无副作用。
"""

from __future__ import annotations

from app.tools.plugin_base import BaseTool


class DummyEchoTool(BaseTool):
    name = "dummy_echo"
    description = "示例插件：原样回显文本（演示插件加载机制，无任何副作用）"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要回显的文本"},
        },
        "required": ["text"],
    }

    def invoke(self, params: dict) -> dict:
        # 只读演示：截断到 256 字符，防止撑爆 episode 载荷
        text = str(params.get("text", ""))
        return {"echo": text[:256]}
