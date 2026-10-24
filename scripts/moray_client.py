"""moray-aio HTTP 客户端辅助（仅标准库）：统一携带 X-API-Key 鉴权头。

外部系统（或本仓库脚本）调用 moray-aio HTTP API 时使用：
密钥只从环境变量 MORAY_AIO_API_KEY 读取（绝不硬编码进源码/示例/测试），
非空时随请求头发送。
"""

from __future__ import annotations

import os

API_KEY_HEADER = "X-API-Key"


def api_key_from_env() -> str:
    """从环境变量读取 API 密钥（空串表示服务端未开启鉴权）。"""
    return os.environ.get("MORAY_AIO_API_KEY", "").strip()


def build_headers(extra: dict = None) -> dict:
    """构造请求头：合并调用方附加头，并在配置了密钥时带上 X-API-Key。"""
    headers = dict(extra or {})
    key = api_key_from_env()
    if key:
        headers[API_KEY_HEADER] = key
    return headers
