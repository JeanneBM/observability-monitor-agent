"""Evaluate the monitor against injected-anomaly scenarios."""
from __future__ import annotations

from dataclasses import dataclass

from .engine import analyze, build_baseline
from .policy import Policy
from .scenarios import build_scenarios, clean_traffic


@dataclass
class Result:
    id: str
    title: str
    expected: frozenset[str]
    found: frozenset[str]

    @property
    def passed(self) -> bool:
        if not self.expected:                 # clean scenario: must stay silent
            return not self.found
        return self.expected <= self.found    # injected anomaly: must be detected

    @property
    def extra(self) -> frozenset[str]:
        return self.found - self.expected


def run_eval(policy: Policy, seed: int = 7) -> list[Result]:
    baseline = build_baseline(clean_traffic(40, 0.1, seed))
    results = []
    for s in build_scenarios(seed):
        findings = analyze(s.events, policy, baseline if s.uses_baseline else None)
        results.append(Result(s.id, s.title, s.expected_rules,
                              frozenset(f.rule for f in findings)))
    return results


def format_report(results: list[Result]) -> str:
    lines = [f"{'ID':<5}{'verdict':<8}{'expected':<26}{'found':<30}title"]
    for r in results:
        lines.append(
            f"{r.id:<5}{'PASS' if r.passed else 'FAIL':<8}"
            f"{','.join(sorted(r.expected)) or '-':<26}"
            f"{','.join(sorted(r.found)) or '-':<30}{r.title}"
        )
    ok = sum(r.passed for r in results)
    lines.append(f"\n{ok}/{len(results)} scenarios passed")
    return "\n".join(lines)
