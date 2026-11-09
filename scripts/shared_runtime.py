"""Device-independent checkpoints in the existing private Base.

GitHub holds only a mutex commit with a fixed public README (random token, busy/idle).
No business data, credentials, machine names or local paths go to GitHub.
Fast-forward-only sibling commits provide compare-and-set; locks never expire
behind a possibly live writer. Every Base mutation checks ownership again.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import uuid

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TABLE = '系统运行状态'
BRANCH = 'codex/runtime-coordination'
LEASE_ENV = 'EBOOK_SHARED_LEASE'
FIELDS = [{'name': name, 'type': 'text'} for name in ('状态键', '内容', '校验值')]
CONFIGS = {
    'copyright-aliases.json', 'ebook-only-cleanup.json', 'fanqie-project-identities.json',
    'fanqie-ranking.json', 'fanqie-source-identity-reviews.json',
    'marketing-expense-notifications.json', 'marketing-fee-policy.json',
    'notification-grouping.json', 'project-model.json', 'runtime.json',
    'service-scope.json', 'source-review-overrides.json', 'tencent-runtime.json',
    'weread-identities-reviewed-2026-11-04.json', 'work-copyright-policy.json',
    'work-metadata-policy.json',
}
ROOT_STATES = {'fanqie-milestones.json', 'manual-project-events-state.json',
    'manual-project-registry.json', 'marketing-expense-notification-state.json',
    'marketing-expense-registry.json', 'publisher-recent-entry-registry.json'}


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def configured(root):
    path = Path(root) / 'config/shared-runtime.json'
    if path.exists():
        return load(path).get('enabled') is True
    # An updated operational checkout must not silently remain a second legacy
    # writer merely because its machine-local config predates this feature.
    return (Path(root) / 'config/base-manifest.json').exists()


def configuration(root):
    path = Path(root) / 'config/shared-runtime.json'
    if path.exists(): return load(path)
    remote = subprocess.check_output(['git', '-C', str(root), 'remote', 'get-url', 'origin'],
                                     text=True, encoding='utf-8', timeout=10).strip()
    match = re.fullmatch(r'(?:https://github.com/|git@github.com:)([^/]+/[^/]+?)(?:\.git)?', remote)
    if not match: raise ValueError('A verified GitHub origin is required for coordination')
    return {'enabled': True, 'coordination_repository': match.group(1)}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def transport_env(root):
    env = dict(os.environ)
    path = Path(root)/'config/shared-runtime.json'
    hosts = load(path).get('direct_hosts', []) if path.exists() else []
    if any(host not in {'api.github.com', 'open.feishu.cn', '.feishu.cn'} for host in hosts):
        raise ValueError('Unexpected shared-runtime direct host')
    if hosts:
        env['NO_PROXY'] = ','.join(filter(None, [env.get('NO_PROXY', ''), *hosts]))
    return env


def gh(repo, endpoint, payload=None):
    args = ['gh', 'api', 'repos/' + repo + '/git/' + endpoint]
    if payload is not None:
        args += ['--method', 'POST' if not endpoint.startswith('refs/heads/') else 'PATCH', '--input', '-']
    for attempt in range(3 if payload is None else 1):
        reply = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                               capture_output=True, text=True, encoding='utf-8', timeout=90,
                               env=transport_env(Path(__file__).resolve().parents[1]))
        if reply.returncode == 0:
            return json.loads(reply.stdout)
    raise RuntimeError('Coordination API failed (writes are never blindly retried): ' + reply.stderr[:400])


class BusyError(RuntimeError):
    """A verified competing execution owns the mutex; safe to skip this attempt."""


class Mutex:
    def __init__(self, repo, call=gh):
        self.repo, self.call, self.token = repo, call, None

    def head(self):
        ref = self.call(self.repo, 'ref/heads/' + BRANCH)
        sha = ref['object']['sha']
        commit = self.call(self.repo, 'commits/' + sha)
        state = json.loads(commit['message'])
        if state.get('schema') != 'ebook-mutex.v1':
            raise RuntimeError('Unexpected coordination branch; do not overwrite')
        return sha, commit, state

    def initialize(self):
        # Explicit bootstrap only. Existing branches are never reset or forced.
        tree = self.call(self.repo, 'trees', {'tree': [{'path': 'README.md', 'mode': '100644',
            'type': 'blob', 'content': 'Execution mutex only. Business checkpoints stay in the private Feishu Base.\n'}]})['sha']
        commit = self.call(self.repo, 'commits', {'message': json.dumps({
            'schema': 'ebook-mutex.v1', 'phase': 'idle'}), 'tree': tree, 'parents': []})
        return self.call(self.repo, 'refs', {'ref': 'refs/heads/' + BRANCH, 'sha': commit['sha']})

    def transition(self, sha, commit, state):
        child = self.call(self.repo, 'commits', {'message': json.dumps(state),
            'tree': commit['tree']['sha'], 'parents': [sha]})
        # Competing children have the same parent: only one can fast-forward.
        try:
            self.call(self.repo, 'refs/heads/' + BRANCH, {'sha': child['sha'], 'force': False})
        except RuntimeError:
            # A lost ACK is resolved with a read, never a second PATCH.
            observed = self.call(self.repo, 'ref/heads/' + BRANCH)
            if observed['object']['sha'] != child['sha']:
                raise
        return child['sha']

    def acquire(self):
        sha, commit, state = self.head()
        if state['phase'] != 'idle':
            stamp = commit.get('committer', {}).get('date')
            if stamp and datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00')) > timedelta(hours=2):
                raise RuntimeError('Shared execution lock is older than two hours; inspect receipts, never auto-steal')
            raise BusyError('Another run owns the shared lock; do not start a second writer')
        token = uuid.uuid4().hex
        try:
            self.transition(sha, commit, {'schema': 'ebook-mutex.v1', 'phase': 'busy', 'token': token})
        except RuntimeError:
            _, _, current = self.head()
            if current.get('phase') == 'busy' and current.get('token') != token:
                raise BusyError('Another execution won the shared lock') from None
            raise
        self.token = token
        self.assert_owned()
        return token

    def assert_owned(self):
        _, _, state = self.head()
        if state.get('phase') != 'busy' or not self.token or state.get('token') != self.token:
            raise PermissionError('Shared execution lock is not owned by this process')

    def release(self):
        sha, commit, state = self.head()
        if state.get('phase') != 'busy' or state.get('token') != self.token:
            raise PermissionError('Cannot release another execution lock')
        self.transition(sha, commit, {'schema': 'ebook-mutex.v1', 'phase': 'idle'})
        self.token = None


def assert_shared_writer(root):
    config = configuration(root)
    mutex = Mutex(config['coordination_repository'])
    mutex.token = os.environ.get(LEASE_ENV)
    mutex.assert_owned()


class Store:
    def __init__(self, api):
        self.api = api
        # Text-heavy checkpoint rows need a byte-bounded batch, well below the
        # normal 200-record API maximum (20 * 24 KB per request).
        self.api.write_batch_size = 20
        self.api.read_page_size = 100

    def discover(self):
        rows = self.api.call('+table-list')['data']['tables']
        tables = [r for r in rows if r['name'] == TABLE]
        if len(tables) != 1:
            raise RuntimeError('Shared state table missing or ambiguous; bootstrap required')
        tid = tables[0].get('id') or tables[0].get('table_id')
        self.api.manifest['tables'][TABLE] = {'id': tid, 'field_readback': {'fields': FIELDS}}
        fields = self.api.call('+field-list', TABLE)['data']['fields']
        if any(not any(f['name'] == e['name'] and f['type'] == e['type'] for f in fields) for e in FIELDS):
            raise RuntimeError('Shared state schema mismatch')
        self.api.manifest['tables'][TABLE]['field_readback']['fields'] = fields
        return self

    def read(self):
        rows = self.api.read(TABLE)
        index = {r['状态键']: r for r in rows}
        if len(index) != len(rows):
            raise RuntimeError('Duplicate shared-state keys')
        head = index.get('HEAD')
        if not head:
            raise RuntimeError('No committed shared checkpoint; never use stale local defaults')
        meta = json.loads(head['内容'])
        if digest(head['内容'].encode()) != head['校验值']:
            raise RuntimeError('Shared HEAD checksum mismatch')
        prefix = 'slot'+str(meta['slot']) if 'slot' in meta else meta['generation']
        encoded = ''.join(index[f"{prefix}:{i:05}"]['内容'] for i in range(meta['chunks']))
        raw = gzip.decompress(base64.b64decode(encoded, validate=True))
        if digest(raw) != meta['sha256']:
            raise RuntimeError('Incomplete or corrupt shared checkpoint')
        data = json.loads(raw)
        if data.get('schema') != 'ebook-shared-checkpoint.v1':
            raise RuntimeError('Unknown shared checkpoint schema')
        return data, meta

    def publish(self, checkpoint):
        raw = json.dumps(checkpoint, ensure_ascii=False, separators=(',', ':')).encode()
        heads = [r for r in self.api.read(TABLE) if r['状态键'] == 'HEAD']
        if len(heads) > 1: raise RuntimeError('Duplicate shared HEAD')
        previous = json.loads(heads[0]['内容']) if heads else {}
        if previous.get('sha256') == digest(raw):
            verified, meta = self.read()
            if verified != checkpoint: raise RuntimeError('Unchanged checkpoint failed readback')
            return meta
        encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
        generation = uuid.uuid4().hex
        slot = 1 - previous['slot'] if 'slot' in previous else 0
        parts = [encoded[i:i+24000] for i in range(0, len(encoded), 24000)]
        rows = [{'状态键': f'slot{slot}:{i:05}', '内容': text,
                 '校验值': digest(text.encode())} for i, text in enumerate(parts)]
        self.api.ensure(TABLE, '状态键', rows, update=True)
        meta = {'generation': generation, 'slot': slot, 'chunks': len(parts), 'sha256': digest(raw),
                'saved_at': datetime.now(timezone.utc).isoformat()}
        text = json.dumps(meta, sort_keys=True)
        # Commit the pointer only after every immutable chunk has passed readback.
        self.api.ensure(TABLE, '状态键', [{'状态键': 'HEAD', '内容': text,
                        '校验值': digest(text.encode())}], update=True)
        verified, result = self.read()
        if verified != checkpoint or result != meta:
            raise RuntimeError('Checkpoint readback mismatch; keep lock for reconciliation')
        return meta


def safe_path(root, name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or p.parts[0] not in {'config','runs','research','receipts'}:
        raise ValueError('Unsafe checkpoint path')
    if p.parts[0] == 'config' and (len(p.parts) != 2 or p.name not in CONFIGS):
        raise ValueError('Machine configuration is not portable')
    if any(s in name.lower() for s in ('session', 'credential', 'cookie', 'password', 'auth-cache')):
        raise ValueError('Authentication data cannot be checkpointed')
    path = (Path(root) / name).resolve()
    if not path.is_relative_to(Path(root).resolve()) or path.suffix not in {'.json', '.jsonp'}:
        raise ValueError('Checkpoint path escapes workspace or is not JSON')
    return path


def capture(root, day):
    root = Path(root)
    paths = {root/'config'/name for name in CONFIGS} | {root/'runs'/name for name in ROOT_STATES}
    # Business policy references are included; login and machine settings stay local.
    for name in ('tencent-runtime.json', 'service-scope.json', 'source-review-overrides.json'):
        path = root/'config'/name
        if path.exists():
            def references(value):
                if isinstance(value, dict):
                    for v in value.values(): yield from references(v)
                elif isinstance(value, list):
                    for v in value: yield from references(v)
                elif isinstance(value, str) and value.endswith('.json') and value.startswith(('config/', 'research/')):
                    yield safe_path(root, value)
            paths.update(references(load(path)))
    provider = root/'config/publisher-newbooks.json'
    if provider.exists():
        paths.add(safe_path(root, load(provider)['providers_file']))
    review = root/'config/source-review-overrides.json'
    if review.exists():
        from scripts.local_settings import project_path
        for entry in load(review).get('overrides', []):
            evidence = project_path(entry['evidence_path'], root)
            safe_path(root, evidence.relative_to(root).as_posix())
            if digest(evidence.read_bytes()) != entry['evidence_sha256']:
                raise ValueError('Source service confirmation evidence changed')
            paths.add(evidence)
    for offset in (0, 1):
        date = (datetime.fromisoformat(day) - timedelta(days=offset)).date().isoformat()
        directory = root/'runs'/date
        paths.update(directory.glob('*.json'))
        for folder in ('tencent-source', 'fanqie-rising'):
            paths.update((directory/folder).glob('*.json'))
            paths.update((directory/folder).glob('*.jsonp'))
        paths.add(directory/'fanqie/snapshot.json')
        for folder in directory.glob('publisher-recent-*'):
            paths.update(folder.glob('*.json'))
    files = {}
    for path in sorted(paths):
        if not path.is_file(): continue
        name = path.relative_to(root).as_posix()
        safe_path(root, name)
        # Preserve raw response bytes exactly: source receipts verify their SHA.
        raw = path.read_bytes()
        files[name] = {'text': raw.decode('utf-8'), 'sha256': digest(raw)}
    origin = root/'config/workspace-origin.json'
    origins = load(origin).get('roots', []) if origin.exists() else []
    from scripts.initialize_manual_projects import unknown_guard_paths
    unresolved = unknown_guard_paths(root)
    for name in ('daily-state.json', 'fanqie-state.json', 'tencent-state.json'):
        for path in (root/'runs').glob('*/'+name):
            if load(path).get('phase') in {'sync_in_progress','apply_in_progress','events_in_progress','requires_review'}:
                unresolved.append(str(path))
    return {'schema': 'ebook-shared-checkpoint.v1', 'source_root': str(root.resolve()), 'source_roots': origins,
            'day': day, 'files': files, 'unresolved_guards': unresolved}


def restore(root, checkpoint):
    """Restore only committed portable files; back up local values, never credentials."""
    root = Path(root)
    from scripts.daily_run import atomic_json
    backup = root/'backups'/('shared-cache-'+uuid.uuid4().hex)
    validated = [(safe_path(root, name), name, value) for name, value in checkpoint['files'].items()]
    for path, name, value in validated:
        raw = value['text'].encode('utf-8')
        if digest(raw) != value['sha256']:
            raise ValueError('Checkpoint file checksum mismatch: ' + name)
    for path, name, value in validated:
        raw = value['text'].encode('utf-8')
        if path.exists() and path.read_bytes() == raw: continue
        if path.exists():
            saved = backup/name
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    origin = root/'config/workspace-origin.json'
    roots = load(origin).get('roots', []) if origin.exists() else []
    atomic_json(origin, {'roots': list(dict.fromkeys([checkpoint['source_root'], *checkpoint.get('source_roots', []), *roots]))})
    return len(validated)


@contextmanager
def session(root, dry_run=False):
    if not configured(root):
        yield None
        return
    from scripts.feishu_io import Base
    from scripts.daily_run import shanghai_day
    from scripts.initialize_manual_projects import unknown_guard_paths
    root = Path(root)
    if unknown_guard_paths(root):
        raise RuntimeError('Unresolved local mutation; reconcile before replacing caches')
    config = configuration(root)
    mutex = Mutex(config['coordination_repository'])
    old_env = os.environ.get(LEASE_ENV)
    acquired = False
    entered = False
    try:
        if not dry_run:
            try:
                os.environ[LEASE_ENV] = mutex.acquire()
            except BusyError:
                yield {'skipped': True, 'reason': 'another_execution_owns_shared_lock', 'business_writes': 0}
                return
            acquired = True
        api = Base(root/'receipts'/shanghai_day()/('shared-state-'+uuid.uuid4().hex))
        store = Store(api).discover()
        checkpoint, meta = store.read()
        if checkpoint.get('unresolved_guards'):
            raise RuntimeError('Shared checkpoint has unresolved mutations; reconcile from server evidence')
        restore(root, checkpoint)
        entered = True
        result = {'generation': meta['generation'], 'restored_files': len(checkpoint['files'])}
        yield result
        if not dry_run:
            saved = store.publish(capture(root, shanghai_day()))
            mutex.release()
            acquired = False
            result.update(checkpoint_committed=True, committed_generation=saved['generation'], lock_released=True)
    finally:
        # Exceptions intentionally retain the remote lock. Never release while
        # a mutation/checkpoint may be unknown or silently revert to local state.
        if acquired and not entered:
            mutex.release()  # Reads/cache preparation only; no business mutation was started.
        if old_env is None: os.environ.pop(LEASE_ENV, None)
        else: os.environ[LEASE_ENV] = old_env
