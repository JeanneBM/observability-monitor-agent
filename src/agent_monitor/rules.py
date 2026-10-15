"""Deterministic detection rules. Pure functions: events in, findings out.

Run-level rules receive the time-ordered events of a single (agent, run_id).
No rule calls an LLM, the network, or mutates its input.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

from .policy import Policy
from .schema import Event, Finding

RunRule = Callable[[list[Event], Policy], list[Finding]]


def permission_violation(run: list[Event], policy: Policy) -> list[Finding]:
    agent = run[0].agent
    ap = policy.agent(agent)
    run_id = run[0].run_id
    if ap is None:
        return [
            Finding(
                "unknown_agent", "high", agent, run_id,
                f"agent '{agent}' has no policy entry; its actions cannot be checked",
                (run[0].id,),
            )
        ]
    out: list[Finding] = []
    seen: set[str] = set()
    for e in run:
        if e.type == "tool_call" and e.tool not in ap.allowed_tools and e.tool not in seen:
            seen.add(e.tool or "?")
            out.append(
                Finding(
                    "permission_violation", "critical", agent, run_id,
                    f"tool '{e.tool}' is outside the agent's allowed set",
                    (e.id,),
                )
            )
    return out


def tool_loop(run: list[Event], policy: Policy) -> list[Finding]:
    ap = policy.agent(run[0].agent)
    if ap is None:
        return []
    calls = [e for e in run if e.type == "tool_call"]
    counts = Counter((e.tool, e.args_hash) for e in calls)
    out = []
    for (tool, h), n in counts.items():
        if n > ap.max_tool_repeats:
            ids = tuple(e.id for e in calls if (e.tool, e.args_hash) == (tool, h))
            out.append(
                Finding(
                    "tool_loop", "medium", run[0].agent, run[0].run_id,
                    f"'{tool}' called {n}x with identical arguments (limit {ap.max_tool_repeats})",
                    ids[:5],
                )
            )
    return out


def budget_exceeded(run: list[Event], policy: Policy) -> list[Finding]:
    ap = policy.agent(run[0].agent)
    if ap is None:
        return []
    tokens = sum(e.tokens for e in run)
    cost = sum(e.cost_usd for e in run)
    out = []
    if tokens > ap.max_tokens_per_run:
        out.append(
            Finding(
                "budget_exceeded", "medium", run[0].agent, run[0].run_id,
                f"run used {tokens} tokens (limit {ap.max_tokens_per_run})",
                (run[-1].id,),
            )
        )
    if cost > ap.max_cost_per_run:
        out.append(
            Finding(
                "budget_exceeded", "medium", run[0].agent, run[0].run_id,
                f"run cost ${cost:.2f} (limit ${ap.max_cost_per_run:.2f})",
                (run[-1].id,),
            )
        )
    return out


def unverified_claim(run: list[Event], policy: Policy) -> list[Finding]:
    """A claimed fix must be followed by an independent, successful verification."""
    ap = policy.agent(run[0].agent)
    if ap is None or not ap.require_verification:
        return []
    out = []
    for i, e in enumerate(run):
        if e.type == "claim" and e.status == "fixed":
            later = [v for v in run[i + 1:] if v.type == "verification"]
            if not later:
                out.append(
                    Finding(
                        "unverified_claim", "high", e.agent, e.run_id,
                        "agent claimed 'fixed' but no verification followed",
                        (e.id,),
                    )
                )
            elif later[-1].status != "ok":
                out.append(
                    Finding(
                        "claim_contradicted", "high", e.agent, e.run_id,
                        f"agent claimed 'fixed' but verification returned '{later[-1].status}'",
                        (e.id, later[-1].id),
                    )
                )
    return out


def citation_check(run: list[Event], policy: Policy) -> list[Finding]:
    ap = policy.agent(run[0].agent)
    if ap is None or not ap.require_citations:
        return []
    out = []
    for e in run:
        if e.type != "answer":
            continue
        if not e.retrieved:
            out.append(
                Finding(
                    "ungrounded_answer", "high", e.agent, e.run_id,
                    "answer produced without any retrieval results", (e.id,),
                )
            )
            continue
        bad = [c for c in e.citations if c not in set(e.retrieved)]
        if bad or not e.citations:
            why = f"cites sources not in retrieval: {bad}" if bad else "answer has no citations"
            out.append(Finding("citation_mismatch", "high", e.agent, e.run_id, why, (e.id,)))
    return out


RUN_RULES: tuple[RunRule, ...] = (
    permission_violation,
    tool_loop,
    budget_exceeded,
    unverified_claim,
    citation_check,
)
