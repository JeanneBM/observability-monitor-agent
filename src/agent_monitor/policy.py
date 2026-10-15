"""Declarative policy: what each monitored agent is allowed to do."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentPolicy:
    allowed_tools: frozenset[str]
    max_tokens_per_run: int = 20_000
    max_cost_per_run: float = 0.50
    max_tool_repeats: int = 3          # identical tool+args calls within one run
    require_verification: bool = False  # a "fixed" claim needs a later ok verification
    require_citations: bool = False     # answers must cite only retrieved sources


@dataclass
class DriftPolicy:
    min_runs: int = 10
    escalation_delta: float = 0.25   # absolute rise in escalation rate vs baseline
    tool_calls_ratio: float = 2.0    # mean tool calls per run vs baseline


@dataclass
class Policy:
    agents: dict[str, AgentPolicy] = field(default_factory=dict)
    drift: DriftPolicy = field(default_factory=DriftPolicy)

    def agent(self, name: str) -> AgentPolicy | None:
        return self.agents.get(name)


def policy_from_dict(raw: dict) -> Policy:
    agents = {}
    for name, cfg in raw.get("agents", {}).items():
        cfg = dict(cfg)
        cfg["allowed_tools"] = frozenset(cfg.get("allowed_tools", []))
        agents[name] = AgentPolicy(**cfg)
    return Policy(agents=agents, drift=DriftPolicy(**raw.get("drift", {})))


def load_policy(path: str | Path) -> Policy:
    with open(path, encoding="utf-8") as fh:
        return policy_from_dict(json.load(fh))
