# Agent Monitor — PoC

A read-only monitor for AI-agent telemetry. It watches what other agents *did* (tool calls, token use, claims, citations, escalations) and flags anomalies, instead of trusting what they *say* they did.

Built as a companion to the Azure Kubernetes Agentic Ops, Foundry RAG and LangGraph CI Repair projects, which all share the same ideas: separated permissions, deterministic checks, human escalation.

## Design

- **Read-only.** The monitor has no tools and no write path to the agents it watches. It flags and escalates; a human decides.
- **Telemetry in, not conclusions.** Input is a shared JSONL event schema (`schema.py`). Raw tool arguments are never stored, only a hash.
- **Deterministic detection first.** Every rule is a pure function with no LLM or network call.
- **LLM only for triage.** `triage.py` summarises findings for humans. It sees findings, not raw traffic, and if it fails the deterministic report is still produced.
- **The monitor is evaluated like the agents.** Anomalies are injected on purpose (M01–M09) and a clean scenario (C00) must stay silent.

## Rules

| Rule | Severity | Detects |
|---|---|---|
| `permission_violation` | critical | tool call outside the agent's allowlist (e.g. diagnostic agent calling a patch tool) |
| `unknown_agent` | high | agent with no policy entry |
| `unverified_claim` | high | claimed `fixed`, no verification afterwards |
| `claim_contradicted` | high | claimed `fixed`, verification returned `failed` |
| `ungrounded_answer` | high | answer without any retrieval results |
| `citation_mismatch` | high | citation not in the retrieved set, or no citations |
| `tool_loop` | medium | same tool + same args repeated beyond limit within a run |
| `budget_exceeded` | medium | tokens or cost per run above policy limit |
| `behavior_drift` | medium | escalation rate or mean tool calls/run far from a known-good baseline |

Limits live in `examples/policy.json`, per agent.

## Quick start

```bash
pip install -e ".[dev]"
pytest

agent-monitor eval --policy examples/policy.json          # injected-anomaly evaluation
agent-monitor generate --out sample/                      # sample JSONL + baseline
agent-monitor analyze sample/M04.jsonl --policy examples/policy.json
agent-monitor analyze sample/M08.jsonl --policy examples/policy.json --baseline sample/baseline.json
agent-monitor analyze sample/M01.jsonl --policy examples/policy.json --json
```

`analyze` exits with code 1 when any `high`/`critical` finding exists, so it can gate a pipeline.

Optional LLM triage (`--llm`) needs `pip install -e ".[llm]"`, `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_DEPLOYMENT`, and authenticates with Entra ID (works with Workload Identity). This path is not covered by the test-suite.

## Event schema

One JSON object per line: `id, ts, agent, run_id, type` plus optional `tool, args_hash, tokens, cost_usd, latency_ms, status, citations, retrieved, attrs`.
Types: `run_started, tool_call, llm_call, escalation, claim, verification, answer, run_finished`.

To integrate an existing agent, emit these events from its tool wrapper (or map OpenTelemetry spans to them).

## Layout

```
src/agent_monitor/
  schema.py     event + finding types, JSONL I/O
  policy.py     per-agent allowlists, budgets, drift thresholds
  rules.py      deterministic rules
  engine.py     run grouping, baseline, drift, analyze()
  scenarios.py  synthetic traffic + injected anomalies
  evaluate.py   scenario evaluation harness
  triage.py     optional LLM summary (offline fallback)
  cli.py        analyze | baseline | eval | generate
tests/          pytest suite
examples/       policy.json
```

## Limitations

- Telemetry is synthetic; no live AKS / Foundry / LangGraph integration yet.
- Drift detection uses simple thresholds against a fixed baseline, not statistical tests.
- Batch analysis only; no streaming or alert delivery.
- Rules are only as good as the telemetry: an agent that does not emit events is invisible.

## Next steps

1. Emit these events from the existing three projects.
2. Map OpenTelemetry traces to the schema.
3. Add rolling-window drift with proper statistical tests.
4. Alert delivery (e.g. Azure Monitor) with human-in-the-loop acknowledgement.
