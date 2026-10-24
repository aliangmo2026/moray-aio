"""Alert pushers: deliver "worth knowing" events to console / webhook / local file.

Hard rules:
- Webhook URLs are http/https only, and the host must hit an operator-configured
  domain allowlist; every request re-validates protocol/host/allowlist, resolves the
  domain and requires every resolved address to be global (rejecting localhost,
  loopback, private and reserved addresses — DNS-rebinding aware); redirects disabled;
- Credentials come from environment variables only (webhook URL/allowlist come from
  Settings / environment);
- FileNotifier appends ONLY to its own alerts file (default data/alerts.log) —
  it never touches watched logs;
- Fan-out across notifiers is isolated: one failure is logged and never affects others.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("moray_aio.notifier")

_ALLOWED_SCHEMES = ("http", "https")


def _assert_global_addresses(host: str) -> None:
    """Resolve and validate address boundaries: literal IPs and every resolved
    address must be globally routable."""
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
    """Parse the domain allowlist (semicolon/comma separated); empty -> empty set (fail-closed)."""
    hosts = set()
    for chunk in str(raw or "").replace(",", ";").split(";"):
        item = chunk.strip().lower()
        if item:
            hosts.add(item)
    return frozenset(hosts)


class BaseNotifier(ABC):
    """Alert push interface. Returns True when this push succeeded."""

    @abstractmethod
    def notify(self, title: str, content: str, severity: str = "warning") -> bool:
        """Push one alert."""


class ConsoleNotifier(BaseNotifier):
    """Print to the terminal (default)."""

    def notify(self, title: str, content: str, severity: str = "warning") -> bool:
        print("[{}] {}\n{}".format(severity, title, content))
        return True


class WebhookNotifier(BaseNotifier):
    """POST to a webhook (DingTalk/Feishu/Slack-compatible JSON; custom body template).

    ``allowed_hosts`` is the operator-configured domain allowlist; an empty allowlist
    makes this pusher refuse to work (fail-closed).
    """

    def __init__(
        self,
        url: str,
        allowed_hosts,
        template: Optional[str] = None,
        timeout: float = 10.0,
    ):
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

        # ---- pre-request boundary validation (protocol + domain allowlist + resolution) ----
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
        _assert_global_addresses(host)  # resolve & block loopback/private/reserved (DNS-rebinding aware)
        target = parts.geturl()

        resp = httpx.post(target, json=body, timeout=self.timeout, follow_redirects=False)
        return 200 <= resp.status_code < 300


class FileNotifier(BaseNotifier):
    """Append to a local alerts file (the only file this project ever writes)."""

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
    """Fan out to multiple notifiers; one failure is logged and never affects others."""

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
    """Embed text safely inside a JSON string literal (for template replacement)."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "")
        .replace("\t", "\\t")
    )


def build_notifiers(settings) -> NotifierGroup:
    """Assemble the notifier group from Settings (console;webhook;file)."""
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
        notifiers.append(ConsoleNotifier())  # keep at least the terminal so alerts are never lost
    return NotifierGroup(notifiers)
