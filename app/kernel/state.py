"""Self-evolution kernel state machine: Phase enum, Episode dataclass and legal transitions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Union


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Phase(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    OBSERVING = "observing"
    ANALYZING = "analyzing"
    PROPOSING = "proposing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    CONCLUDED = "concluded"
    FAILED = "failed"


# Explicit transition table; anything outside it raises ValueError in transition().
# created -> observing (sync path) and created -> queued -> observing (async path) are both legal.
ALLOWED: Dict[Phase, Set[Phase]] = {
    Phase.CREATED: {Phase.OBSERVING, Phase.QUEUED, Phase.FAILED},
    Phase.QUEUED: {Phase.OBSERVING, Phase.FAILED},
    Phase.OBSERVING: {Phase.ANALYZING, Phase.FAILED},
    Phase.ANALYZING: {Phase.PROPOSING, Phase.CONCLUDED, Phase.FAILED},
    Phase.PROPOSING: {
        Phase.AWAITING_APPROVAL,
        Phase.EXECUTING,
        Phase.CONCLUDED,
        Phase.FAILED,
    },
    Phase.AWAITING_APPROVAL: {Phase.EXECUTING, Phase.CONCLUDED, Phase.FAILED},
    Phase.EXECUTING: {Phase.ANALYZING, Phase.CONCLUDED, Phase.FAILED},
    Phase.CONCLUDED: set(),
    Phase.FAILED: set(),
}


@dataclass
class Episode:
    """Full trajectory of one self-evolution run: observations, analysis, tasks, conclusion."""

    id: str
    trigger: str
    phase: Phase
    iteration: int = 0
    observations: List[dict] = field(default_factory=list)
    analysis: Optional[dict] = None
    tasks: List[dict] = field(default_factory=list)
    conclusion: Optional[str] = None
    error: Optional[str] = None
    # Low-level tracing: one entry per core stage
    # {stage, iteration, phase, timestamp, duration_ms, llm}
    trace: List[dict] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def new(cls, trigger: str = "recent_errors") -> "Episode":
        return cls(id=str(uuid.uuid4()), trigger=trigger, phase=Phase.CREATED)

    def transition(self, target: Union[Phase, str]) -> Phase:
        """Move to the target phase; raises ValueError on any illegal transition."""
        if isinstance(target, str):
            target = Phase(target)
        if target not in ALLOWED.get(self.phase, set()):
            raise ValueError(
                "illegal transition {} -> {}".format(self.phase.value, target.value)
            )
        self.phase = target
        self.updated_at = _now()
        return self.phase

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "trigger": self.trigger,
            "phase": self.phase.value,
            "iteration": self.iteration,
            "observations": self.observations,
            "analysis": self.analysis,
            "tasks": self.tasks,
            "conclusion": self.conclusion,
            "error": self.error,
            "trace": self.trace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Episode":
        """Rebuild from a persisted payload; unknown keys are dropped (forward compatible)."""
        data = dict(data or {})
        data["phase"] = Phase(data.get("phase", Phase.CREATED.value))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
