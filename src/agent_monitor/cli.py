from __future__ import annotations

import argparse
import json
import sys

from .engine import analyze, build_baseline, load_baseline, save_baseline
from .evaluate import format_report, run_eval
from .policy import load_policy
from .scenarios import build_scenarios, clean_traffic
from .schema import dump_events, load_events
from .triage import AzureOpenAISummarizer, triage


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-monitor", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="analyze a JSONL event file")
    a.add_argument("events")
    a.add_argument("--policy", required=True)
    a.add_argument("--baseline")
    a.add_argument("--json", action="store_true", help="machine-readable output")
    a.add_argument("--llm", action="store_true", help="LLM triage summary (Azure OpenAI)")

    b = sub.add_parser("baseline", help="build a baseline from known-good events")
    b.add_argument("events")
    b.add_argument("--out", required=True)

    e = sub.add_parser("eval", help="run injected-anomaly evaluation")
    e.add_argument("--policy", required=True)

    g = sub.add_parser("generate", help="write sample telemetry to a directory")
    g.add_argument("--out", required=True)

    args = p.parse_args(argv)

    if args.cmd == "analyze":
        policy = load_policy(args.policy)
        baseline = load_baseline(args.baseline) if args.baseline else None
        findings = analyze(load_events(args.events), policy, baseline)
        if args.json:
            print(json.dumps([f.to_dict() for f in findings], indent=2))
        else:
            print(triage(findings, AzureOpenAISummarizer() if args.llm else None))
        return 1 if any(f.severity in ("high", "critical") for f in findings) else 0

    if args.cmd == "baseline":
        save_baseline(build_baseline(load_events(args.events)), args.out)
        return 0

    if args.cmd == "eval":
        results = run_eval(load_policy(args.policy))
        print(format_report(results))
        return 0 if all(r.passed for r in results) else 1

    if args.cmd == "generate":
        import os

        os.makedirs(args.out, exist_ok=True)
        dump_events(clean_traffic(40, 0.1), f"{args.out}/baseline_events.jsonl")
        save_baseline(build_baseline(clean_traffic(40, 0.1)), f"{args.out}/baseline.json")
        for s in build_scenarios():
            dump_events(s.events, f"{args.out}/{s.id}.jsonl")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
