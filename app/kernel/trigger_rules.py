"""触发器规则引擎：决定「什么时候值得唤醒 Agent 做一次分析」。

规则语义：
- rapid_error_burst：window_seconds 内命中 >= threshold 条 → 触发；
- critical_keywords：命中 pattern（单次即触发，threshold=1）；
- error_spike：最近 window_seconds 的命中数 >= threshold x 前一个窗口（突增检测）。
所有规则共享冷却机制：cooldown_seconds 内重复命中一律返回 None。
时间源 time.time() 可被 monkeypatch（now 参数亦可显式注入）。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: str
    threshold: int = 1
    window_seconds: float = 60.0
    cooldown_seconds: float = 300.0


DEFAULT_RULES: Tuple[Rule, ...] = (
    Rule(
        name="rapid_error_burst",
        pattern=r"\bERROR\b",
        threshold=5,
        window_seconds=60.0,
        cooldown_seconds=300.0,
    ),
    Rule(
        name="critical_keywords",
        pattern=r"OOM|Connection refused|Segmentation fault|panic",
        threshold=1,
        window_seconds=60.0,
        cooldown_seconds=120.0,
    ),
    Rule(
        name="error_spike",
        pattern=r"\bERROR\b",
        threshold=3,
        window_seconds=60.0,
        cooldown_seconds=300.0,
    ),
)


class TriggerEngine:
    """喂入日志行，命中规则（且不在冷却期）时返回该规则。"""

    def __init__(self, rules=None, max_hits_memory: int = 500):
        self.rules: List[Rule] = list(rules or DEFAULT_RULES)
        self._compiled = {r.name: re.compile(r.pattern) for r in self.rules}
        self._hits: Dict[str, Deque[Tuple[float, str]]] = {
            r.name: deque(maxlen=max_hits_memory) for r in self.rules
        }
        self._cooldown_until: Dict[str, float] = {}
        self._last_fired_at: Dict[str, Optional[float]] = {}
        self.fire_count: Dict[str, int] = {r.name: 0 for r in self.rules}

    def feed(self, line: str, now: Optional[float] = None) -> Optional[Rule]:
        now = time.time() if now is None else float(now)
        for rule in self.rules:
            if not self._compiled[rule.name].search(str(line)):
                continue
            self._hits[rule.name].append((now, str(line)))
            if now < self._cooldown_until.get(rule.name, 0.0):
                continue  # 冷却期内：只记录，不触发
            if not self._should_fire(rule, now):
                continue
            self._cooldown_until[rule.name] = now + float(rule.cooldown_seconds)
            self._last_fired_at[rule.name] = now
            self.fire_count[rule.name] += 1
            # 不清空命中缓存：recent_hits 需要它做告警证据；
            # 重新计数靠时间窗自然过期（cooldown 远大于 window）
            return rule
        return None

    def _should_fire(self, rule: Rule, now: float) -> bool:
        hits = self._hits[rule.name]
        if rule.name == "error_spike":
            current = sum(1 for t, _ in hits if now - rule.window_seconds < t <= now)
            previous = sum(
                1 for t, _ in hits
                if now - 2 * rule.window_seconds < t <= now - rule.window_seconds
            )
            return previous >= 1 and current >= max(1, rule.threshold) * previous
        recent = sum(1 for t, _ in hits if now - rule.window_seconds <= t <= now)
        return recent >= max(1, rule.threshold)

    def recent_hits(self, rule_name: str, limit: int = 5) -> List[str]:
        """该规则最近命中的原始日志行（告警正文用）。"""
        hits = self._hits.get(rule_name)
        if not hits:
            return []
        return [line for _t, line in list(hits)[-max(1, int(limit)):]]

    def status(self) -> dict:
        out = {}
        for rule in self.rules:
            out[rule.name] = {
                "pattern": rule.pattern,
                "threshold": rule.threshold,
                "window_seconds": rule.window_seconds,
                "cooldown_seconds": rule.cooldown_seconds,
                "fire_count": self.fire_count.get(rule.name, 0),
                "last_fired_at": self._last_fired_at.get(rule.name),
                "cooldown_until": self._cooldown_until.get(rule.name, 0.0),
                "hits_stored": len(self._hits.get(rule.name, ())),
            }
        return out


def parse_rules_config(config_json: str) -> List[Rule]:
    """解析 MORAY_AIO_WATCH_RULES_CONFIG（JSON 数组）；空串返回默认规则。

    每项形如 {"name","pattern","threshold","window_seconds","cooldown_seconds"}；
    非法 JSON / 非法正则 / 缺 name 一律抛 ValueError（fail-closed）。
    """
    text = (config_json or "").strip()
    if not text:
        return list(DEFAULT_RULES)
    import json

    try:
        items = json.loads(text)
    except ValueError as exc:
        raise ValueError("watch_rules_config is not valid JSON") from exc
    if not isinstance(items, list) or not items:
        raise ValueError("watch_rules_config must be a non-empty JSON array")
    rules: List[Rule] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("watch_rules_config items must be objects")
        name = str(item.get("name", "")).strip()
        pattern = str(item.get("pattern", ""))
        if not name or name in seen:
            raise ValueError("rule name missing or duplicated: {!r}".format(name))
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError("rule {!r} has invalid regex: {}".format(name, exc)) from exc
        seen.add(name)
        rules.append(
            Rule(
                name=name,
                pattern=pattern,
                threshold=int(item.get("threshold", 1)),
                window_seconds=float(item.get("window_seconds", 60)),
                cooldown_seconds=float(item.get("cooldown_seconds", 300)),
            )
        )
    return rules
