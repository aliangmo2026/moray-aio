"""Trigger rule engine: decides when an error stream is worth waking the agent up.

Rule semantics:
- rapid_error_burst: >= threshold hits within window_seconds -> fire;
- critical_keywords: pattern hit fires immediately (threshold=1);
- error_spike: hits in the last window >= threshold x the previous window (surge).
All rules share cooldowns: hits inside cooldown_seconds never fire again.
The time source time.time() can be monkeypatched (or pass ``now`` explicitly).
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
    """Feed log lines in; returns the fired rule (None while cooling down)."""

    def __init__(self, rules: Optional[List[Rule]] = None, max_hits_memory: int = 500):
        self.rules: List[Rule] = list(rules or DEFAULT_RULES)
        self._compiled = {r.name: re.compile(r.pattern) for r in self.rules}
        self._hits: Dict[str, Deque[Tuple[float, str]]] = {
            r.name: deque(maxlen=max_hits_memory) for r in self.rules
        }
        self._cooldown_until: Dict[str, float] = {}
        self._last_fired_at: Dict[str, Optional[float]] = {}
        self.fire_count: Dict[str, int] = {r.name: 0 for r in self.rules}

    def feed(self, line: str, now: Optional[float] = None) -> Optional[Rule]:
        """Feed one line; returns the fired rule, or None when nothing fires."""
        now = time.time() if now is None else float(now)
        for rule in self.rules:
            if not self._compiled[rule.name].search(str(line)):
                continue
            self._hits[rule.name].append((now, str(line)))
            if now < self._cooldown_until.get(rule.name, 0.0):
                continue  # inside cooldown: record but never fire
            if not self._should_fire(rule, now):
                continue
            self._cooldown_until[rule.name] = now + float(rule.cooldown_seconds)
            self._last_fired_at[rule.name] = now
            self.fire_count[rule.name] += 1
            # Hits are intentionally NOT cleared: recent_hits needs them as alert
            # evidence; re-arming relies on the time window expiring naturally
            # (cooldown is always much larger than window).
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
        """Recent raw log lines matched by the rule (alert body evidence)."""
        hits = self._hits.get(rule_name)
        if not hits:
            return []
        return [line for _t, line in list(hits)[-max(1, int(limit)):]]

    def status(self) -> dict:
        """Per-rule snapshot for /watch/status."""
        out: Dict[str, dict] = {}
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
    """Parse MORAY_AIO_WATCH_RULES_CONFIG (JSON array); empty string -> default rules.

    Each item: {"name","pattern","threshold","window_seconds","cooldown_seconds"}.
    Invalid JSON / invalid regex / missing name raise ValueError (fail-closed).
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
    seen: set = set()
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
