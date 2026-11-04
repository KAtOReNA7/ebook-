"""Read observed public Tencent preload endpoint with normal session cookies.

Only page-observed endpoints/parameters and workbook-observed sheet IDs are used.
No authentication bypass, hidden endpoint discovery, Feishu writes or messages.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from http.cookiejar import CookieJar
from html import unescape
import json
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

try:
    from .parse_tencent import parse_package
    from .local_settings import SOURCE_URL
except ImportError:
    from parse_tencent import parse_package
    from local_settings import SOURCE_URL

ROOT = Path(__file__).resolve().parents[1]
SOURCE = SOURCE_URL
TZ = timezone(timedelta(hours=8))


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def observed_endpoint(html):
    for value in re.findall(r'''["']([^"']*opendoc[^"']*)["']''', html):
        value = unescape(value).replace(r'\/', '/')
        url = urllib.parse.urljoin(SOURCE, value)
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.netloc == 'docs.qq.com' and parsed.path == '/dop-api/opendoc' and 'block_start_row' in query:
            return url
    raise RuntimeError('No known public preload endpoint was observed in the source page')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    default = ROOT / 'runs' / datetime.now(TZ).date().isoformat() / 'tencent-source'
    out = (args.output or default).resolve()
    if (out / 'manifest.json').exists():
        existing = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
        if existing.get('status') == 'packages_collected':
            if args.output:
                raise ValueError('Completed source evidence is frozen; choose a new output directory for a fresh read')
            out = default.with_name(default.name + '-' + datetime.now(TZ).strftime('%H%M%S-%f'))
    out.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    opener.addheaders = [('User-Agent', 'Mozilla/5.0'), ('Referer', SOURCE)]

    def fetch(url, name, requested=None):
        with opener.open(url, timeout=45) as response:
            data = response.read()
            status = response.status
        (out / name).write_bytes(data)
        text = data.decode('utf-8-sig')
        metadata = {'file': name, 'http_status': status, 'bytes': len(data),
                    'sha256': hashlib.sha256(data).hexdigest(), 'observed_at': datetime.now(TZ).isoformat(),
                    'requested': requested}
        return text, metadata

    html, page_meta = fetch(SOURCE, 'source-page.html')
    endpoint = observed_endpoint(html)
    first, first_meta = fetch(endpoint, 'preload.jsonp')
    package = parse_package(first)
    sheets = package['sheets']
    if not sheets:
        raise RuntimeError('Workbook did not provide sheet inventory')
    manifest = {'source': SOURCE, 'started_at': page_meta['observed_at'], 'sheets': sheets,
                'source_page': page_meta, 'initial_preload': first_meta, 'pages': [], 'independent_reads': [], 'status': 'collecting'}
    save(out / 'manifest.json', manifest)
    parsed_url = urllib.parse.urlsplit(endpoint)
    base_query = urllib.parse.parse_qs(parsed_url.query)
    for sheet in sheets:
        for start in range(0, sheet['rows'], 256):
            end = min(start + 255, sheet['rows'] - 1)
            query = {key: list(values) for key, values in base_query.items()}
            updates = {'tab': sheet['sheet_id'], 'startrow': start, 'endrow': end,
                       'block_start_row': start, 'block_end_row': end}
            for key, value in updates.items():
                if key not in query:
                    raise RuntimeError('Pagination parameter was not observed: ' + key)
                query[key] = [str(value)]
            url = urllib.parse.urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path,
                                          urllib.parse.urlencode(query, doseq=True), ''))
            stem = sheet['sheet_id'] + '-' + str(start) + '-' + str(end)
            body, metadata = fetch(url, stem + '.jsonp', updates)
            parsed = parse_package(body)
            save(out / (stem + '-parsed.json'), parsed)
            actual_ids = sorted({cell['sheet_id'] for chunk in parsed['chunks'] for cell in chunk['cells']})
            # The workbook also carries small referenced ranges from other sheets.
            # Preserve them as evidence, but do not count them as full-page coverage.
            if sheet['sheet_id'] not in actual_ids:
                raise RuntimeError('Response sheet identity differs from requested sheet')
            metadata.update({'parsed_file': stem + '-parsed.json', 'actual_sheet_ids': actual_ids,
                             'block_metadata': [chunk['metadata'] for chunk in parsed['chunks']],
                             'value_ranges': [r for chunk in parsed['chunks'] for r in chunk['ranges']]})
            manifest['pages'].append(metadata)
            save(out / 'manifest.json', manifest)
            print(json.dumps({'sheet': sheet['name'], 'requested_rows': [start, end],
                              'chunks': len(parsed['chunks']), 'cell_messages': sum(len(c['cells']) for c in parsed['chunks'])}, ensure_ascii=False), flush=True)
            time.sleep(0.3)
    # Independent small-range reads check source coordinates and decoded values,
    # especially a page boundary and a formula-valued unit price.
    checks = [('BB08J2', 0, 4), ('BB08J2', 254, 257), ('BB08J2', 936, 938),
              ('wa845a', 21, 24), ('wa845a', 196, 198)]
    for sid, start, end in checks:
        query = {key: list(vals) for key, vals in base_query.items()}
        updates = {'tab': sid, 'startrow': start, 'endrow': end,
                   'block_start_row': start, 'block_end_row': end}
        for key, value in updates.items():
            query[key] = [str(value)]
        url = urllib.parse.urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path,
                                      urllib.parse.urlencode(query, doseq=True), ''))
        stem = 'verify-' + sid + '-' + str(start) + '-' + str(end)
        body, metadata = fetch(url, stem + '.jsonp', updates)
        parsed = parse_package(body)
        save(out / (stem + '-parsed.json'), parsed)
        metadata['parsed_file'] = stem + '-parsed.json'
        manifest['independent_reads'].append(metadata)
        save(out / 'manifest.json', manifest)
    manifest.update({'status': 'packages_collected', 'finished_at': datetime.now(TZ).isoformat()})
    save(out / 'manifest.json', manifest)
    print(json.dumps({'output': str(out), 'sheets': len(sheets), 'pages': len(manifest['pages']),
                      'status': manifest['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
