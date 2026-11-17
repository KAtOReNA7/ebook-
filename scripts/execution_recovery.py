"""Controlled same-host continuation; never steals a lease or clears a guard.

Remote-host liveness and unknown/partial mutations remain operator reconciliation
cases. Completed remote stages can continue without collecting their sources again.
"""
import json
import os
from pathlib import Path
import subprocess


PROGRESS = 'stage-progress.json'
PLANS = 'stage-write-plans.json'
REUSABLE = {'project_initialization', 'tencent', 'weread', 'fanqie', 'fanqie_rising',
            'publisher_recent', 'project_notifications', 'marketing_expense_notifications',
            'notification_comparison', 'notification_digest', 'notification_health'}


def read(path, default):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).is_file() else default


def record_plan(root, table, key, rows, immutable_fields=None):
    from scripts.stage_checkpoint import context
    from scripts.daily_run import atomic_json
    path, ctx = context(root)
    if path is None or table == '系统运行状态' or not rows:
        return
    target = Path(root)/'runs'/ctx['day']/PLANS
    plans = read(target, {'tables': {}})
    group = plans['tables'].setdefault(table, {'key': key, 'rows': {}})
    if group['key'] != key:
        raise ValueError('Recovery plan key changed within an execution')
    for row in rows:
        item = {k:v for k,v in row.items() if immutable_fields is None or k in immutable_fields}
        group['rows'].setdefault(row[key], {}).update(item)
    atomic_json(target, plans)


def verify_plans(api, plans):
    from scripts.feishu_io import same
    counts = {}
    # Read each affected table completely, then verify every field of every
    # planned mutation, including later updates to an earlier row.
    for table, plan in plans.get('tables', {}).items():
        actual = api.read(table)
        key = plan['key']
        keyed = [r for r in actual if r.get(key)]
        index = {r[key]:r for r in keyed}
        if len(index) != len(keyed):
            raise RuntimeError('Recovery readback contains duplicate keys: '+table)
        for identity, expected in plan['rows'].items():
            row = index.get(identity)
            if row is None or any(not same(v, api._present(table, row).get(k)) for k,v in expected.items()):
                raise RuntimeError('Recovery plan differs from complete readback: '+table)
        counts[table] = {'read_rows': len(actual), 'verified_plan_rows':len(plan['rows'])}
    return counts


def assert_original_stopped(execution, root):
    from scripts.runner_policy import host_fingerprint
    if (execution.get('host') != host_fingerprint() or
            execution.get('root') != str(Path(root).resolve())):
        raise RuntimeError('Original host liveness cannot be verified here; keep lease for reconciliation')
    if not execution.get('pid') or execution['pid'] == os.getpid():
        raise RuntimeError('Original execution identity is missing or still active')
    if os.name != 'nt':
        raise RuntimeError('Controlled process-death recovery is currently verified only on Windows')
    # Conservative: a surviving collector or any other Python runner blocks.
    # Do not emit command lines, which could contain credentials.
    command = "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?(3)?(.exe)?$' } | Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    output = subprocess.check_output(['powershell','-NoProfile','-Command',command],text=True,timeout=20).strip()
    pids = json.loads(output) if output else []
    if isinstance(pids, int): pids = [pids]
    if any(pid != os.getpid() for pid in pids):
        raise RuntimeError('Another Python process is live; original execution/children not proven stopped')


def prepare(root, checkpoint, meta, journal, mutex, api, day):
    from scripts.shared_runtime import digest
    from scripts.stage_checkpoint import reconstruct
    if not journal or journal.get('parent_generation') != meta['generation']:
        raise RuntimeError('No matching interrupted journal; do not infer an execution')
    _, _, lock = mutex.head()
    if lock.get('phase') != 'busy' or journal.get('lease_sha256') != digest(lock.get('token','').encode()):
        raise RuntimeError('Interrupted journal and original lease differ')
    recovered = reconstruct(root, checkpoint, meta['generation'], journal)
    if recovered['day'] != day or recovered.get('unresolved_guards'):
        raise RuntimeError('Historical or unknown mutation requires reconciliation; no automatic continuation')
    files = recovered['files']
    def content(name):
        value = files.get('runs/'+day+'/'+name)
        if not value: raise RuntimeError('Recovery evidence missing: '+name)
        return json.loads(value['text'])
    progress = content(PROGRESS)
    assert_original_stopped(progress['execution'], root)
    mutex.token = lock['token']
    mutex.assert_owned()
    plans = content(PLANS) if 'runs/'+day+'/'+PLANS in files else {'tables':{}}
    counts = verify_plans(api, plans)
    mutex.assert_owned()
    return recovered, progress, counts
