"""示例插件：k8s_pod_check —— 自定义只读探针（Mock 实现）。

演示如何为 moray-aio 编写「系统探针类」插件：本例模拟 K8s Pod 健康检查。
真实实现建议用 `kubectl get pods --no-headers -n <ns>` 之类的**只读**命令
（list 形式 + shell=False）或 K8s API 的 GET 查询；本示例为纯 Mock，
不发起任何网络请求、不修改任何集群状态。

用法：
    set MORAY_AIO_PLUGIN_DIRS=examples
    python cli.py run --trigger recent_errors
工具 k8s_pod_check 即会出现在注册表中（planner/LLM 可选用）。
"""

from __future__ import annotations

from app.tools.plugin_base import BaseTool


class K8sPodCheckTool(BaseTool):
    name = "k8s_pod_check"
    description = "[只读探针-示例] 检查 K8s Pod 状态（Mock：返回样例数据，演示插件写法）"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "description": "命名空间，默认 default"},
            "pod_pattern": {"type": "string", "description": "Pod 名过滤（可选，示例忽略）"},
        },
    }

    def invoke(self, params: dict) -> dict:
        namespace = str(params.get("namespace") or "default")
        # Mock：只读样例数据。真实实现应替换为 kubectl/API 的只读查询。
        return {
            "namespace": namespace,
            "source": "mock",
            "pods": [
                {"name": "redis-0", "ready": "0/1", "status": "CrashLoopBackOff", "restarts": 7},
                {"name": "api-7d9f", "ready": "1/1", "status": "Running", "restarts": 0},
                {"name": "api-2b1c", "ready": "1/1", "status": "Running", "restarts": 0},
            ],
            "note": "Mock 实现：仅供演示插件机制；探针只读，不改集群状态",
        }
