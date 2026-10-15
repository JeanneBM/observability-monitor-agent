"""Continuous read-only collection: append-only JSONL and Kubernetes pod logs.

SQLite preserves deduplication and file cursors across restarts. Reports are atomic;
raw log lines and malformed records are never persisted in diagnostics.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

from .engine import analyze, load_baseline
from .policy import load_policy
from .schema import Event, Finding


def atomic(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


class Collector:
    def __init__(self, config, output, command=None, clock=time.time):
        self.config = config
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.command = command or self._command
        self.policy = load_policy(config['policy'])
        self.baseline = load_baseline(config['baseline']) if config.get('baseline') else None
        self.db = sqlite3.connect(self.output / 'events.sqlite')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT NOT NULL, ts REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors (path TEXT PRIMARY KEY, identity TEXT, position INTEGER);
            CREATE TABLE IF NOT EXISTS alerts (fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL, seen REAL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.sources = {}
        self.errors = []

    @staticmethod
    def _command(argv):
        result = subprocess.run(argv, text=True, capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError('read_only_kubernetes_request_failed')
        return result.stdout

    def problem(self, rule, source, message):
        self.errors.append(Finding(rule, 'high', source, None, message))

    def ingest(self, data, source, expected=None):
        if not isinstance(data, dict):
            raise ValueError('event_not_object')
        event = Event.from_dict(data)
        if expected and event.agent != expected:
            self.problem('source_identity_mismatch', source, 'Event identity does not match its observed source')
            return
        if abs(event.ts - self.clock()) > 7 * 86400:
            raise ValueError('event_timestamp_outside_retention')
        # Accept only metadata used by the contract, not arbitrary producer payloads.
        allowed = {'usage_available', 'claim_id', 'verifier', 'error_type'}
        event.attrs = {key: value for key, value in event.attrs.items() if key in allowed}
        event.attrs['observed_source'] = source
        payload = json.dumps(event.to_dict(), sort_keys=True)
        prior = self.db.execute('SELECT payload FROM events WHERE id=?', (event.id,)).fetchone()
        if prior and prior[0] != payload:
            self.problem('event_id_collision', source, 'An event ID was reused for different data')
            return
        self.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)', (event.id, payload, event.ts))

    def file(self, source):
        name, path = source['name'], Path(source['path'])
        count = 0
        try:
            stat = path.stat()
            identity = f'{stat.st_dev}:{stat.st_ino}'
            prior = self.db.execute('SELECT identity,position FROM cursors WHERE path=?', (str(path),)).fetchone()
            offset = prior[1] if prior and prior[0] == identity and prior[1] <= stat.st_size else 0
            with path.open('rb') as file:
                file.seek(offset)
                while True:
                    position = file.tell()
                    raw = file.readline(65537)
                    if not raw:
                        break
                    if not raw.endswith(b'\n') and len(raw) <= 65536:
                        file.seek(position)  # Wait for a concurrent append to complete the record.
                        break
                    if len(raw) > 65536:
                        raise ValueError('oversized_event')
                    try:
                        self.ingest(json.loads(raw), name, source['agent'])
                        count += 1
                    except (ValueError, TypeError, KeyError):
                        self.problem('invalid_telemetry', name, 'Invalid JSONL event; record rejected')
                offset = file.tell()
            self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?)', (str(path), identity, offset))
            self.sources[name] = {'status': 'watching', 'newRecords': count,
                                  'agent': source['agent'], 'onDemand': True}
        except (OSError, ValueError):
            self.sources[name] = {'status': 'unavailable'}
            self.problem('source_unavailable', name, 'Telemetry file cannot be read; coverage is incomplete')

    def kubernetes(self, source):
        name = source['name']
        base = ['kubectl', '--kubeconfig', source['kubeconfig'], '-n', source['namespace']]
        try:
            expiry = source.get('credentialExpiresAt')
            if expiry and self.clock() >= expiry:
                raise RuntimeError('collector_credential_expired')
            pods = json.loads(self.command([*base, 'get', 'pods', '-l', 'app=agentic-ops', '-o', 'json']))['items']
            roles = set()
            prior = self.db.execute("SELECT value FROM metadata WHERE key='kube_last_poll'").fetchone()
            start = float(prior[0]) - 5 if prior else self.clock() - 600
            since = datetime.fromtimestamp(start, timezone.utc).isoformat().replace('+00:00', 'Z')
            for pod in pods:
                role = pod['metadata'].get('labels', {}).get('role')
                if role not in ('diagnostic', 'remediation'):
                    continue
                roles.add(role)
                statuses = pod.get('status', {}).get('containerStatuses') or []
                if not statuses or not all(s.get('ready') for s in statuses):
                    self.problem('agent_unhealthy', role, 'Agent pod is not ready')
                pod_name = pod['metadata']['name']
                for previous in ([False, True] if any(s.get('restartCount', 0) for s in statuses) else [False]):
                    args = [*base, 'logs', pod_name, '--all-containers=true', '--since-time', since,
                            '--tail=10000']
                    if previous:
                        args.append('--previous=true')
                    logs = self.command(args)
                    if len(logs.splitlines()) >= 10000:
                        self.problem('collection_truncated', role, 'Log tail limit reached; telemetry may be incomplete')
                    for raw in logs.splitlines():
                        try:
                            envelope = json.loads(raw)
                        except ValueError:
                            continue  # Ordinary application logs are not telemetry.
                        if not isinstance(envelope, dict) or 'agent_monitor_event' not in envelope:
                            continue
                        try:
                            self.ingest(envelope['agent_monitor_event'], name, role)
                        except (ValueError, TypeError, KeyError):
                            self.problem('invalid_telemetry', role, 'Invalid Kubernetes event; record rejected')
            if roles != {'diagnostic', 'remediation'}:
                raise RuntimeError('agent_pods_missing')
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('kube_last_poll',?)", (str(self.clock()),))
            self.sources[name] = {'status': 'watching', 'roles': sorted(roles), 'pods': len(pods)}
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired):
            self.sources[name] = {'status': 'unavailable'}
            self.problem('source_unavailable', name, 'Kubernetes source unavailable; check collector credentials and pod health')

    def cycle(self):
        self.sources, self.errors = {}, []
        for source in self.config['sources']:
            if source['type'] == 'jsonl':
                self.file(source)
            elif source['type'] == 'kubernetes':
                self.kubernetes(source)
            else:
                self.problem('source_unavailable', source['name'], 'Unsupported source type')
        # Retain entire runs; never drop an early claim but keep only its verification.
        cutoff = self.clock() - self.config.get('retentionHours', 24) * 3600
        self.db.execute('''DELETE FROM events WHERE json_extract(payload,'$.agent') || ':' ||
            json_extract(payload,'$.run_id') IN
            (SELECT json_extract(payload,'$.agent') || ':' || json_extract(payload,'$.run_id')
             FROM events GROUP BY json_extract(payload,'$.agent'), json_extract(payload,'$.run_id')
             HAVING MAX(ts) < ?)''', (cutoff,))
        events = [Event.from_dict(json.loads(row[0])) for row in self.db.execute('SELECT payload FROM events ORDER BY ts,id')]
        findings = analyze(events, self.policy, self.baseline)
        findings.extend(self.errors)
        from .engine import group_runs
        for (agent, run_id), run in group_runs(events).items():
            if not any(e.type == 'run_finished' for e in run) and self.clock() - run[-1].ts > self.config.get('runIdleTimeoutSeconds', 600):
                findings.append(Finding('run_stalled', 'high', agent, run_id,
                                        'Run has no terminal event and exceeded the idle timeout', (run[-1].id,)))
        for event in events:
            if event.type == 'run_finished' and event.status == 'error':
                findings.append(Finding('run_failed', 'high', event.agent, event.run_id,
                                        'Agent execution ended with an error', (event.id,)))
        # De-duplicate current findings, and persist a durable alert history once per fingerprint.
        current = {}
        for finding in findings:
            item = finding.to_dict()
            fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
            current[fingerprint] = item
            self.db.execute('INSERT OR IGNORE INTO alerts VALUES (?,?,?)',
                            (fingerprint, json.dumps(item), self.clock()))
        self.db.commit()
        atomic(self.output / 'findings.json', list(current.values()))
        summary = {}
        for agent in self.policy.agents:
            agent_events = [e for e in events if e.agent == agent]
            summary[agent] = {'events': len(agent_events), 'lastEventAt': max((e.ts for e in agent_events), default=None),
                              'idle': not agent_events,
                              'tokenUsageComplete': (all(e.attrs.get('usage_available', False) for e in agent_events if e.type == 'llm_call')
                                                     if any(e.type == 'llm_call' for e in agent_events) else None),
                              'costTelemetryAvailable': False}
        health = {'updatedAt': self.clock(), 'pid': os.getpid(), 'instance': self.config['instance'],
                  'sources': self.sources, 'agents': summary,
                  'status': 'degraded' if self.errors else 'watching', 'events': len(events),
                  'highFindings': sum(i['severity'] in ('high', 'critical') for i in current.values())}
        atomic(self.output / 'health.json', health)
        return health


def watch(config_path, output, *, interval=5, once=False):
    if interval < 1:
        raise ValueError('Polling interval must be at least one second')
    config = json.loads(Path(config_path).read_text())
    collector = Collector(config, output)
    stop = Path(output) / 'stop'
    try:
        while not stop.exists():
            collector.cycle()
            if once:
                break
            for _ in range(interval * 5):
                if stop.exists():
                    break
                time.sleep(0.2)
    finally:
        collector.db.close()
