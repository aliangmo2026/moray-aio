"""v0.7.0 event-driven tests: watcher polling, trigger rules, alert pushers, and
the watch service end-to-end chain."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.kernel.state import Phase
from app.kernel.trigger_rules import DEFAULT_RULES, Rule, TriggerEngine, parse_rules_config
from app.kernel.watch_service import WatchService
from app.kernel.watcher import LogWatcher
from app import notifier as nt


CRITICAL_RULE = Rule(
    name="critical_keywords",
    pattern=r"OOM|Connection refused|Segmentation fault|panic",
    threshold=1,
    window_seconds=60.0,
    cooldown_seconds=120.0,
)
RAPID_RULE = Rule(
    name="rapid_error_burst", pattern=r"\bERROR\b", threshold=5,
    window_seconds=60.0, cooldown_seconds=300.0,
)
SPIKE_RULE = Rule(
    name="error_spike", pattern=r"\bERROR\b", threshold=3,
    window_seconds=60.0, cooldown_seconds=300.0,
)


# ---------- LogWatcher ----------

def test_watcher_detects_appended_error(tmp_path):
    log = tmp_path / "w.log"
    log.write_text("INFO start\n", encoding="utf-8")
    got = []
    ev = threading.Event()

    def on_lines(lines):
        got.extend(lines)
        ev.set()

    watcher = LogWatcher(str(log), on_lines, poll_interval=0.05)
    watcher.start()
    assert watcher.is_running()
    time.sleep(0.2)  # let it consume the initial content first
    log.write_text("INFO start\nERROR boom-1\n", encoding="utf-8")  # append one line

    assert ev.wait(timeout=3.0), "watcher did not detect the new ERROR in time"
    watcher.stop()
    assert not watcher.is_running()
    assert got == ["ERROR boom-1"]


def test_watcher_handles_rotation(tmp_path):
    log = tmp_path / "w.log"
    log.write_text("ERROR one\nERROR two\n", encoding="utf-8")
    watcher = LogWatcher(str(log), on_lines=lambda lines: None, poll_interval=0.05)

    assert watcher._read_new_lines() == ["ERROR one", "ERROR two"]
    assert watcher._read_new_lines() == []  # nothing new

    # logrotate: truncate to zero, then write shorter new content -> offset resets
    log.write_text("", encoding="utf-8")
    log.write_text("ERROR after-rotate\n", encoding="utf-8")
    assert watcher._read_new_lines() == ["ERROR after-rotate"]


# ---------- TriggerEngine ----------

def _frozen_clock(monkeypatch, start=1000.0):
    import app.kernel.trigger_rules as tr

    fake = {"now": start}
    monkeypatch.setattr(tr.time, "time", lambda: fake["now"])
    return fake


def test_trigger_rapid_burst(monkeypatch):
    fake = _frozen_clock(monkeypatch)
    engine = TriggerEngine([RAPID_RULE])
    for i in range(4):
        assert engine.feed("ERROR boom") is None
        fake["now"] += 5
    fired = engine.feed("ERROR boom")  # the 5th hit inside the 60s window
    assert fired is not None and fired.name == "rapid_error_burst"
    assert engine.fire_count["rapid_error_burst"] == 1


def test_trigger_cooldown(monkeypatch):
    fake = _frozen_clock(monkeypatch)
    engine = TriggerEngine([RAPID_RULE])
    fired = None
    for i in range(5):
        fired = engine.feed("ERROR boom")
    assert fired is not None  # the 5th line fires

    # inside the 300s cooldown: even 5 fresh hits never fire
    for i in range(5):
        assert engine.feed("ERROR boom") is None
        fake["now"] += 1

    # after the cooldown, counting restarts and can fire again
    fake["now"] += 400
    for i in range(5):
        fired = engine.feed("ERROR boom")
    assert fired is not None and fired.name == "rapid_error_burst"
    assert engine.fire_count["rapid_error_burst"] == 2


def test_trigger_critical_keyword(monkeypatch):
    _frozen_clock(monkeypatch)
    engine = TriggerEngine([CRITICAL_RULE])
    fired = engine.feed("2026-10-24 ERROR Connection refused to redis")
    assert fired is not None and fired.name == "critical_keywords"
    # within the 120s cooldown it never fires again
    assert engine.feed("panic: OOM killer") is None


def test_trigger_error_spike(monkeypatch):
    fake = _frozen_clock(monkeypatch)
    engine = TriggerEngine([SPIKE_RULE])
    # 2 hits in the previous window
    assert engine.feed("ERROR a") is None
    assert engine.feed("ERROR b") is None
    # next window surge: fires at 3x (2 x 3 = 6)
    fake["now"] += 70
    fired = None
    for i in range(6):
        fired = engine.feed("ERROR surge")
    assert fired is not None and fired.name == "error_spike"


def test_rules_config_parse():
    assert parse_rules_config("") == list(DEFAULT_RULES)
    rules = parse_rules_config(
        '[{"name": "r1", "pattern": "boom", "threshold": 2, '
        '"window_seconds": 30, "cooldown_seconds": 60}]'
    )
    assert rules[0].name == "r1" and rules[0].threshold == 2
    with pytest.raises(ValueError):
        parse_rules_config("not-json")
    with pytest.raises(ValueError):
        parse_rules_config('[{"name": "bad", "pattern": "(unclosed"}]')


# ---------- Notifier ----------

class _RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, title, content, severity="warning"):
        self.calls.append((title, content, severity))
        return True


class _BoomNotifier:
    def notify(self, title, content, severity="warning"):
        raise RuntimeError("boom")


def _stub_dns(monkeypatch, ip="93.184.216.34"):
    import socket as socket_mod

    monkeypatch.setattr(
        nt.socket,
        "getaddrinfo",
        lambda host, port: [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", (ip, 443))],
    )


def test_notifier_webhook_success(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, follow_redirects=None):
        captured["url"] = url
        captured["json"] = json
        captured["follow_redirects"] = follow_redirects
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(nt.httpx, "post", fake_post)
    _stub_dns(monkeypatch)

    webhook = nt.WebhookNotifier("https://hooks.example.com/services/demo", {"hooks.example.com"})
    ok = webhook.notify("[moray-aio] warning | rapid_error_burst", "line1\nline2", "warning")

    assert ok is True
    assert captured["url"] == "https://hooks.example.com/services/demo"
    assert captured["json"]["title"] == "[moray-aio] warning | rapid_error_burst"
    assert "line1" in captured["json"]["content"]
    assert captured["json"]["severity"] == "warning"
    assert captured["follow_redirects"] is False  # redirects disabled


def test_notifier_webhook_blocks_private_targets(monkeypatch):
    _stub_dns(monkeypatch)
    # loopback / private / reserved addresses and off-allowlist domains are rejected
    for bad in ("http://127.0.0.1:8080/x", "http://10.0.0.5/x", "http://169.254.169.254/latest"):
        with pytest.raises(ValueError):
            nt.WebhookNotifier(bad, {"hooks.example.com"}).notify("t", "c")
    with pytest.raises(ValueError):
        nt.WebhookNotifier("https://evil.example.org/x", {"hooks.example.com"}).notify("t", "c")
    with pytest.raises(ValueError):
        nt.WebhookNotifier("https://hooks.example.com/x", set()).notify("t", "c")  # empty allowlist


def test_notifier_isolation():
    recorder = _RecordingNotifier()
    group = nt.NotifierGroup([_BoomNotifier(), recorder])
    assert group.notify("title-x", "content-x", "critical") is True
    assert recorder.calls == [("title-x", "content-x", "critical")]
    # everything failing -> returns False without raising
    assert nt.NotifierGroup([_BoomNotifier()]).notify("t", "c") is False


# ---------- WatchService end-to-end ----------

class _FakeLoop:
    def __init__(self, episode):
        self.episode = episode
        self.started = []

    def start_async(self, trigger):
        self.started.append(trigger)
        return "ep-1"

    def require(self, episode_id):
        return self.episode


def test_watch_service_end_to_end(tmp_path):
    log = tmp_path / "w.log"
    log.write_text("INFO boot\n", encoding="utf-8")
    episode = SimpleNamespace(phase=Phase.CONCLUDED, conclusion="Check the redis service", error=None)
    loop = _FakeLoop(episode)
    recorder = _RecordingNotifier()
    watcher = LogWatcher(str(log), on_lines=lambda lines: None, poll_interval=0.05)
    service = WatchService(
        log_path=str(log),
        rules=[CRITICAL_RULE],
        watcher=watcher,
        loop=loop,
        notifier=recorder,
        check_interval=0.1,
    )

    service.feed_lines(["ERROR Connection refused to redis 127.0.0.1:6379"])
    assert loop.started == ["watch:critical_keywords"]  # fired -> self-driven analysis

    service.tick()  # episode already concluded -> push the alert
    assert len(recorder.calls) == 1
    title, content, severity = recorder.calls[0]
    assert title == "[moray-aio] critical | critical_keywords"
    assert "Connection refused" in content
    assert "Check the redis service" in content
    assert severity == "critical"
    assert service.status()["notifications_sent"] == 1


def test_watch_api_endpoints(monkeypatch):
    from app.kernel import watch_service as ws
    from app.main import create_app

    manager = SimpleNamespace(
        start=lambda: {"running": True},
        stop=lambda: {"running": False},
        status=lambda: {"running": False, "rules": {}},
    )
    monkeypatch.setattr(ws, "get_watch_manager", lambda: manager)
    client = TestClient(create_app(Settings()))

    assert client.post("/watch/start").status_code == 200
    assert client.get("/watch/status").json() == {"running": False, "rules": {}}
    assert client.post("/watch/stop").status_code == 200
