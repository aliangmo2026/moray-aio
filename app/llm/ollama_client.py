"""Ollama local-LLM client.

Interface-first by design: when Ollama is down / requests fail / output cannot be
parsed, the upper layers (loop/planner) automatically fall back to heuristics —
the service never depends on Ollama being alive.

SSRF protection (fail-closed):
- base_url must be http/https and origin-only (no path/credentials/query/fragment);
- the host must hit an operator-configured whitelist (loopback only by default:
  127.0.0.1 / localhost / ::1, fully replaceable via MORAY_AIO_OLLAMA_ALLOWED_HOSTS)
  — this product's purpose is to reach the operator's own local LLM, and the
  whitelist implements "validate the host before every request";
- request URLs are always "validated origin + fixed path constants"; no external
  input ever flows into the URL.

Network resilience: POSTs retry with exponential backoff (1s/2s/4s, at most 3
retries) for timeouts/connection errors/5xx; 4xx (e.g. 400) raises immediately
without retry. The retry count is recorded in the token usage (visible in trace).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.llm.prompts import ANALYZE_SYSTEM, ANALYZE_USER, PLAN_SYSTEM, PLAN_USER

logger = logging.getLogger("moray_aio.ollama")

ALLOWED_SCHEMES: FrozenSet[str] = frozenset({"http", "https"})
DEFAULT_ALLOWED_HOSTS: FrozenSet[str] = frozenset({"127.0.0.1", "localhost", "::1"})

# Fixed path constants: request URL = validated origin + one of these
_TAGS_PATH = "/api/tags"
_GENERATE_PATH = "/api/generate"

# Exponential backoff: at most 3 retries, 1s / 2s / 4s
RETRY_DELAYS: Tuple[float, ...] = (1.0, 2.0, 4.0)


def _validate_origin(base_url: str) -> Tuple[str, str, Optional[int]]:
    """Validate and split (scheme, host, port); anything malformed raises ValueError."""
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
    """Thin client over the Ollama REST API with usage tracking and retries."""

    def __init__(self, base_url: str, model: str, timeout: int = 120, allowed_hosts=None):
        scheme, host, port = _validate_origin(base_url)
        whitelist = frozenset(
            h.strip().lower() for h in (allowed_hosts or DEFAULT_ALLOWED_HOSTS) if h
        )
        if host.lower() not in whitelist:
            raise ValueError(
                "ollama host {!r} is not in the allowed hosts whitelist {}".format(
                    host, sorted(whitelist)
                )
            )
        self._origin = "{}://{}{}".format(
            scheme,
            ("[{}]".format(host) if ":" in host else host),
            (":{}".format(port) if port else ""),
        )
        self.model = model
        self.timeout = int(timeout)
        self._available: Optional[bool] = None
        # Token/retry usage is thread-local: background workers run episodes
        # concurrently and must not overwrite each other's attribution
        self._tls = threading.local()

    def _endpoint(self, path: str) -> str:
        """Build a request URL: only fixed path constants are accepted; the origin
        was validated (scheme + host whitelist) in __init__."""
        if path not in (_TAGS_PATH, _GENERATE_PATH):
            raise ValueError("unknown endpoint path: {!r}".format(path))
        return self._origin + path

    def is_available(self) -> bool:
        """Probe /api/tags; only success is cached as True, failures re-probe next time."""
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

    def _post_with_retry(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST with exponential backoff.

        Retries on: network timeouts, connection errors, 5xx; 4xx (e.g. 400) raises
        immediately without retry. At most 3 retries with 1s / 2s / 4s delays; the
        retry count lands in thread-local state for trace attribution.
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
            resp.raise_for_status()  # 4xx and anything else raise immediately
            self._tls.last_retries = attempt
            return resp.json()

    def _generate(self, prompt: str, system: str) -> Dict[str, Any]:
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

    def take_last_usage(self) -> Optional[Dict[str, Any]]:
        """Take (and clear) this thread's latest token/retry usage for trace attribution."""
        usage = getattr(self._tls, "last_usage", None)
        self._tls.last_usage = None
        return usage

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        """Parse model output; unwraps ```json fences; failures return
        {"raw": ..., "parse_error": True}."""
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

    def analyze(
        self,
        errors: List[str],
        extra: Optional[list] = None,
        lessons: Optional[list] = None,
        failed: Optional[list] = None,
        similar: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Analyze clustered errors and produce summary/causes/gaps/conclusion."""
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
    ) -> Dict[str, Any]:
        """Propose the next batch of read-only investigation tasks."""
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
        return "(none)"
    return "\n".join("- {}".format(item) for item in items)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
