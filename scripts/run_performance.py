"""Private, execution-scoped timings; never a production success signal."""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import json
import os
from pathlib import Path
import time
import uuid

ENV = 'EBOOK_PERFORMANCE_DIR'
STAGE_ENV = 'EBOOK_PERFORMANCE_STAGE'


def event(kind, started, **values):
    folder = os.environ.get(ENV)
    if not folder:
        return
    row = {'kind': kind, 'stage': os.environ.get(STAGE_ENV, 'coordinator'),
           'finished_at': datetime.now(timezone.utc).isoformat(),
           'seconds': time.monotonic()-started, **values}
    path = Path(folder)/(uuid.uuid4().hex+'.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(row, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


@contextmanager
def span(name):
    old = os.environ.get(STAGE_ENV)
    os.environ[STAGE_ENV] = name
    started = time.monotonic()
    outcome = 'failed'
    try:
        yield
        outcome = 'completed'
    finally:
        event('span', started, name=name, outcome=outcome, parent_stage=old)
        if old is None:
            os.environ.pop(STAGE_ENV, None)
        else:
            os.environ[STAGE_ENV] = old


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def summarize(rows, wall_seconds):
    stages = {}
    children = {}
    for row in rows:
        group = stages.setdefault(row['stage'], {'cli_calls': 0, 'cli_seconds_sum': 0,
            'response_artifact_bytes': 0, 'request_bytes': 0, 'read_retries': 0,
            'source_http_calls': 0, 'source_seconds_sum': 0, 'source_body_bytes': 0, 'backoff_seconds': 0,
            'coordination_http_calls': 0, 'coordination_seconds_sum': 0, 'spans': []})
        if row['kind'] == 'cli':
            group['cli_calls'] += 1
            group['cli_seconds_sum'] += row['seconds']
            group['response_artifact_bytes'] += row.get('response_bytes', 0)
            group['request_bytes'] += row.get('request_bytes', 0)
            group['read_retries'] += int(row.get('attempt', 1) > 1)
        elif row['kind'] == 'coordination_http':
            group['coordination_http_calls'] += 1
            group['coordination_seconds_sum'] += row['seconds']
            group['read_retries'] += int(row.get('attempt', 1) > 1)
        elif row['kind'] == 'source_http':
            group['source_http_calls'] += 1
            group['source_seconds_sum'] += row['seconds']
            group['source_body_bytes'] += row.get('body_bytes', 0)
            group['backoff_seconds'] += row.get('backoff_seconds', 0)
            group['read_retries'] += int(row.get('attempt', 1) > 1)
        elif row['kind'] == 'span':
            group['spans'].append({k: row[k] for k in ('name', 'seconds', 'outcome')})
            if row.get('parent_stage') and row['parent_stage'] != row['stage']:
                children.setdefault(row['parent_stage'], set()).add(row['stage'])
    for name, group in stages.items():
        seen, pending = set(), [name]
        while pending:
            child = pending.pop()
            if child in seen: continue
            seen.add(child); pending.extend(children.get(child, []))
        for field in ('cli_calls','coordination_http_calls','source_http_calls','source_body_bytes','backoff_seconds','response_artifact_bytes','request_bytes','read_retries'):
            group['inclusive_'+field] = sum(stages.get(child, {}).get(field, 0) for child in seen)
    return {'wall_seconds': wall_seconds, 'stages': stages,
            'notes': ['Wall time includes failures and recovery inside this invocation.',
                      'Spans may overlap; their sum is not total elapsed time.',
                      'CLI calls are not HTTP calls. Artifact bytes are not wire traffic.',
                      'Separate invocations are retained separately; gaps must be included in incident comparisons.']}


@contextmanager
def execution(root):
    from scripts.daily_run import atomic_json, shanghai_day
    started = time.monotonic()
    stamp = datetime.now(timezone.utc).isoformat()
    folder = Path(root)/'receipts'/shanghai_day()/('performance-'+uuid.uuid4().hex)
    folder.mkdir(parents=True)
    old = os.environ.get(ENV)
    os.environ[ENV] = str(folder)
    outcome = 'failed'
    try:
        yield
        outcome = 'returned'  # Actual business outcome remains the pipeline's gate.
    finally:
        rows = [json.loads(p.read_text(encoding='utf-8')) for p in folder.glob('*.json')]
        report = {**summarize(rows, time.monotonic()-started), 'started_at': stamp,
                  'finished_at': datetime.now(timezone.utc).isoformat(), 'entry_outcome': outcome,
                  'receipt_directory': str(folder)}
        sources = [r['finished_at'] for r in rows if r['kind']=='span' and r.get('outcome')=='completed'
                   and (r.get('name','').startswith('collect_') or r.get('name') in {'publisher_metrics','publisher_lists','fanqie_ranking_collect'})]
        if sources:
            report['last_source_operation_finished_at'] = max(sources)
            report['seconds_after_last_source_operation'] = (datetime.fromisoformat(report['finished_at'])-
                datetime.fromisoformat(max(sources))).total_seconds()
        atomic_json(folder/'summary.json', report)
        atomic_json(Path(root)/'runs'/shanghai_day()/'performance-summary.json', report)
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old
