# CI Repair shadow monitoring pilot — 2026-11-12

Local verdict: **PASS (4/4)**.

- Monitor revision: [`69af1d1`](https://github.com/JeanneBM/observability-monitor-agent/commit/69af1d122453b6cc2faa7760d456e3a804825359).
- CI Repair revision: [`14a9dc0`](https://github.com/JeanneBM/langgraph-ci-repair-agent/commit/14a9dc08d5f97e761d860bdf86a0fa3a74501378).
- Python 3.12, local demo mode and a continuous `watch` process polling every second. S01 and S05 ran through the standard CI Repair CLI and stopped at `awaiting_approval`. The CLI emitted 7 and 15 events respectively through `AGENT_TELEMETRY_FILE`. No repair was approved.
- The pilot used an isolated policy for agent `ci-repair`. The allowed tools were `policy_check`, `fixture_validation`, and `draft_export`. The JSONL source also required the `ci-repair` identity.
- Regression suites: 30 monitor tests and 52 CI Repair tests passed.

| Check | Observed | Result |
| --- | --- | --- |
| Clean S01 and S05 | 22 events, source `watching`, no current findings and zero rows in SQLite alert history | PASS |
| Controlled anomaly | A separate run emitted three events through the same emitter, including a call named `unapproved_pilot_tool`. No tool ran. The monitor raised exactly one critical `permission_violation` and recorded one alert. | PASS |
| Source loss | Renaming the active JSONL path produced `health.status=degraded` and `health.sources.ci.status=unavailable`. The monitor raised `source_unavailable` and retained the 25 previously ingested events. | PASS |
| Restart and replay | Restoring the same JSONL bytes at the original path with a new inode and starting a new process with the same output directory forced a full replay. All 25 records were read, the event count stayed at 25, and the alert history stayed at two rows with identical payloads. The current source was `watching` and only the injected permission finding remained active. | PASS |

To reproduce locally, create an empty absolute JSONL path, point the monitor's `jsonl` source and `AGENT_TELEMETRY_FILE` to it, and start `agent-monitor watch --config sources.json --output monitor --interval 1`. Run `python -m ci_repair.cli run --mode demo --scenario S01 --thread shadow-S01` and repeat with `S05` and `shadow-S05`. For the anomaly, enter `Trace('ci-repair', 'injected-anomaly')` from `ci_repair.telemetry` and call `trace.tool('unapproved_pilot_tool', None)`. Inspect `findings.json`, `health.json` and the `alerts` table in `events.sqlite` at each stage. Rename the source, wait for degradation, stop via `monitor/stop`, restore the source as a new file, remove the stop flag, and rerun `watch --once` against the same output directory.

## Kubernetes pod logs — PASS

The [GitHub Actions run](https://github.com/JeanneBM/observability-monitor-agent/actions/runs/43188455498) used an ephemeral kind cluster with one log source in the `shadow-smoke` namespace. Two fixture pods, `diagnostic` and `remediation`, each wrote one canonical event envelope to their Kubernetes pod logs.

The monitor's ServiceAccount could get and list pods and get `pods/log` in the namespace. Authorization checks denied creating, patching, and deleting pods. `watch --once` reported source status `watching`, two pods and both roles, two events, and zero findings. The kind post-job cleanup succeeded. The probe used synthetic event payloads and exercised the real Kubernetes API and log transport.

[Exact workflow used for the smoke run](https://github.com/JeanneBM/observability-monitor-agent/blob/f46893b0c919867d575def874ff800f07b86236f/.github/workflows/shadow-kubernetes-smoke.yml)
