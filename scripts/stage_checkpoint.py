"""Incremental private stage journal. Never grants a lease or retries business writes."""
import base64
import gzip
import json
import os
import time
from pathlib import Path
import uuid

from scripts.shared_runtime import TABLE, LEASE_ENV, capture, digest, safe_path, chunk_conditions

ENV = 'EBOOK_STAGE_CONTEXT'
SCHEMA = 'ebook-stage-journal.v2'
HEAD = 'stage:HEAD'


class StagePersistenceError(RuntimeError):
    pass


def write(path, value):
    from scripts.daily_run import atomic_json
    atomic_json(Path(path), value)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


class StageStore:
    def __init__(self, api):
        self.api = api
        from scripts.evidence_store import EvidenceStore
        self.evidence = EvidenceStore(api)

    def rows(self):
        return self.api.read(TABLE, [['状态键', 'intersects', 'stage:']])

    def head(self):
        rows = self.api.read(TABLE, [['状态键','==',HEAD]])
        if len(rows)>1: raise StagePersistenceError('Duplicate stage HEAD')
        if not rows: return None
        head = rows[0]
        if digest(head['内容'].encode()) != head['校验值']: raise StagePersistenceError('Stage HEAD checksum differs')
        meta = json.loads(head['内容'])
        if meta.get('schema') not in ('ebook-stage-journal.v1', SCHEMA) or meta.get('slot') not in (0, 1):
            raise StagePersistenceError('Unknown stage journal schema')
        return meta

    def read(self, *, fresh=True):
        meta = self.head()
        if meta is None: return None, None
        rows = self.api.read(TABLE, chunk_conditions(f"stage:slot{meta['slot']}:",meta['chunks']))
        index = {r['状态键']: r for r in rows}
        if len(index) != len(rows): raise StagePersistenceError('Duplicate stage journal keys')
        for i in range(meta['chunks']):
            row=index[f"stage:slot{meta['slot']}:{i:05}"]
            if digest(row['内容'].encode())!=row['校验值']: raise StagePersistenceError('Stage chunk checksum differs')
        encoded = ''.join(index[f"stage:slot{meta['slot']}:{i:05}"]['内容'] for i in range(meta['chunks']))
        raw = gzip.decompress(base64.b64decode(encoded, validate=True))
        if digest(raw) != meta['sha256']: raise StagePersistenceError('Stage payload checksum differs')
        value = json.loads(raw)
        if value.get('schema') not in ('ebook-stage-journal.v1', SCHEMA): raise StagePersistenceError('Invalid stage payload')
        if value.get('storage'):
            value = self.evidence.thaw(value, fresh=fresh)
        if self.head()!=meta: raise StagePersistenceError('Stage HEAD changed during read')
        return value, meta

    def publish(self, value):
        prior = self.head()
        logical = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        if prior and prior.get('content_sha256',prior['sha256']) == digest(logical):
            actual, observed = self.read(fresh=False)
            if actual != value: raise StagePersistenceError('Unchanged stage failed readback')
            return observed
        wire = self.evidence.freeze(value) if value.get('schema') == SCHEMA else value
        raw = json.dumps(wire, ensure_ascii=False, separators=(',', ':')).encode()
        slot = 1 - prior['slot'] if prior else 0
        encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
        parts = [encoded[i:i+24000] for i in range(0, len(encoded), 24000)]
        prefix = f'stage:slot{slot}:'
        rows = [{'状态键':prefix+f'{i:05}', '内容':part, '校验值':digest(part.encode())} for i,part in enumerate(parts)]
        # Chunk writes are read back before moving the pointer. Unknown outcomes
        # retain the global lease; no business action is retried here.
        self.api.ensure(TABLE, '状态键', rows, chunk_conditions(prefix,len(parts)), update=True)
        meta = {'schema':value.get('schema'),'slot':slot,'chunks':len(parts),'sha256':digest(raw),'generation':uuid.uuid4().hex}
        if wire.get('storage'): meta['content_sha256'] = digest(logical)
        text = json.dumps(meta, sort_keys=True)
        self.api.ensure(TABLE,'状态键',[{'状态键':HEAD,'内容':text,'校验值':digest(text.encode())}],
                        [['状态键','==',HEAD]], update=True)
        actual, observed = self.read(fresh=False)
        if actual != value or observed != meta: raise StagePersistenceError('Stage journal readback differs')
        return meta


def file_signatures(files):
    """Bind the complete original UTF-8 bytes and all other entry metadata."""
    signatures = {}
    for name, entry in files.items():
        if digest(entry['text'].encode('utf-8')) != entry['sha256']:
            raise StagePersistenceError('Stage input file checksum differs: ' + name)
        signatures[name] = {key: value for key, value in entry.items() if key != 'text'}
    return signatures


def _journal(changed, removed, current, parent, lease, stage, phase):
    return {'schema':SCHEMA, 'parent_generation':parent, 'lease_sha256':digest(lease.encode()),
            'stage':stage, 'phase':phase,
            'checkpoint_meta':{k:v for k,v in current.items() if k!='files'},
            'files':changed, 'removed':sorted(removed)}


def delta(base, current, parent, lease, stage, phase):
    changed = {k:v for k,v in current['files'].items() if base['files'].get(k)!=v}
    return _journal(changed, base['files'].keys()-current['files'].keys(), current, parent, lease, stage, phase)


def indexed_delta(signatures, current, parent, lease, stage, phase):
    observed = file_signatures(current['files'])
    changed = {k:current['files'][k] for k,v in observed.items() if signatures.get(k)!=v}
    return _journal(changed, signatures.keys()-observed.keys(), current, parent, lease, stage, phase)


def index_digest(index):
    return digest(json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def stage_baseline(folder, ctx):
    if 'baseline_index_sha256' not in ctx:
        return file_signatures(load(folder/'baseline.json')['files'])  # Pre-index local context.
    index = load(folder/'baseline-index.json')
    if index_digest(index) != ctx['baseline_index_sha256']:
        raise StagePersistenceError('Local stage baseline index checksum differs')
    if (index['parent_generation'], index['lease_sha256']) != (ctx['parent_generation'], ctx['lease_sha256']):
        raise StagePersistenceError('Local stage baseline belongs to another checkpoint or lease')
    return index['files']


def reconstruct(root, base, parent, journal):
    if journal.get('schema') not in ('ebook-stage-journal.v1', SCHEMA) or journal.get('parent_generation')!=parent:
        raise StagePersistenceError('Stage parent checkpoint differs; do not combine runs')
    files = dict(base['files'])
    for name in journal['removed']:
        safe_path(root,name)
        if name not in files: raise StagePersistenceError('Invalid stage removal')
        files.pop(name)
    for name,value in journal['files'].items():
        safe_path(root,name)
        if digest(value['text'].encode())!=value['sha256']: raise StagePersistenceError('Stage file checksum differs')
        files[name]=value
    return {**journal['checkpoint_meta'],'files':files}


def require_settled(root, checkpoint, generation, journal):
    if journal and journal.get('parent_generation')==generation:
        recovered=reconstruct(root,checkpoint,generation,journal)
        if journal.get('phase')!='completed' or recovered!=checkpoint:
            raise StagePersistenceError('Interrupted remote stage evidence exists; inspect/reconcile it before any new run')


def start(root, base, meta, lease, resume_progress=None, restored_checkpoint=None):
    folder = Path(root)/'runs'/('.stage-'+uuid.uuid4().hex)
    folder.mkdir(parents=True)
    context = {'root':str(Path(root).resolve()),'parent_generation':meta['generation'],
               'lease_sha256':digest(lease.encode()),'day':base['day'],
               'stage':'initializing','before_saved':False,'failed':False,'saves':0}
    index = {'parent_generation':meta['generation'], 'lease_sha256':context['lease_sha256'],
             'files':file_signatures(base['files'])}
    context['baseline_index_sha256'] = index_digest(index)
    write(folder/'baseline-index.json', index)
    # Day belongs to this execution, not yesterday's restored baseline.
    from scripts.daily_run import shanghai_day
    context['day']=shanghai_day()
    from scripts.weread_progress import allowed_files
    context['sampling_allowed'] = allowed_files(restored_checkpoint or base, context['day'])
    if resume_progress is not None:
        context['resume_results'] = resume_progress.get('completed', {})
    write(folder/'context.json',context)
    os.environ[ENV]=str(folder/'context.json')


def context(root):
    path=os.environ.get(ENV)
    if not path:return None,None
    path=Path(path).resolve()
    if not path.is_relative_to((Path(root)/'runs').resolve()): raise StagePersistenceError('Stage context outside current workspace')
    value=load(path)
    if value['root']!=str(Path(root).resolve()) or value['lease_sha256']!=digest(os.environ.get(LEASE_ENV,'').encode()):
        raise StagePersistenceError('Stage context is not owned by this execution')
    if value['failed']:raise StagePersistenceError('Stage persistence failed; reconcile before continuing')
    return path,value


def persist(root, phase):
    from scripts.run_performance import span
    _, ctx = context(root)
    with span('stage_evidence_'+(ctx['stage'] if ctx else 'none')+'_'+phase):
        return _persist(root, phase)


def _persist(root, phase):
    path,ctx=context(root)
    if path is None:return
    started=time.monotonic()
    try:
        from scripts.feishu_io import Base
        from scripts.shared_runtime import Store
        current=capture(root,ctx['day'])
        capture_seconds=time.monotonic()-started
        fingerprint=digest(json.dumps(current,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
        # Only a completed, previously confirmed checkpoint may suppress another
        # read-only completed stage. Never suppress before-write or failure evidence.
        if (phase=='completed' and ctx.get('confirmed_phase')=='completed'
                and ctx.get('confirmed_fingerprint')==fingerprint):
            ctx['unchanged_stages']=ctx.get('unchanged_stages',0)+1
            ctx['last_save']={'phase':phase,'skipped_unchanged':True,'seconds':time.monotonic()-started}
            write(path,ctx)
            return
        value=indexed_delta(stage_baseline(path.parent, ctx),current,ctx['parent_generation'],
                            os.environ[LEASE_ENV],ctx['stage'],phase)
        api=Base(Path(root)/'receipts'/ctx['day']/('stage-'+uuid.uuid4().hex))
        Store(api).discover()  # Existing private system table only.
        store = StageStore(api)
        try:
            store.publish(value)
        except RuntimeError:
            # A successfully published manifest can lose only its ACK/readback.
            # Reconcile the complete journal under the original lease, never
            # rerun the completed business stage or blindly replay its writes.
            from scripts.shared_runtime import assert_shared_writer
            assert_shared_writer(root)
            observed, _ = store.read()
            if observed != value:
                raise
        ctx['saves']+=1
        ctx.update(confirmed_fingerprint=fingerprint,confirmed_phase=phase)
        ctx['last_save']={'phase':phase,'skipped_unchanged':False,
                          'capture_seconds':capture_seconds,'seconds':time.monotonic()-started}
        if phase=='before_write':ctx['before_saved']=True
        write(path,ctx)
    except Exception as exc:
        ctx['failed']=True;write(path,ctx)
        raise StagePersistenceError('Remote stage evidence was not confirmed; business writes blocked') from exc


def before_mutation(root, table):
    if table==TABLE:return
    path,ctx=context(root)
    if path is not None and not ctx['before_saved']:persist(root,'before_write')


def run_stage(root, name, function, *args, **kwargs):
    from scripts.run_performance import span, event
    with span(name):
        started=time.monotonic()
        result=_run_stage(root, name, function, *args, **kwargs)
        event('stage_result', started, business_status=result.get('status') if isinstance(result,dict) else None)
        return result


def _run_stage(root, name, function, *args, **kwargs):
    path,ctx=context(root)
    if path is None:return function(*args,**kwargs)
    from scripts.execution_recovery import PROGRESS, REUSABLE, read
    from scripts.runner_policy import host_fingerprint
    if name in REUSABLE and name in ctx.get('resume_results', {}):
        print(f'[stage] {name}: using reconciled remote completion, no repeated collection/write', flush=True)
        return ctx['resume_results'][name]
    ctx.update(stage=name,before_saved=False);write(path,ctx)
    progress_path = Path(root)/'runs'/ctx['day']/PROGRESS
    progress = read(progress_path, {'completed': {}})
    progress['completed'].pop(name, None)
    progress.update(execution={'host':host_fingerprint(), 'root':str(Path(root).resolve()), 'pid':os.getpid()},
                    current_stage=name)
    write(progress_path, progress)
    started=time.monotonic()
    print(f'[stage] {name}: started',flush=True)
    try:
        result=function(*args,**kwargs)
    except StagePersistenceError:
        raise
    except BaseException:
        persist(root,'failed')
        raise
    path,ctx=context(root)
    if isinstance(result, dict) and result.get('status') in {'completed','partial','ok'}:
        progress['completed'][name] = result
    write(progress_path, progress)
    # Only deterministic, local event preparation may share its save with the
    # next comparison. Any external mutation forces the normal before/after pair.
    deferred = (name in {'project_notifications', 'marketing_expense_notifications'}
                and not ctx['before_saved'])
    if deferred:
        ctx.setdefault('deferred_stages', []).append(name)
        write(path,ctx)
    else:
        persist(root,'completed')
    elapsed=time.monotonic()-started
    path,ctx=context(root)
    ctx.setdefault('stage_timings',[]).append({'stage':name,'seconds':elapsed})
    write(path,ctx)
    print(f'[stage] {name}: completed in {elapsed:.1f}s (including evidence persistence)',flush=True)
    return result
