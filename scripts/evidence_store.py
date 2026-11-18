"""Content-addressed evidence in the existing private state table.

Only manifests rotate. Immutable packs are read back before reference publication.
The optional cache belongs to one verified lease, never to a machine's old state.
Cold reads verify all referenced active and archived packs, including every file.
"""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re

from scripts.run_performance import span, timed

FORMAT = 'ebook-content-manifest.v1'
CACHE_ENV = 'EBOOK_EVIDENCE_CACHE'
PACK_BYTES = 4 * 1024 * 1024
TABLE = '系统运行状态'
MAX_PACK_FILTERS = 8
PACK_PAGE_SIZE = 100


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def pack_identity(ref):
    checksum, count = ref['pack'], ref['chunks']
    if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum) or (
            type(count) is not int or not 1 <= count <= 100000):
        raise ValueError('Invalid evidence pack reference')
    return checksum, count


def pack_groups(counts):
    """Combine small packs in at most one page, with an isolated query for a large pack."""
    group, chunks = {}, 0
    for checksum, count in counts.items():
        if group and (len(group) == MAX_PACK_FILTERS or chunks + count > PACK_PAGE_SIZE):
            yield group
            group, chunks = {}, 0
        group[checksum] = count
        chunks += count
    if group:
        yield group


class EvidenceStore:
    def __init__(self, api):
        self.api = api
        self.packs = {}
        self.versions = {}
        self.folder = None
        name = os.environ.get(CACHE_ENV)
        if name:
            from scripts.shared_runtime import LEASE_ENV
            lease = os.environ.get(LEASE_ENV)
            if not lease:
                raise ValueError('Evidence cache requires an owned lease context')
            self.folder = Path(name)
            identity = self.folder/'identity.json'
            if json.loads(identity.read_text(encoding='utf-8')) != {'lease_sha256': sha(lease.encode())}:
                raise ValueError('Evidence cache lease mismatch')
            index = self.folder/'index.json'
            if index.exists():
                self.versions = json.loads(index.read_text(encoding='utf-8'))

    def save_index(self):
        if self.folder:
            from scripts.daily_run import atomic_json
            atomic_json(self.folder/'index.json', self.versions)

    def remember(self, name, ref):
        from scripts.shared_runtime import safe_path
        safe_path(Path.cwd(), name)
        versions = self.versions.setdefault(name, {})
        versions[ref['sha256']] = dict(ref)

    def _verified_values(self, checksum, raw, *, read_from_disk=False):
        if sha(raw) != checksum:
            raise ValueError('Evidence pack checksum mismatch')
        values = json.loads(raw)
        if not isinstance(values, dict) or any(sha(text.encode()) != key for key, text in values.items()):
            raise ValueError('Evidence file checksum mismatch')
        self.packs[checksum] = raw
        if self.folder and not read_from_disk:
            # A fresh read still verifies every remote chunk and every file.
            # Avoid rewriting an identical local pack on every stage readback.
            cache = self.folder/(checksum+'.json')
            try:
                unchanged = cache.read_bytes() == raw
            except FileNotFoundError:
                unchanged = False
            if not unchanged:
                cache.write_bytes(raw)
        return values

    def _verify_remote_pack(self, checksum, count, rows):
        prefix = 'cas:'+checksum+':'
        index = {row['状态键']: row for row in rows}
        expected = [prefix+f'{i:05}' for i in range(count)]
        if len(index) != len(rows) or index.keys() != set(expected):
            raise ValueError('Missing, duplicate or unexpected immutable evidence chunk')
        parts = []
        for key in expected:
            row = index[key]
            if sha(row['内容'].encode()) != row['校验值']:
                raise ValueError('Evidence chunk checksum mismatch')
            parts.append(row['内容'])
        raw = gzip.decompress(base64.b64decode(''.join(parts), validate=True))
        return self._verified_values(checksum, raw)

    def pack_raw(self, ref, *, fresh):
        checksum, count = pack_identity(ref)
        if not fresh:
            if checksum in self.packs:
                return self._verified_values(checksum, self.packs[checksum])
            cache = self.folder/(checksum+'.json') if self.folder else None
            if cache and cache.is_file():
                return self._verified_values(checksum, cache.read_bytes(), read_from_disk=True)
        with span('evidence_chunk_read', nested=True):
            rows = self.api.read(TABLE, [['状态键', 'intersects', 'cas:'+checksum+':']])
        with span('evidence_chunk_verification', nested=True):
            return self._verify_remote_pack(checksum, count, rows)

    def _read_packs(self, counts):
        verified = {}
        for group in pack_groups(counts):
            conditions = [['状态键', 'intersects', 'cas:'+checksum+':'] for checksum in group]
            with span('evidence_chunk_read', nested=True):
                rows = self.api.read(TABLE, conditions, filter_logic='or' if len(group) > 1 else 'and')
            with span('evidence_chunk_verification', nested=True):
                by_pack = {checksum: [] for checksum in group}
                for row in rows:
                    key = row.get('状态键')
                    parts = key.split(':', 2) if isinstance(key, str) else []
                    if len(parts) != 3 or parts[0] != 'cas' or parts[1] not in by_pack:
                        raise ValueError('Unexpected row in immutable evidence query')
                    by_pack[parts[1]].append(row)
                for checksum, count in group.items():
                    verified[checksum] = self._verify_remote_pack(checksum, count, by_pack[checksum])
        return verified

    @timed('evidence_pack_publish', nested=True)
    def put_pack(self, values):
        with span('evidence_pack_encode', nested=True):
            raw = encoded(values)
            checksum = sha(raw)
            body = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
            parts = [body[i:i+24000] for i in range(0, len(body), 24000)]
            prefix = 'cas:'+checksum+':'
            ref = {'pack': checksum, 'chunks': len(parts)}
            rows = [{'状态键': prefix+f'{i:05}', '内容': part, '校验值': sha(part.encode())}
                    for i, part in enumerate(parts)]
        # ensure returns remote rows, including an independent read after any
        # create. A partial existing pack fails before dispatching any writes.
        with span('evidence_pack_ensure', nested=True):
            observed = self.api.ensure(TABLE, '状态键', rows, [['状态键', 'intersects', prefix]], immutable=True)
        with span('evidence_chunk_verification', nested=True):
            if self._verify_remote_pack(checksum, len(parts), list(observed.values())) != values:
                raise ValueError('Evidence pack differs on independent readback')
        return ref

    @timed('evidence_freeze', nested=True)
    def freeze(self, value):
        """Store only unseen content; retain old versions as archive references."""
        # Child stages share this lease, while the final Store instance predates
        # them. Merge their verified references before writing the final manifest.
        if self.folder and (self.folder/'index.json').exists():
            for name, versions in json.loads((self.folder/'index.json').read_text(encoding='utf-8')).items():
                for ref in versions.values():
                    self.remember(name, ref)
        files = value.get('files', {})
        known = {h: ref for versions in self.versions.values() for h, ref in versions.items()}
        missing = {}
        for name, item in files.items():
            if sha(item['text'].encode()) != item['sha256']:
                raise ValueError('Cannot store evidence with invalid file hash')
            if item['sha256'] not in known:
                missing[item['sha256']] = item['text']
        batch, size = {}, 0
        def flush():
            if batch:
                ref = self.put_pack(batch)
                for h in batch:
                    known[h] = {**ref, 'sha256': h}
        for h, text in sorted(missing.items()):
            if batch and size+len(text.encode()) > PACK_BYTES:
                flush(); batch, size = {}, 0
            batch[h] = text
            size += len(text.encode())
        flush()
        refs, verified = {}, {}
        for name, item in files.items():
            ref = known[item['sha256']]
            # Cache reuse verifies bytes, not existence/size/mtime.
            if ref['pack'] not in verified:
                verified[ref['pack']] = self.pack_raw(ref, fresh=False)
            if verified[ref['pack']].get(item['sha256']) != item['text']:
                raise ValueError('Reused evidence does not match source')
            refs[name] = ref
            self.remember(name, ref)
        archive = {name: [ref for h, ref in sorted(versions.items())
                          if refs.get(name, {}).get('sha256') != h]
                   for name, versions in sorted(self.versions.items())}
        archive = {name: refs for name, refs in archive.items() if refs}
        self.save_index()
        return {'schema': value.get('schema'), 'storage': FORMAT,
                'value': {k:v for k,v in value.items() if k != 'files'},
                'files': refs, 'archive': archive}

    @timed('evidence_thaw', nested=True)
    def thaw(self, manifest, *, fresh=True):
        if manifest.get('storage') != FORMAT:
            raise ValueError('Unknown evidence format')
        if manifest.get('schema') != manifest['value'].get('schema'):
            raise ValueError('Manifest and logical schema differ')
        all_refs = [(name, ref) for name, ref in manifest['files'].items()]
        all_refs += [(name, ref) for name, refs in manifest.get('archive', {}).items() for ref in refs]
        counts = {}
        for _, ref in all_refs:
            checksum, count = pack_identity(ref)
            if counts.setdefault(checksum, count) != count:
                raise ValueError('Conflicting evidence pack chunk counts')
        verified = self._read_packs(counts) if fresh else {
            checksum: self.pack_raw({'pack':checksum, 'chunks':count}, fresh=False)
            for checksum, count in counts.items()}
        for name, ref in all_refs:
            if ref['sha256'] not in verified[ref['pack']]:
                raise ValueError('Referenced evidence file is missing or corrupt')
            self.remember(name, ref)
        result = {**manifest['value'], 'files': {
            name: {'text': verified[ref['pack']][ref['sha256']], 'sha256': ref['sha256']}
            for name, ref in manifest['files'].items()}}
        self.save_index()
        return result


def lease_cache(root, lease):
    """A new cache per lease, inherited by child sync processes."""
    from scripts.daily_run import atomic_json
    directory = Path(root)/'runs'/('.evidence-'+sha(lease.encode()))
    directory.mkdir(parents=True, exist_ok=True)
    identity = {'lease_sha256': sha(lease.encode())}
    path = directory/'identity.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8')) != identity:
        raise ValueError('Evidence cache ownership differs')
    atomic_json(path, identity)
    return str(directory)
