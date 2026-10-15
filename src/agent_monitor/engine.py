"""Analysis engine: groups events into runs, applies rules, checks drift."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .policy import Policy
from .rules import RUN_RULES
from .schema import SEVERITY_ORDER, Event, Finding


def group_runs(events: Iterable[Event]) -> dict[tuple[str, str], list[Event]]:
    runs: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for e in events:
        runs[(e.agent, e.run_id)].append(e)
    for evs in runs.values():
        evs.sort(key=lambda e: (e.ts, e.id))
    return dict(runs)


def agent_stats(events: Iterable[Event]) -> dict[str, dict[str, float]]:
    """Per-agent behavioural metrics used as a baseline for drift detection."""
    stats: dict[str, dict[str, float]] = {}
    for (agent, _), run in group_runs(events).items():
        finished = [e for e in run if e.type == "run_finished"]
        if not finished:
            continue
        s = stats.setdefault(agent, {"runs": 0, "escalated": 0, "tool_calls": 0})
        s["runs"] += 1
        s["escalated"] += finished[-1].status == "escalated"
        s["tool_calls"] += sum(e.type == "tool_call" for e in run)
    return {
        a: {
            "runs": s["runs"],
            "escalation_rate": s["escalated"] / s["runs"],
            "mean_tool_calls": s["tool_calls"] / s["runs"],
        }
        for a, s in stats.items()
    }


def build_baseline(events: Iterable[Event]) -> dict[str, dict[str, float]]:
    return agent_stats(events)


def save_baseline(baseline: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")


def load_baseline(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def drift_findings(events: list[Event], policy: Policy, baseline: dict) -> list[Finding]:
    out: list[Finding] = []
    d = policy.drift
    for agent, cur in agent_stats(events).items():
        base = baseline.get(agent)
        if not base or cur["runs"] < d.min_runs:
            continue
        rise = cur["escalation_rate"] - base["escalation_rate"]
        if rise > d.escalation_delta:
            out.append(
                Finding(
                    "behavior_drift", "medium", agent, None,
                    f"escalation rate {cur['escalation_rate']:.0%} vs baseline "
                    f"{base['escalation_rate']:.0%} over {int(cur['runs'])} runs",
                )
            )
        b = base["mean_tool_calls"]
        if b > 0 and cur["mean_tool_calls"] / b > d.tool_calls_ratio:
            out.append(
                Finding(
                    "behavior_drift", "medium", agent, None,
                    f"mean tool calls/run {cur['mean_tool_calls']:.1f} vs baseline {b:.1f}",
                )
            )
    return out


def analyze(
    events: list[Event], policy: Policy, baseline: dict | None = None
) -> list[Finding]:
    """Read-only analysis. Does not modify `events`."""
    findings: list[Finding] = []
    for run in group_runs(events).values():
        for rule in RUN_RULES:
            findings.extend(rule(run, policy))
    if baseline:
        findings.extend(drift_findings(events, policy, baseline))
    return sorted(
        findings,
        key=lambda f: (-SEVERITY_ORDER[f.severity], f.agent, f.run_id or "", f.rule),
    )
