"""Merge same-day read-only supplements without replacing successful observations."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from scripts.local_settings import project_path


def merge(initial, supplement, expected_ids, day):
    for source in (initial, supplement):
        if (source.get('schema_version') != 'fanqie.public.v1'
                or source.get('collection_date') != day
                or not str(source.get('finished_at', '')).startswith(day)):
            raise ValueError('Supplement must be a finished observation from the same day')
    original = {str(b['book_id']): b for b in initial['books']}
    added = {str(b['book_id']): b for b in supplement['books']}
    if len(original) != len(initial['books']) or len(added) != len(supplement['books']):
        raise ValueError('Duplicate observation IDs')
    if supplement.get('errors') or not added or set(original) & set(added):
        raise ValueError('Supplement failed or would overwrite a successful observation')
    missing = set(expected_ids) - set(original)
    if set(added) != missing or set(original) - set(expected_ids):
        raise ValueError('Supplement does not exactly cover missing registered IDs')
    failed = {str(e['book_id']) for e in initial.get('errors', [])}
    if failed != missing:
        raise ValueError('Missing IDs do not match the recorded collection errors')
    for book in added.values():
        if book.get('identity_status') != 'book_id_and_title_verified':
            raise ValueError('Unverified supplemental identity')
    return {**initial, 'books': [*initial['books'], *supplement['books']], 'errors': [],
            'finished_at': max(initial['finished_at'], supplement['finished_at']),
            'status': 'partial' if any(b.get('missing_fields') for b in [*original.values(), *added.values()]) else 'success',
            'sources': [*initial.get('sources', []), *supplement.get('sources', [])],
            'resolved_collection_errors': initial.get('errors', []),
            'supplement_policy': 'Keep successful observations and their real sampling times; replace no past data.'}


def prepare(root, directory, supplement_path, expected_ids, day):
    root = Path(root).resolve()
    extra_path = project_path(supplement_path, root).resolve()
    extra_path.relative_to(root)
    raw = extra_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    target = Path(directory)/'fanqie-effective.json'
    initial = json.loads((Path(directory)/'fanqie/snapshot.json').read_text(encoding='utf-8-sig'))
    extra = json.loads(raw)
    result = merge(initial, extra, expected_ids, day)
    if not extra.get('sources'):
        raise ValueError('Supplement has no archived source responses')
    for source in extra['sources']:
        parsed = urlsplit(source['url'])
        if parsed.scheme != 'https' or parsed.hostname not in {'api.fqnovel.com', 'fanqienovel.com'}:
            raise ValueError('Supplement source is not an approved official HTTPS endpoint')
        path = project_path(source['archive_path'], root).resolve()
        path.relative_to(extra_path.parent)
        if (source.get('http_status') != 200
                or not str(source.get('observed_at', '')).startswith(day)
                or hashlib.sha256(path.read_bytes()).hexdigest() != source['response_sha256']):
            raise ValueError('Supplement source receipt failed verification')
    from scripts.collect_fanqie import normalize
    for book in extra['books']:
        detail, identity = book['detail_source'], book['identity_source']
        if detail not in extra['sources'] or identity not in extra['sources']:
            raise ValueError('Supplement book references an unverified response')
        rebuilt = normalize(str(book['book_id']),
                            json.loads(project_path(detail['archive_path'], root).read_bytes()),
                            json.loads(project_path(identity['archive_path'], root).read_bytes()),
                            detail, identity)
        if rebuilt != book:
            raise ValueError('Supplement values differ from archived official responses')
    result.update(supplement_sha256=digest,
                  reviewed_supplements=[extra_path.relative_to(root).as_posix()])
    if target.exists():
        existing = json.loads(target.read_text(encoding='utf-8-sig'))
        if existing == result:
            return target
        raise ValueError('Existing effective observation differs; review before replacement')
    from scripts.daily_run import atomic_json
    atomic_json(target, result)
    return target
