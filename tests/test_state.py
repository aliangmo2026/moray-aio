"""状态机测试：合法转移链 happy path；非法转移抛 ValueError；序列化往返。"""

from __future__ import annotations

import pytest

from app.kernel.state import Episode, Phase


def test_happy_path_transition_chain():
    ep = Episode.new(trigger="recent_errors")
    assert ep.phase == Phase.CREATED
    assert ep.iteration == 0

    ep.transition(Phase.OBSERVING)
    ep.transition(Phase.ANALYZING)
    ep.transition(Phase.PROPOSING)
    ep.transition(Phase.AWAITING_APPROVAL)
    ep.transition(Phase.EXECUTING)
    ep.transition(Phase.ANALYZING)  # 执行后回到分析，形成闭环
    ep.transition(Phase.PROPOSING)
    ep.transition(Phase.CONCLUDED)
    assert ep.phase == Phase.CONCLUDED


def test_illegal_transition_raises_valueerror():
    ep = Episode.new()
    with pytest.raises(ValueError):
        ep.transition(Phase.CONCLUDED)  # created -> concluded 非法
    with pytest.raises(ValueError):
        ep.transition(Phase.EXECUTING)  # created -> executing 非法


def test_terminal_phases_are_frozen():
    ep = Episode.new()
    ep.transition(Phase.OBSERVING)
    ep.transition(Phase.FAILED)
    with pytest.raises(ValueError):
        ep.transition(Phase.OBSERVING)  # failed 是终态
    done = Episode.new()
    done.transition(Phase.OBSERVING)
    done.transition(Phase.ANALYZING)
    done.transition(Phase.PROPOSING)
    done.transition(Phase.CONCLUDED)
    with pytest.raises(ValueError):
        done.transition(Phase.ANALYZING)


def test_transition_accepts_string_and_roundtrip():
    ep = Episode.new(trigger="manual")
    ep.transition("observing")
    ep.transition(Phase.ANALYZING)
    data = ep.to_dict()
    assert data["phase"] == "analyzing"

    clone = Episode.from_dict(data)
    assert clone.id == ep.id
    assert clone.phase == Phase.ANALYZING
    assert clone.trigger == "manual"
    assert clone.created_at == ep.created_at


def test_from_dict_ignores_unknown_keys():
    ep = Episode.new()
    data = ep.to_dict()
    data["future_field"] = 123
    clone = Episode.from_dict(data)
    assert clone.phase == Phase.CREATED
