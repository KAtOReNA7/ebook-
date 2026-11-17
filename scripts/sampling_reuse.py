"""Reuse verified official-ID metrics only inside the collecting execution."""
import copy
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse


def eligible_snapshot(path, day):
    from scripts.shared_runtime import LEASE_ENV
    lease = os.environ.get(LEASE_ENV)
    if not lease or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if (data.get('execution_lease_sha256') != hashlib.sha256(lease.encode()).hexdigest()
            or data.get('collection_date') != day):
        return {}
    books = data.get('books', [])
    if len({b['book_id'] for b in books}) != len(books):
        raise ValueError('Duplicate official IDs in reuse source')
    return {b['book_id']: b for b in books}


def verified_copy(book, target, destination, day):
    """Absent/failed evidence falls back to a fresh request; never copy a guess."""
    from scripts.collect_weread import rating
    from scripts.coverage import metric_request_failed
    bid = str(target['book_id'])
    if not book or book.get('status') != 'ok' or metric_request_failed(book) or book.get('book_id') != bid:
        return None
    detail, reading = book.get('detail_source', {}), book.get('today_reading_source') or {}
    for source in (detail, reading):
        url = urlparse(source.get('url') or source.get('source_url') or '')
        if url.scheme != 'https' or url.netloc != 'weread.qq.com' or source.get('observed_at', '')[:10] != day:
            return None
    try:
        metadata_bytes = Path(detail['archive_path']).read_bytes()
        reading_bytes = Path(reading['archive_path']).read_bytes()
        if (hashlib.sha256(metadata_bytes).hexdigest() != detail['archive_sha256'] or
                hashlib.sha256(reading_bytes).hexdigest() != reading['response_sha256']):
            return None
        info = json.loads(metadata_bytes)['bookInfo']
        rows = json.loads(reading_bytes)['books']
        matches = [r for r in rows if str(r.get('bookInfo', {}).get('bookId')) == bid]
        if (str(info['bookId']) != bid or len(matches) != 1 or
                matches[0].get('readingCount') != book.get('today_reading_count') or
                reading.get('value') != book.get('today_reading_count') or
                info.get('newRatingCount') != book.get('reviewer_count') or
                rating(info.get('newRating')) != book.get('recommendation_percent')):
            return None
        for source_field, field in (('title','title'),('author','author'),('totalWords','word_count'),('category','category')):
            if info.get(source_field) != book.get(field):
                return None
        if (info.get('copyrightInfo') or {}).get('name') != book.get('copyright_provider'):
            return None
    except (KeyError, ValueError, OSError, TypeError):
        return None
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    result = copy.deepcopy(book)
    # Retain original observation times and provenance; only relocate raw bytes.
    relocations = {}
    for source, raw in ((detail, metadata_bytes), (reading, reading_bytes)):
        dest = destination/(hashlib.sha256(raw).hexdigest()+'.json')
        dest.write_bytes(raw)
        relocations[source['archive_path']] = str(dest.resolve())
    def relocate(value):
        if isinstance(value, dict):
            return {k: relocate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [relocate(v) for v in value]
        return relocations.get(value, value) if isinstance(value, str) else value
    result = relocate(result)
    result['memberships'] = []
    result['sampling_reuse'] = {'kind': 'same_execution_official_id', 'book_id': bid,
                                'original_detail_observed_at': detail['observed_at'],
                                'original_reading_observed_at': reading['observed_at']}
    return result
