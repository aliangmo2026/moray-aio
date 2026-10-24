"""告警推送器：把「值得人类知道」的事件推送到终端 / Webhook / 本地文件。

硬约束：
- Webhook URL 仅允许 http/https，且 host 必须命中运维显式配置的域名白名单；
  每次请求前校验协议/host/白名单，并解析域名校验每个地址都是全局地址
  （拒绝 localhost、环回、私有和保留地址，防 DNS rebinding）；禁用重定向；
- 密钥/凭据只从环境变量读取（webhook URL/白名单本身来自 Settings/环境变量）；
- FileNotifier 只追加写自己的 alerts 文件（默认 data/alerts.log），
  绝不碰被监控的日志；
- 多 Notifier 扇出互相隔离：单个失败只记录日志，不影响其他。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("moray_aio.notifier")

_ALLOWED_SCHEMES = ("http", "https")


def _assert_global_addresses(host: str) -> None:
    """解析并校验地址边界：literal IP 与域名解析出的每个地址都必须 is_global。"""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError(
                "webhook_url must not target loopback/private/reserved addresses: {!r}".format(host)
            )
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ValueError("webhook_url host cannot be resolved: {!r}".format(host)) from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not ip.is_global:
            raise ValueError(
                "webhook_url resolves to non-global address {} -> {}".format(host, addr)
            )


def parse_allowed_hosts(raw: str) -> frozenset:
    """解析域名白名单（分号/逗号分隔）；空 → 空集（fail-closed）。"""
    hosts = set()
    for chunk in str(raw or "").replace(",", ";").split(";"):
        item = chunk.strip().lower()
        if item:
            hosts.add(item)
    return frozenset(hosts)


class BaseNotifier(ABC):
    """告警推送接口。返回 True 表示本次推送成功。"""

    @abstractmethod
    def notify(self, title: str, content: str, severity: str = "warning") -> bool:
        """推送一条告警。"""


class ConsoleNotifier(BaseNotifier):
    """打印到终端（默认）。"""

    def notify(self, title: str, content: str, severity: str = "warning") -> bool:
        print("[{}] {}\n{}".format(severity, title, content))
        return True


class WebhookNotifier(BaseNotifier):
    """POST 到 Webhook（钉钉/飞书/Slack 通用 JSON；支持自定义 body 模板）。

    allowed_hosts 为运维配置的域名白名单；空集时本推送器拒绝工作（fail-closed）。
    """

    def __init__(self, url: str, allowed_hosts, template: Optional[str] = None, timeout: float = 10.0):
        self.url = str(url)
        self.allowed_hosts = frozenset(h.strip().lower() for h in (allowed_hosts or ()) if h)
        self.template = template
        self.timeout = float(timeout)

    def notify(self, title: str, content: str, severity: str = "warning") -> bool:
        if self.template:
            body_text = (
                self.template
                .replace("{title}", _json_safe(title))
                .replace("{content}", _json_safe(content))
                .replace("{severity}", _json_safe(severity))
            )
            import json as _json

            body = _json.loads(body_text)
        else:
            body = {"title": title, "content": content, "severity": severity}

        # ---- 请求前边界校验（协议 + 域名白名单 + 地址解析边界）----
        parts = urlsplit(str(self.url).strip())
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        if scheme not in _ALLOWED_SCHEMES or not host:
            raise ValueError("webhook_url must be http/https with host: {!r}".format(self.url))
        if parts.username or parts.password:
            raise ValueError("webhook_url must not contain credentials")
        if not self.allowed_hosts or host not in self.allowed_hosts:
            raise ValueError(
                "webhook_url host {!r} is not in the allowed hosts whitelist".format(host)
            )
        _assert_global_addresses(host)  # 解析并阻断私网/环回/链路本地（防 DNS rebinding）
        target = parts.geturl()

        resp = httpx.post(target, json=body, timeout=self.timeout, follow_redirects=False)
        return 200 <= resp.status_code < 300


class FileNotifier(BaseNotifier):
    """追加写入本地 alerts 文件（唯一允许的写文件：自己的告警日志）。"""

    def __init__(self, path: str):
        candidate = Path(str(path))
        if ".." in candidate.parts:
            raise ValueError("alerts_log_path must not contain '..': {!r}".format(path))
        import os

        self._path = Path(os.path.realpath(str(candidate)))

    def notify(self, title: str, content: str, severity: str = "warning") -> bool:
        stamp = datetime.now(timezone.utc).isoformat()
        one_line = " | ".join(part.strip() for part in content.splitlines() if part.strip())
        line = "{}\t[{}]\t{}\t{}\n".format(stamp, severity, title, one_line[:2000])
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        return True


class NotifierGroup(BaseNotifier):
    """扇出多个 Notifier；单个失败只记日志，不影响其他（隔离）。"""

    def __init__(self, notifiers: List[BaseNotifier]):
        self.notifiers: List[BaseNotifier] = list(notifiers)

    def notify(self, title: str, content: str, severity: str = "warning") -> bool:
        ok = False
        for notifier in self.notifiers:
            try:
                if notifier.notify(title, content, severity):
                    ok = True
            except Exception:
                logger.exception("notifier %s failed (isolated)", type(notifier).__name__)
        return ok


def _json_safe(text: str) -> str:
    """把文本安全嵌入 JSON 字符串字面量（模板替换用）。"""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "")
        .replace("\t", "\\t")
    )


def build_notifiers(settings) -> NotifierGroup:
    """按 Settings 组装 Notifier 组合（console;webhook;file，分号分隔）。"""
    notifiers: List[BaseNotifier] = []
    seen = set()
    for raw in str(getattr(settings, "notifier_types_raw", "console") or "console").split(";"):
        kind = raw.strip().lower()
        if not kind or kind in seen:
            continue
        seen.add(kind)
        if kind == "console":
            notifiers.append(ConsoleNotifier())
        elif kind == "webhook":
            url = str(getattr(settings, "webhook_url", "") or "").strip()
            allowed = parse_allowed_hosts(getattr(settings, "webhook_allowed_hosts_raw", ""))
            if not url:
                logger.warning("webhook notifier skipped: webhook_url is empty")
                continue
            if not allowed:
                logger.warning(
                    "webhook notifier skipped: webhook_allowed_hosts is empty (fail-closed)"
                )
                continue
            notifiers.append(WebhookNotifier(url, allowed))
        elif kind == "file":
            path = str(getattr(settings, "alerts_log_path", "data/alerts.log") or "data/alerts.log")
            try:
                notifiers.append(FileNotifier(path))
            except ValueError as exc:
                logger.warning("file notifier skipped: %s", exc)
        else:
            logger.warning("unknown notifier type skipped: %r", kind)
    if not notifiers:
        notifiers.append(ConsoleNotifier())  # 至少保留终端，保证告警不丢
    return NotifierGroup(notifiers)
