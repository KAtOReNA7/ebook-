"""Per-book observations reusable only from the restored authoritative checkpoint.

Uncommitted local files never grant reuse. Every result and raw file is checked
against the restored manifest, and observation times survive a resumed attempt.
"""
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import threading
from urllib.parse import urlparse


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def allowed_files(checkpoint, day=None):
    return {name: value['sha256'] for name, value in checkpoint.get('files', {}).items()
            if '/raw-weread-' in name and (day is None or name.startswith('runs/'+day+'/'))}


def local_day(stamp):
    value = datetime.fromisoformat(stamp)
    if value.tzinfo is None:
        raise ValueError('Observation has no timezone')
    return value.astimezone(timezone(timedelta(hours=8))).date().isoformat()


class Journal:
    def __init__(self, root, destination, day, authorized=None):
        self.root, self.destination, self.day = Path(root).resolve(), Path(destination), day
        if authorized is None:
            from scripts.stage_checkpoint import context
            _, ctx = context(self.root)
            authorized = (ctx or {}).get('sampling_allowed', {})
        self.authorized = authorized
        self.entries = {}
        self.lock = threading.Lock()
        self.stats = {'detail_reused': 0, 'reading_reused': 0, 'rejected': 0,
                      'detail_saved': 0, 'reading_saved': 0}
        for name in sorted(authorized):
            if not Path(name).name.startswith('sample-'):
                continue
            try:
                row = json.loads(self.raw(name))
                if row['schema'] != 'weread-observation.v1' or row['day'] != day:
                    continue
                self.entries.setdefault((row['kind'], row['book_id']), []).append(row)
            except (OSError, ValueError, KeyError, TypeError):
                self.stats['rejected'] += 1

    def raw(self, name):
        from scripts.shared_runtime import safe_path
        path = safe_path(self.root, name)
        raw = path.read_bytes()
        if self.authorized.get(name) != sha(raw):
            raise ValueError('Observation is not the restored authoritative version')
        return raw

    def take(self, kind, bid, url=None):
        candidates = []
        for row in self.entries.get((kind, str(bid)), []):
            try:
                result = copy.deepcopy(row['result'])
                source = result['detail_source'] if kind == 'detail' else result['source']
                if local_day(source['observed_at']) != self.day:
                    continue
                parsed = urlparse(source.get('url') or source.get('source_url', ''))
                if parsed.scheme != 'https' or parsed.netloc != 'weread.qq.com':
                    raise ValueError('Unofficial observation source')
                name = row['evidence_path']
                raw = self.raw(name)
                if sha(raw) != row['evidence_sha256']:
                    raise ValueError('Raw observation changed')
                data = json.loads(raw)
                if kind == 'detail':
                    from scripts.collect_weread import parsed_detail
                    if result['book_id'] != str(bid) or result['detail_url'] != url or source['url'] != url:
                        continue
                    if sha(raw) != source['archive_sha256']:
                        raise ValueError('Metadata hash differs')
                    if parsed_detail(data['bookInfo'], data.get('readingStat', {}), url, source) != result:
                        raise ValueError('Metadata result differs from raw fields')
                else:
                    if data.get('errcode') or data.get('errCode') or data.get('error'):
                        raise ValueError('Search response reports an error')
                    matches = [b for b in data['books'] if str(b.get('bookInfo', {}).get('bookId')) == str(bid)]
                    if (result.get('book_id') != str(bid) or len(matches) != 1
                            or source.get('source_kind') != 'official_search_exact_book_id'
                            or parsed.path != '/web/search/global'
                            or type(result['value']) is not int or result['value'] < 0
                            or matches[0].get('readingCount') != result['value']
                            or source.get('matched_book_id') != str(bid)
                            or source.get('value') != result['value']
                            or source.get('source_field') != 'books[].readingCount'
                            or source.get('source_label') != '人今日阅读'
                            or sha(raw) != source.get('response_sha256')):
                        raise ValueError('Reading result differs from exact official ID')
                # Only the evidence path changes across hosts, never its timestamp.
                old_path = source['archive_path']
                new_path = str((self.root / name).resolve())
                def relocate(value):
                    if isinstance(value, dict): return {k: relocate(v) for k, v in value.items()}
                    if isinstance(value, list): return [relocate(v) for v in value]
                    return new_path if value == old_path else value
                candidates.append((source['observed_at'], relocate(result)))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                with self.lock: self.stats['rejected'] += 1
        if not candidates:
            return None
        with self.lock: self.stats[kind+'_reused'] += 1
        return max(candidates, key=lambda x: datetime.fromisoformat(x[0]))[1]

    def save(self, kind, bid, result):
        from scripts.daily_run import atomic_json
        if (kind == 'detail' and result.get('status') != 'ok') or (kind == 'reading' and not result.get('source')):
            return
        source = result['detail_source'] if kind == 'detail' else result['source']
        if local_day(source['observed_at']) != self.day:
            return
        path = Path(source['archive_path']).resolve()
        # Standalone read-only exports outside the checkout are valid outputs,
        # but cannot become shared continuation evidence.
        if not path.is_relative_to(self.root): return
        name = path.relative_to(self.root).as_posix()
        raw = path.read_bytes()
        expected = source.get('archive_sha256') if kind == 'detail' else source.get('response_sha256')
        if sha(raw) != expected:
            raise ValueError('Cannot record corrupt sampling evidence')
        row = {'schema': 'weread-observation.v1', 'day': self.day, 'kind': kind,
               'book_id': str(bid), 'evidence_path': name, 'evidence_sha256': sha(raw), 'result': result}
        key = sha((kind+':'+str(bid)).encode())
        atomic_json(self.destination/('sample-'+key+'.json'), row)
        with self.lock: self.stats[kind+'_saved'] += 1
