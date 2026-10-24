"""Ollama 本地大模型客户端。

MVP 阶段只留接口：Ollama 未启动 / 请求失败 / 输出解析失败时，
上层（loop/planner）自动回落启发式，服务本身不依赖 Ollama 存活。

SSRF 防护（fail-closed）：
- base_url 只接受 http/https 且必须 origin-only（无路径/凭据/query/fragment）；
- 主机必须命中显式白名单（默认仅环回：127.0.0.1 / localhost / ::1，
  可用环境变量 MORAY_AIO_OLLAMA_ALLOWED_HOSTS 完全替换）；
- 请求 URL 只由「校验过的 origin + 固定路径常量」拼出，任何外部输入不进入 URL。
"""

from __future__ import annotations

import json
import re
from typing import FrozenSet, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.llm.prompts import ANALYZE_SYSTEM, ANALYZE_USER, PLAN_SYSTEM, PLAN_USER

ALLOWED_SCHEMES: FrozenSet[str] = frozenset({"http", "https"})
DEFAULT_ALLOWED_HOSTS: FrozenSet[str] = frozenset({"127.0.0.1", "localhost", "::1"})

# 固定路径常量：请求 URL = 校验过的 origin + 这两个常量之一
_TAGS_PATH = "/api/tags"
_GENERATE_PATH = "/api/generate"


def _validate_origin(base_url: str) -> Tuple[str, str, Optional[int]]:
    """校验并拆出 (scheme, host, port)；任何不合形抛 ValueError。"""
    parts = urlsplit(str(base_url).strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("ollama_url scheme must be http/https: {!r}".format(base_url))
    host = parts.hostname
    if not host:
        raise ValueError("ollama_url host missing: {!r}".format(base_url))
    if parts.username or parts.password:
        raise ValueError("ollama_url must not contain credentials: {!r}".format(base_url))
    if parts.query or parts.fragment:
        raise ValueError("ollama_url must be origin-only: {!r}".format(base_url))
    if parts.path not in ("", "/"):
        raise ValueError("ollama_url must be origin-only (no path): {!r}".format(base_url))
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("ollama_url has invalid port: {!r}".format(base_url)) from exc
    return scheme, host, port


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: int = 120, allowed_hosts=None):
        scheme, host, port = _validate_origin(base_url)
        whitelist = frozenset(h.strip().lower() for h in (allowed_hosts or DEFAULT_ALLOWED_HOSTS) if h)
        if host.lower() not in whitelist:
            raise ValueError(
                "ollama host {!r} is not in the allowed hosts whitelist {}".format(host, sorted(whitelist))
            )
        self._origin = "{}://{}{}".format(
            scheme, ("[{}]".format(host) if ":" in host else host), (":{}".format(port) if port else "")
        )
        self.model = model
        self.timeout = int(timeout)
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        """探测 /api/tags；成功才缓存 True，失败不缓存、下次再试。"""
        if self._available:
            return True
        try:
            resp = httpx.get(self._origin + _TAGS_PATH, timeout=3)
        except httpx.HTTPError:
            return False
        if resp.status_code == 200:
            self._available = True
            return True
        return False

    def _generate(self, prompt: str, system: str) -> dict:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }
        resp = httpx.post(self._origin + _GENERATE_PATH, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        return self._parse_json(body.get("response", ""))

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """解析模型输出；处理 ```json 包裹；失败返回 {"raw": ..., "parse_error": True}。"""
        text = (raw or "").strip()
        if not text:
            return {"raw": raw, "parse_error": True}
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"raw": raw, "parse_error": True}
        return parsed if isinstance(parsed, dict) else {"raw": raw, "parse_error": True}

    def analyze(self, errors: List[str], extra: Optional[list] = None, lessons: Optional[list] = None) -> dict:
        prompt = ANALYZE_USER.format(
            lessons=_bullets(lessons),
            errors=_bullets(errors),
            extra=_bullets(extra),
        )
        return self._generate(prompt, ANALYZE_SYSTEM)

    def plan_tasks(
        self,
        errors: List[str],
        log_path: str,
        tools: str,
        existing: Optional[list] = None,
        lessons: Optional[list] = None,
    ) -> dict:
        prompt = PLAN_USER.format(
            lessons=_bullets(lessons),
            errors=_bullets(errors),
            existing=_bullets(existing),
            tools=tools,
            log_path=log_path,
        )
        return self._generate(prompt, PLAN_SYSTEM)


def _bullets(items) -> str:
    items = list(items or [])
    if not items:
        return "(无)"
    return "\n".join("- {}".format(item) for item in items)
