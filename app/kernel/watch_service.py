"""Watch 模式：日志变化 → 规则触发 → 自动分析 Episode → 告警推送。

WatchService 把三个部件缝起来：
- LogWatcher：轮询日志新增的 ERROR 行（只读）；
- TriggerEngine：判断「是否值得唤醒」（阈值/窗口/冷却）；
- EvolutionLoop：start_async 触发一次只读分析 episode；
- BaseNotifier：episode 终态后推送结论（concluded）或错误（failed）。
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from app.kernel.state import Phase
from app.kernel.trigger_rules import Rule, TriggerEngine
from app.kernel.watcher import LogWatcher

logger = logging.getLogger("moray_aio.watch")


def _severity_for(rule: Rule) -> str:
    return "critical" if rule.name == "critical_keywords" else "warning"


class WatchService:
    """事件驱动主循环。tick() 为单步检查（线程与测试共用同一入口）。"""

    def __init__(self, log_path: str, rules, watcher: LogWatcher, loop, notifier,
                 check_interval: float = 2.0):
        self.log_path = str(log_path)
        self.engine = rules if isinstance(rules, TriggerEngine) else TriggerEngine(rules)
        self.watcher = watcher
        self.loop = loop
        self.notifier = notifier
        self.check_interval = max(0.1, float(check_interval))
        self._pending: Dict[str, dict] = {}
        self._hits: Dict[str, int] = {name: 0 for name in self.engine.fire_count}
        self._last_fired_at: Dict[str, Optional[str]] = {name: None for name in self.engine.fire_count}
        self.notifications_sent = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # 监听器回调接到本服务
        self.watcher.on_lines = self.feed_lines

    # ---------- 事件入口 ----------

    def feed_lines(self, lines: List[str]) -> None:
        """watcher 回调：逐行喂给规则引擎，命中即触发 episode。"""
        for line in lines:
            try:
                rule = self.engine.feed(line)
                if rule is None:
                    continue
                self._hits[rule.name] = self._hits.get(rule.name, 0) + 1
                self._last_fired_at[rule.name] = _now_iso()
                trigger = "watch:{}".format(rule.name)
                episode_id = self.loop.start_async(trigger)
                self._pending[episode_id] = {
                    "rule": rule.name,
                    "severity": _severity_for(rule),
                    "lines": self.engine.recent_hits(rule.name, 5),
                }
                logger.info("watch rule %s fired -> episode %s", rule.name, episode_id)
            except Exception:
                logger.exception("watch feed failed (ignored)")

    def tick(self) -> None:
        """检查待完成 episode：终态即推送告警（concluded 带结论，failed 带错误）。"""
        for episode_id in list(self._pending):
            try:
                ep = self.loop.require(episode_id)
            except Exception:
                continue  # episode 暂不可读：下一轮再试
            phase = getattr(ep.phase, "value", ep.phase)
            if phase not in (Phase.CONCLUDED.value, Phase.FAILED.value):
                continue
            meta = self._pending.pop(episode_id)
            self._notify_episode(ep, meta)

    def _notify_episode(self, ep, meta: dict) -> None:
        rule_name = meta.get("rule", "unknown")
        severity = meta.get("severity", "warning")
        lines = list(meta.get("lines") or [])[:5]
        title = "[moray-aio] {} | {}".format(severity, rule_name)
        parts = ["命中日志："]
        parts.extend("  " + line for line in lines)
        phase = getattr(ep.phase, "value", ep.phase)
        if phase == Phase.CONCLUDED.value:
            parts.append("结论: {}".format(getattr(ep, "conclusion", None) or "(无)"))
        else:
            parts.append("错误: {}".format(getattr(ep, "error", None) or "episode failed"))
        content = "\n".join(parts)
        try:
            if self.notifier.notify(title, content, severity):
                self.notifications_sent += 1
        except Exception:
            logger.exception("notifier failed (isolated)")

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self.watcher.start()
        self._thread = threading.Thread(target=self._run, name="moray-watch-service", daemon=True)
        self._thread.start()
        logger.info("watch service started: %s", self.log_path)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.watcher.stop()
        except Exception:
            logger.exception("watcher stop failed (ignored)")
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        logger.info("watch service stopped")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("watch tick failed (ignored)")
            self._stop.wait(self.check_interval)

    def status(self) -> dict:
        rules_status = self.engine.status()
        for name, info in rules_status.items():
            info["hits_total"] = self._hits.get(name, 0)
            info["last_fired_at"] = self._last_fired_at.get(name)
        return {
            "running": self.is_running(),
            "watched_log": self.log_path,
            "pending_episodes": len(self._pending),
            "notifications_sent": self.notifications_sent,
            "rules": rules_status,
        }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- 全局管理器

class WatchManager:
    """进程级 WatchService 管理器（API / lifespan 共用单例）。"""

    def __init__(self):
        self._service: Optional[WatchService] = None

    def start(self) -> dict:
        if self._service is not None and self._service.is_running():
            return self._service.status()
        from app.config import settings
        from app.deps import get_loop, get_tools
        from app.kernel.trigger_rules import parse_rules_config
        from app.notifier import build_notifiers

        checked = get_tools()._check_path(settings.log_path)  # 白名单 fail-closed
        rules = parse_rules_config(settings.watch_rules_config)
        watcher = LogWatcher(
            str(checked),
            on_lines=lambda lines: None,
            poll_interval=settings.watch_poll_interval_seconds,
        )
        service = WatchService(
            log_path=str(checked),
            rules=rules,
            watcher=watcher,
            loop=get_loop(),
            notifier=build_notifiers(settings),
            check_interval=settings.watch_poll_interval_seconds,
        )
        service.start()
        self._service = service
        return service.status()

    def stop(self) -> dict:
        if self._service is not None:
            self._service.stop()
        return self.status()

    def status(self) -> dict:
        if self._service is None:
            return {
                "running": False,
                "watched_log": None,
                "pending_episodes": 0,
                "notifications_sent": 0,
                "rules": {},
            }
        return self._service.status()


_MANAGER = WatchManager()


def get_watch_manager() -> WatchManager:
    return _MANAGER
