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


def inspect_plans(api, plans):
    """Complete readback; absence is NOT proof that a request was never sent."""
    from scripts.feishu_io import same
    tables, issues = {}, []
    # Read each affected table completely, then verify every field of every
    # planned mutation, including later updates to an earlier row.
    for table, plan in plans.get('tables', {}).items():
        prior_size = getattr(api,'read_page_size',2000)
        try:
            api.read_page_size = 2000
            actual = api.read(table)
        finally:
            api.read_page_size = prior_size
        key = plan['key']
        keyed = [r for r in actual if r.get(key)]
        index = {r[key]:r for r in keyed}
        duplicate = len(index) != len(keyed)
        outcomes = []
        for identity, expected in plan['rows'].items():
            row = index.get(identity)
            mismatches = list(expected) if row is None else [k for k,v in expected.items()
                             if not same(v, api._present(table, row).get(k))]
            status = ('duplicate_key' if duplicate else 'not_observed' if row is None
                      else 'conflicting_fields' if mismatches else 'confirmed')
            outcome = {'identity': identity, 'status': status, 'differing_fields': mismatches}
            outcomes.append(outcome)
            if status != 'confirmed': issues.append({'table':table, **outcome})
        # Even an empty plan cannot hide duplicate existing keys.
        if duplicate and not outcomes: issues.append({'table': table, 'status': 'duplicate_key'})
        tables[table] = {'read_rows':len(actual), 'verified_plan_rows':sum(r['status']=='confirmed' for r in outcomes),
                         'planned_rows': len(outcomes), 'outcomes':outcomes}
    return {'status':'requires_reconciliation' if issues else 'confirmed', 'tables':tables, 'issues':issues,
            'business_writes':0, 'messages_created':0}


def verify_plans(api, plans):
    result = inspect_plans(api, plans)
    if result['issues']:
        raise RuntimeError('Recovery plan differs from complete readback: '+
                           ', '.join(sorted({i['table'] for i in result['issues']})))
    return {name: {k:v[k] for k in ('read_rows','verified_plan_rows')} for name,v in result['tables'].items()}


def recovery_report(root, checkpoint, meta, journal, lock, api, day, check_process=True):
    """Produce a repeatable, non-mutating recovery decision from remote evidence."""
    from scripts.shared_runtime import digest
    from scripts.stage_checkpoint import reconstruct
    result={'schema':'ebook-recovery-plan.v1','day':day,'base_generation':meta['generation'],
            'business_writes':0,'messages_created':0,'blockers':[], 'ready_to_resume':False}
    if not journal or journal.get('parent_generation') != meta['generation']:
        return {**result,'status':'no_matching_interrupted_stage'}
    recovered=reconstruct(root,checkpoint,meta['generation'],journal)
    result.update(stage=journal['stage'], phase=journal['phase'])
    if journal.get('phase') == 'completed' and recovered == checkpoint:
        return {**result, 'status':'already_committed'}
    if lock.get('phase')!='busy' or journal['lease_sha256']!=digest(lock.get('token','').encode()):
        result['blockers'].append('Original lease does not match; never acquire or steal it')
    if recovered['day'] != day:result['blockers'].append('Historical execution cannot resume automatically')
    guards=recovered.get('unresolved_guards',[])
    result['unresolved_guards']=guards
    if guards:result['blockers'].append('Unknown mutation guards require reconciliation; no guard is cleared')
    def content(name, default):
        value=recovered['files'].get('runs/'+recovered['day']+'/'+name)
        return json.loads(value['text']) if value else default
    progress=content(PROGRESS,{})
    completed=progress.get('completed',{})
    valid={k:v for k,v in completed.items() if v.get('status') in {'completed','partial','ok'}}
    if len(valid)!=len(completed):result['blockers'].append('Invalid completed-stage status')
    result['completed_stages']=sorted(valid)
    result['remaining_stages']=sorted(REUSABLE-set(valid))
    result['publication_readback_required']=True
    result['process_stopped_verified']=False
    if check_process:
        try:
            assert_original_stopped(progress.get('execution',{}),root)
            result['process_stopped_verified']=True
        except RuntimeError as exc:result['blockers'].append(str(exc))
    else:result['blockers'].append('Original process has not been verified stopped')
    # Reconcile even guarded/partial plans, but never use a mismatch to resend.
    result['readback']=inspect_plans(api,content(PLANS,{'tables':{}}))
    if result['readback']['issues']:result['blockers'].append('Full table readback does not confirm all planned writes')
    result['ready_to_resume']=not result['blockers']
    result['status']='ready_for_controlled_resume' if result['ready_to_resume'] else 'requires_reconciliation'
    result['next_action']=('python scripts/daily_run.py --run-kind scheduled --resume-interrupted'
                           if result['ready_to_resume'] else 'Keep evidence and original lease; reconcile blockers without replaying writes')
    return result


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
