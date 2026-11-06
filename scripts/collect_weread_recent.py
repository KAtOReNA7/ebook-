"""Read provider recent-release lists through an explicitly authorized local session.

Read-only source collector: never writes Base data, sends messages, or updates a baseline.
Session is encrypted (Windows DPAPI or opt-in AES-GCM), from a separate explicit login.
"""
import argparse
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .collect_weread import now, listing_time, rating, write_json
    from .session_store import load_session
except ImportError:
    from collect_weread import now, listing_time, rating, write_json
    from session_store import load_session

ENDPOINT = 'https://i.weread.qq.com/shelf/opus'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_recent(provider, session):
    query = {'userVid': str(provider['user_vid']), 'count': 0,
             'listType': 1, 'listMode': 0, 'album': 0}
    url = ENDPOINT + '?' + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'WeRead/2.1.2 WRBrand/Onyx wr_eink Dalvik/2.1.0 (Linux; U; Android 11; BOOX Build/onyx)',
        'baseapi': '30', 'appver': '2.1.2.10245900', 'basever': '2.1.2.10245900',
        'osver': '11', 'channelId': '900',
        'vid': str(session['vid']), 'accessToken': session['accessToken'],
    })
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=25) as response:
            status, raw = response.status, response.read(20 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read(1024 * 1024)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError('Recent-list response exceeds the configured size limit')
    return json.loads(raw), {'url': url, 'http_status': status, 'observed_at': now(),
                             'response_sha256': hashlib.sha256(raw).hexdigest()}


def validate_recent(body, provider, evidence):
    result = {'provider': provider['name'], 'status': 'incomplete', 'source': evidence,
              'total_count': None, 'returned_rows': None, 'unique_ids': None,
              'books': [], 'issues': []}
    if evidence['http_status'] != 200 or not isinstance(body, dict) or any(
            body.get(k) not in (None, 0) for k in ('errcode', 'errCode')):
        result['status'] = 'source_error'
        result['issues'] = ['Authentication or source error; this is not an empty list']
        return result
    rows, total = body.get('data'), body.get('totalCount')
    if not isinstance(rows, list) or type(total) is not int or total < 0:
        result['issues'] = ['Missing or invalid data/totalCount']
        return result
    result.update(total_count=total, returned_rows=len(rows))
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get('bookInfo'), dict) or not isinstance(row.get('meta'), dict):
            result['issues'].append(f'Row {index}: invalid bookInfo/meta')
            continue
        book, meta = row['bookInfo'], row['meta']
        bid = book.get('bookId')
        if not isinstance(bid, str) or not bid.isdigit() or bid in seen:
            result['issues'].append(f'Row {index}: missing or duplicate official ID')
            continue
        seen.add(bid)
        if book.get('cpid') != provider['copyright_id']:
            result['issues'].append(f'Row {index}: copyright identity mismatch')
            continue
        if (book.get('format') not in ('epub', 'txt') or type(book.get('type')) is not int
                or book['type'] != 0 or type(meta.get('type')) is not int or meta['type'] != 0
                or row.get('albumInfo') or row.get('albumInfoExtra')):
            result['issues'].append(f'Row {index}: audio or unconfirmed media excluded')
            continue
        timestamp = listing_time(meta.get('updateTime'))
        if timestamp is None:
            result['issues'].append(f'Row {index}: missing recent-list timestamp')
            continue
        count = book.get('newRatingCount')
        result['books'].append({
            'book_id': bid, 'title': book.get('title'), 'author': book.get('author'),
            'copyright_provider': provider['name'], 'copyright_id': book['cpid'],
            'recent_list_time': timestamp, 'recent_list_time_raw': meta['updateTime'],
            'recent_list_time_source': 'shelf/opus data[].meta.updateTime; can mean serial update',
            'platform_listing_time': None,
            'listing_time_status': 'requires_detail_onTime; do not substitute list updateTime',
            'recommendation_percent': rating(book.get('newRating')),
            'reviewer_count': count if type(count) is int and count >= 0 else None,
            'today_count_raw': meta.get('todayCount'),
            'cumulative_reader_count': None,
            'reader_url': book.get('readerLink'), 'format': book['format'],
            'categories': book.get('categories'), 'observed_at': evidence['observed_at'],
        })
    result['unique_ids'] = len(seen)
    if len(rows) != total or len(seen) != total:
        result['issues'].append('Returned unique IDs do not match declared totalCount')
    if not result['issues']:
        result['status'] = 'complete'
    return result


def collect(providers, session, output, fetch=fetch_recent):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new output directory to preserve previous observations')
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for index, provider in enumerate(providers):
        try:
            body, evidence = fetch(provider, session)
            write_json(output / f'provider-{index}-raw.json', body)
            result = validate_recent(body, provider, evidence)
        except Exception as error:
            # Do not persist exception messages or request headers containing session material.
            result = {'provider': provider['name'], 'status': 'source_error',
                      'error_type': type(error).__name__, 'observed_at': now()}
        results.append(result)
        write_json(output / f'provider-{index}.json', result)
    summary = {'observed_at': now(), 'complete': all(r['status'] == 'complete' for r in results),
               'providers': [{k: v for k, v in r.items() if k != 'books'} for r in results],
               'base_writes': 0, 'notifications': 0, 'baseline_advanced': False}
    write_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--providers-file', required=True, help='Private JSON array: name, user_vid, copyright_id')
    parser.add_argument('--session-file', required=True, help='Encrypted .dpapi or .aesgcm session; never commit')
    parser.add_argument('--out-dir', required=True, help='New private observation directory')
    args = parser.parse_args()
    providers = json.loads(Path(args.providers_file).read_text(encoding='utf-8'))
    if not isinstance(providers, list) or not providers:
        raise ValueError('At least one verified provider is required')
    summary = collect(providers, load_session(args.session_file), args.out_dir)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
