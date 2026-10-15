import copy
import pathlib

import pytest

from agent_monitor import analyze, load_policy
from agent_monitor.engine import build_baseline
from agent_monitor.evaluate import run_eval
from agent_monitor.schema import Event, dump_events, load_events
from agent_monitor.scenarios import build_scenarios, clean_traffic
from agent_monitor.triage import OfflineSummarizer, triage

POLICY = load_policy(pathlib.Path(__file__).parents[1] / "examples" / "policy.json")
SCENARIOS = {s.id: s for s in build_scenarios()}
BASELINE = build_baseline(clean_traffic(40, 0.1))


def rules_for(sid):
    s = SCENARIOS[sid]
    return {f.rule for f in analyze(s.events, POLICY, BASELINE if s.uses_baseline else None)}


def test_clean_traffic_produces_no_findings():
    assert analyze(clean_traffic(40, 0.1, seed=99), POLICY) == []


@pytest.mark.parametrize("sid", [s for s in SCENARIOS if s != "C00"])
def test_injected_anomaly_is_detected(sid):
    assert SCENARIOS[sid].expected_rules <= rules_for(sid)


def test_eval_harness_all_pass():
    results = run_eval(POLICY)
    assert all(r.passed for r in results), [r.id for r in results if not r.passed]


def test_permission_violation_is_critical_and_cites_event():
    f = analyze(SCENARIOS["M01"].events, POLICY)[0]
    assert (f.rule, f.severity) == ("permission_violation", "critical")
    assert f.evidence and f.evidence[0].startswith("m01-")


def test_loop_below_limit_is_not_flagged():
    from agent_monitor.scenarios import RunBuilder

    b = RunBuilder("diagnostic", "ok").start()
    for _ in range(3):
        b.tool("kubectl_logs", {"pod": "x"})
    assert analyze(b.finish().events, POLICY) == []


def test_drift_needs_baseline_and_min_runs():
    events = SCENARIOS["M08"].events
    assert not any(f.rule == "behavior_drift" for f in analyze(events, POLICY))
    few = [e for e in events if int(e.run_id.split("-")[-1]) < 5]
    assert not any(f.rule == "behavior_drift" for f in analyze(few, POLICY, BASELINE))


def test_analysis_is_read_only():
    events = SCENARIOS["M04"].events
    before = copy.deepcopy([e.to_dict() for e in events])
    analyze(events, POLICY, BASELINE)
    assert [e.to_dict() for e in events] == before


def test_jsonl_roundtrip(tmp_path):
    events = SCENARIOS["M06"].events
    path = tmp_path / "e.jsonl"
    dump_events(events, path)
    assert [e.to_dict() for e in load_events(path)] == [e.to_dict() for e in events]


def test_unknown_event_type_rejected():
    with pytest.raises(ValueError):
        Event(id="1", ts=0, agent="a", run_id="r", type="nope")


def test_triage_falls_back_when_llm_fails():
    class Broken:
        def summarize(self, findings):
            raise RuntimeError("endpoint down")

    findings = analyze(SCENARIOS["M01"].events, POLICY)
    text = triage(findings, Broken())
    assert "permission_violation" in text and "LLM triage unavailable" in text


def test_offline_summary_empty():
    assert OfflineSummarizer().summarize([]) == "No anomalies detected."
