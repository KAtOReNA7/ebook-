"""Incremental private stage journal. Never grants a lease or retries business writes."""
import base64
import gzip
import json
import os
from pathlib import Path
import uuid

from scripts.shared_runtime import TABLE, LEASE_ENV, capture, digest, safe_path

ENV = 'EBOOK_STAGE_CONTEXT'
SCHEMA = 'ebook-stage-journal.v1'
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

    def rows(self):
        return self.api.read(TABLE, [['状态键', 'intersects', 'stage:']])

    def read(self):
        rows = self.rows()
        index = {r['状态键']: r for r in rows}
        if len(index) != len(rows): raise StagePersistenceError('Duplicate stage journal keys')
        if HEAD not in index: return None, None
        head = index[HEAD]
        if digest(head['内容'].encode()) != head['校验值']: raise StagePersistenceError('Stage HEAD checksum differs')
        meta = json.loads(head['内容'])
        if meta.get('schema') != SCHEMA or meta.get('slot') not in (0, 1):
            raise StagePersistenceError('Unknown stage journal schema')
        encoded = ''.join(index[f"stage:slot{meta['slot']}:{i:05}"]['内容'] for i in range(meta['chunks']))
        raw = gzip.decompress(base64.b64decode(encoded, validate=True))
        if digest(raw) != meta['sha256']: raise StagePersistenceError('Stage payload checksum differs')
        value = json.loads(raw)
        if value.get('schema') != SCHEMA: raise StagePersistenceError('Invalid stage payload')
        return value, meta

    def publish(self, value):
        _, prior = self.read()
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        if prior and prior['sha256'] == digest(raw): return prior
        slot = 1 - prior['slot'] if prior else 0
        encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
        parts = [encoded[i:i+24000] for i in range(0, len(encoded), 24000)]
        prefix = f'stage:slot{slot}:'
        rows = [{'状态键':prefix+f'{i:05}', '内容':part, '校验值':digest(part.encode())} for i,part in enumerate(parts)]
        # Chunk writes are read back before moving the pointer. Unknown outcomes
        # retain the global lease; no business action is retried here.
        self.api.ensure(TABLE, '状态键', rows, [['状态键','intersects',prefix]], update=True)
        meta = {'schema':SCHEMA,'slot':slot,'chunks':len(parts),'sha256':digest(raw),'generation':uuid.uuid4().hex}
        text = json.dumps(meta, sort_keys=True)
        self.api.ensure(TABLE,'状态键',[{'状态键':HEAD,'内容':text,'校验值':digest(text.encode())}],
                        [['状态键','==',HEAD]], update=True)
        actual, observed = self.read()
        if actual != value or observed != meta: raise StagePersistenceError('Stage journal readback differs')
        return meta


def delta(base, current, parent, lease, stage, phase):
    return {'schema':SCHEMA, 'parent_generation':parent, 'lease_sha256':digest(lease.encode()),
            'stage':stage, 'phase':phase,
            'checkpoint_meta':{k:v for k,v in current.items() if k!='files'},
            'files':{k:v for k,v in current['files'].items() if base['files'].get(k)!=v},
            'removed':sorted(set(base['files'])-set(current['files']))}


def reconstruct(root, base, parent, journal):
    if journal.get('schema')!=SCHEMA or journal.get('parent_generation')!=parent:
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


def start(root, base, meta, lease):
    folder = Path(root)/'runs'/('.stage-'+uuid.uuid4().hex)
    folder.mkdir(parents=True)
    write(folder/'baseline.json',base)
    context = {'root':str(Path(root).resolve()),'parent_generation':meta['generation'],
               'lease_sha256':digest(lease.encode()),'day':base['day'],
               'stage':'initializing','before_saved':False,'failed':False,'saves':0}
    # Day belongs to this execution, not yesterday's restored baseline.
    from scripts.daily_run import shanghai_day
    context['day']=shanghai_day()
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
    path,ctx=context(root)
    if path is None:return
    try:
        from scripts.feishu_io import Base
        from scripts.shared_runtime import Store
        current=capture(root,ctx['day'])
        value=delta(load(path.with_name('baseline.json')),current,ctx['parent_generation'],
                    os.environ[LEASE_ENV],ctx['stage'],phase)
        api=Base(Path(root)/'receipts'/ctx['day']/('stage-'+uuid.uuid4().hex))
        Store(api).discover()  # Existing private system table only.
        StageStore(api).publish(value)
        ctx['saves']+=1
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
    path,ctx=context(root)
    if path is None:return function(*args,**kwargs)
    ctx.update(stage=name,before_saved=False);write(path,ctx)
    try:
        result=function(*args,**kwargs)
    except StagePersistenceError:
        raise
    except BaseException:
        persist(root,'failed')
        raise
    persist(root,'completed')
    return result
