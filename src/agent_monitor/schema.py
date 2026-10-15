"""Shared event schema. Every monitored agent emits these as JSON Lines.

The monitor consumes raw telemetry only, never another agent's conclusions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

EVENT_TYPES = frozenset(
    {
        "run_started",
        "tool_call",
        "llm_call",
        "escalation",
        "claim",         # agent states an outcome, e.g. status="fixed"
        "verification",  # independent check, status="ok" | "failed"
        "answer",        # RAG answer with citations + retrieved ids
        "run_finished",  # status="success" | "escalated" | "error"
    }
)

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def args_hash(args: Any) -> str:
    """Stable short hash of tool arguments (raw args are never stored)."""
    blob = json.dumps(args, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:10]


@dataclass
class Event:
    id: str
    ts: float
    agent: str
    run_id: str
    type: str
    tool: str | None = None
    args_hash: str | None = None
    tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    status: str | None = None
    citations: tuple[str, ...] = ()
    retrieved: tuple[str, ...] = ()
    attrs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {self.type!r}")
        self.citations = tuple(self.citations)
        self.retrieved = tuple(self.retrieved)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items()}
        d["citations"] = list(self.citations)
        d["retrieved"] = list(self.retrieved)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class Finding:
    rule: str
    severity: str
    agent: str
    run_id: str | None
    message: str
    evidence: tuple[str, ...] = ()  # event ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "agent": self.agent,
            "run_id": self.run_id,
            "message": self.message,
            "evidence": list(self.evidence),
        }


def load_events(path: str | Path) -> list[Event]:
    events: list[Event] = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(Event.from_dict(json.loads(line)))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{n}: {exc}") from exc
    return events


def dump_events(events: Iterable[Event], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e.to_dict(), sort_keys=True) + "\n")
