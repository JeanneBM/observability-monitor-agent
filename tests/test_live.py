import json
import time
from pathlib import Path
import pytest
from agent_monitor.live import Collector
from agent_monitor.schema import Event

NOW = time.time()

def event(agent='rag', kind='answer', id='1', **values):
    return {**dict(id=id, ts=NOW, agent=agent, run_id='run', type=kind), **values}


def setup(tmp_path, sources):
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'agents': {
        'rag': {'allowed_tools': ['knowledge_base_retrieve'], 'require_citations': True},
        'diagnostic': {'allowed_tools': ['kubectl_get']},
        'remediation': {'allowed_tools': ['kubectl_patch'], 'require_verification': True},
        'ci-repair': {'allowed_tools': ['policy_check']}}}))
    return {'instance': 'test', 'policy': str(policy), 'sources': sources}


def test_file_cursor_survives_restart_and_waits_for_partial_record(tmp_path):
    file = tmp_path / 'rag.jsonl'
    payload = json.dumps(event(citations=['missing'], retrieved=['real']))
    file.write_text(payload[:20])
    config = setup(tmp_path, [{'name':'rag','type':'jsonl','path':str(file),'agent':'rag'}])
    c = Collector(config, tmp_path / 'reports')
    assert c.cycle()['events'] == 0
    with file.open('a') as f: f.write(payload[20:] + '\n')
    assert c.cycle()['events'] == 1
    assert json.loads((c.output / 'findings.json').read_text())[0]['rule'] == 'citation_mismatch'
    c.db.close()
    c = Collector(config, tmp_path / 'reports')
    assert c.cycle()['events'] == 1
    assert c.db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0] == 1
    c.db.close()


def test_missing_source_is_not_reported_as_no_anomalies(tmp_path):
    c = Collector(setup(tmp_path,[{'name':'rag','type':'jsonl','path':str(tmp_path/'missing'),'agent':'rag'}]),tmp_path/'out')
    report = c.cycle()
    assert report['status'] == 'degraded' and report['highFindings'] == 1
    c.db.close()


def test_kubernetes_reads_both_roles_and_detects_permission_violation(tmp_path):
    commands = []
    def command(args):
        commands.append(args)
        if 'get' in args:
            return json.dumps({'items':[{'metadata':{'name':role,'labels':{'role':role}},
                'status':{'containerStatuses':[{'ready':True,'restartCount':0}]}}
                for role in ('diagnostic','remediation')]})
        role = args[args.index('logs') + 1]
        e = event(agent=role,kind='tool_call',id=role,tool='kubectl_patch',args_hash='hash')
        return 'ordinary log\n' + json.dumps({'agent_monitor_event': e})
    config = setup(tmp_path,[{'type':'kubernetes','name':'aks','namespace':'agents','kubeconfig':'scoped'}])
    c = Collector(config,tmp_path/'out',command=command)
    report = c.cycle()
    assert report['events'] == 2
    assert report['sources']['aks']['status'] == 'watching'
    findings = json.loads((c.output/'findings.json').read_text())
    assert any(f['rule']=='permission_violation' and f['agent']=='diagnostic' for f in findings)
    assert all('get' in args or 'logs' in args for args in commands)
    assert c.cycle()['events'] == 2
    c.db.close()


def test_source_cannot_impersonate_another_agent(tmp_path):
    file=tmp_path/'events';file.write_text(json.dumps(event(agent='remediation',kind='run_started'))+'\n')
    c=Collector(setup(tmp_path,[{'type':'jsonl','name':'rag','path':str(file),'agent':'rag'}]),tmp_path/'out')
    report=c.cycle()
    assert report['events']==0 and report['status']=='degraded'
    assert json.loads((c.output/'findings.json').read_text())[0]['rule']=='source_identity_mismatch'
    c.db.close()


def test_inflight_claim_waits_for_verification_and_closed_claim_alerts(tmp_path):
    file=tmp_path/'events'
    events=[event('remediation','run_started',id='s'), event('remediation','claim',id='c',status='fixed')]
    file.write_text(''.join(json.dumps(e)+'\n' for e in events))
    c=Collector(setup(tmp_path,[{'type':'jsonl','name':'remediation','path':str(file),'agent':'remediation'}]),tmp_path/'out')
    assert c.cycle()['highFindings']==0
    with file.open('a') as f: f.write(json.dumps(event('remediation','run_finished',id='f',status='success'))+'\n')
    assert c.cycle()['highFindings']==1
    c.db.close()


def test_valid_abstention_is_not_a_fabricated_answer(tmp_path):
    file=tmp_path/'events';file.write_text(json.dumps(event(status='abstained',citations=[],retrieved=[]))+'\n')
    c=Collector(setup(tmp_path,[{'type':'jsonl','name':'rag','path':str(file),'agent':'rag'}]),tmp_path/'out')
    assert c.cycle()['highFindings']==0
    c.db.close()


@pytest.mark.parametrize('changes',[{'tokens':-1},{'ts':float('nan')},{'citations':'text'},{'attrs':[]}])
def test_invalid_numeric_and_collection_fields_rejected(changes):
    with pytest.raises(ValueError): Event.from_dict(event(**changes))


def test_real_watch_process_collects_appends_and_stops(tmp_path):
    import os
    import subprocess
    import sys
    source = tmp_path/'rag.jsonl'; source.touch()
    config = setup(tmp_path,[{'name':'rag','type':'jsonl','agent':'rag','path':str(source)}])
    config_path=tmp_path/'sources.json';config_path.write_text(json.dumps(config))
    output=tmp_path/'worker'
    env=os.environ.copy();env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
    process=subprocess.Popen([sys.executable,'-m','agent_monitor','watch','--config',str(config_path),
                              '--output',str(output),'--interval','1'],env=env,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    def wait_for(predicate):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if process.poll() is not None: raise AssertionError(process.stderr.read().decode())
            if predicate():return
            time.sleep(0.05)
        raise AssertionError('watch did not update in time')
    try:
        wait_for(lambda:(output/'health.json').exists())
        with source.open('a') as file:
            file.write(json.dumps(event(citations=['fake'],retrieved=['real']))+'\n')
        wait_for(lambda:json.loads((output/'health.json').read_text())['highFindings']==1)
        (output/'stop').touch()
        assert process.wait(timeout=10)==0
    finally:
        if process.poll() is None:
            process.terminate();process.wait(timeout=10)
