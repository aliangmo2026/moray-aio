"""Ollama 本地大模型客户端。

MVP 阶段只留接口：Ollama 未启动 / 请求失败 / 输出解析失败时，
上层（loop/planner）自动回落启发式，服务本身不依赖 Ollama 存活。

SSRF 防护（fail-closed）：
- base_url 只接受 http/https 且必须 origin-only（无路径/凭据/query/fragment）；
- 主机必须命中运维显式配置的白名单（默认仅环回：127.0.0.1 / localhost / ::1，
  可用环境变量 MORAY_AIO_OLLAMA_ALLOWED_HOSTS 完全替换）——本产品定位就是
  连接运维自己部署的本地 LLM，白名单即「发请求前校验 host」的实现；
- 请求 URL 只由「校验过的 origin + 固定路径常量」拼出，任何外部输入不进入 URL。

网络弹性：POST 带指数退避重试（1s/2s/4s，最多 3 次），仅针对网络超时/连接错误/5xx；
4xx（如 400）不重试直接抛出。重试次数记录进 token 用量（trace 可见）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import FrozenSet, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.llm.prompts import ANALYZE_SYSTEM, ANALYZE_USER, PLAN_SYSTEM, PLAN_USER

logger = logging.getLogger("moray_aio.ollama")

ALLOWED_SCHEMES: FrozenSet[str] = frozenset({"http", "https"})
DEFAULT_ALLOWED_HOSTS: FrozenSet[str] = frozenset({"127.0.0.1", "localhost", "::1"})

# 固定路径常量：请求 URL = 校验过的 origin + 这两个常量之一
_TAGS_PATH = "/api/tags"
_GENERATE_PATH = "/api/generate"

# 指数退避：最多重试 3 次，间隔 1s / 2s / 4s
RETRY_DELAYS: Tuple[float, ...] = (1.0, 2.0, 4.0)


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
        # Token/重试用量按线程隔离：后台线程池并发跑 episode，避免互相覆盖
        self._tls = threading.local()

    def _endpoint(self, path: str) -> str:
        """拼接请求地址：仅接受固定路径常量；origin 已在 __init__ 校验协议与主机白名单。"""
        if path not in (_TAGS_PATH, _GENERATE_PATH):
            raise ValueError("unknown endpoint path: {!r}".format(path))
        return self._origin + path

    def is_available(self) -> bool:
        """探测 /api/tags；成功才缓存 True，失败不缓存、下次再试。"""
        if self._available:
            return True
        try:
            resp = httpx.get(self._endpoint(_TAGS_PATH), timeout=3)
        except httpx.HTTPError:
            return False
        if resp.status_code == 200:
            self._available = True
            return True
        return False

    def _post_with_retry(self, payload: dict) -> dict:
        """带指数退避的 POST。

        重试条件：网络超时、连接错误、5xx；4xx（如 400 Bad Request）不重试直接抛出。
        最多重试 3 次，间隔 1s / 2s / 4s；重试次数写入 thread-local 供 trace 归因。
        """
        attempt = 0
        while True:
            try:
                resp = httpx.post(
                    self._endpoint(_GENERATE_PATH), json=payload, timeout=self.timeout
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1
                if attempt > len(RETRY_DELAYS):
                    self._tls.last_retries = attempt - 1
                    raise
                logger.warning(
                    "ollama request failed (%s), retry %d/%d",
                    type(exc).__name__, attempt, len(RETRY_DELAYS),
                )
                time.sleep(RETRY_DELAYS[attempt - 1])
                continue
            if 500 <= resp.status_code <= 599 and attempt < len(RETRY_DELAYS):
                attempt += 1
                logger.warning(
                    "ollama HTTP %d, retry %d/%d", resp.status_code, attempt, len(RETRY_DELAYS)
                )
                time.sleep(RETRY_DELAYS[attempt - 1])
                continue
            resp.raise_for_status()  # 4xx/其余状态直接抛出，不重试
            self._tls.last_retries = attempt
            return resp.json()

    def _generate(self, prompt: str, system: str) -> dict:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }
        body = self._post_with_retry(payload)
        self._tls.last_usage = {
            "prompt_tokens": _as_int(body.get("prompt_eval_count")),
            "completion_tokens": _as_int(body.get("eval_count")),
            "retries": int(getattr(self._tls, "last_retries", 0) or 0),
        }
        return self._parse_json(body.get("response", ""))

    def take_last_usage(self) -> Optional[dict]:
        """取走并清空本线程最近一次生成的 token/重试用量（供 trace 打点归因）。"""
        usage = getattr(self._tls, "last_usage", None)
        self._tls.last_usage = None
        return usage

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

    def analyze(self, errors: List[str], extra: Optional[list] = None, lessons: Optional[list] = None,
                failed: Optional[list] = None, similar: Optional[list] = None) -> dict:
        prompt = ANALYZE_USER.format(
            lessons=_bullets(lessons),
            errors=_bullets(errors),
            extra=_bullets(extra),
            failed=_bullets(failed),
            similar_cases=_bullets(similar),
            n_classes=len(list(errors or [])),
        )
        return self._generate(prompt, ANALYZE_SYSTEM)

    def plan_tasks(
        self,
        errors: List[str],
        log_path: str,
        tools: str,
        existing: Optional[list] = None,
        lessons: Optional[list] = None,
        failed: Optional[list] = None,
    ) -> dict:
        prompt = PLAN_USER.format(
            lessons=_bullets(lessons),
            errors=_bullets(errors),
            existing=_bullets(existing),
            tools=tools,
            log_path=log_path,
            failed=_bullets(failed),
        )
        return self._generate(prompt, PLAN_SYSTEM)


def _bullets(items) -> str:
    items = list(items or [])
    if not items:
        return "(无)"
    return "\n".join("- {}".format(item) for item in items)


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
