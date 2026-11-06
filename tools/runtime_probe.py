"""Non-business probe for a candidate runner. Never imports the daily pipeline.

The marker and its digest must survive a DIFFERENT Dot task. A successful local
check is not evidence of durable storage or independent scheduling by itself.
"""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

URLS = {
    'tencent': 'https://docs.qq.com/', 'weread': 'https://weread.qq.com/',
    'weread_api': 'https://i.weread.qq.com/', 'wechat_login': 'https://open.weixin.qq.com/',
    'wechat_poll': 'https://long.open.weixin.qq.com/', 'fanqie': 'https://fanqienovel.com/',
    'feishu': 'https://open.feishu.cn/',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def describe():
    try:
        beijing_zone, zoneinfo_available = ZoneInfo('Asia/Shanghai'), True
    except ZoneInfoNotFoundError:
        beijing_zone, zoneinfo_available = timezone(timedelta(hours=8)), False
    return {'observed_at': now(), 'python': platform.python_version(),
            'platform': sys.platform, 'pid': os.getpid(),
            'beijing_time': datetime.now(beijing_zone).isoformat(),
            'asia_shanghai_zoneinfo_available': zoneinfo_available,
            'lark_cli_found': bool(shutil.which('lark-cli') or shutil.which('lark-cli.cmd')),
            'base_writes': 0, 'notifications': 0, 'scheduler_created': False}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def begin(path):
    path = Path(path)
    payload = {**describe(), 'schema': 'ebook.runtime-probe.v1', 'nonce': secrets.token_hex(32)}
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return {**describe(), 'marker': str(path.resolve()), 'expected_sha256': digest(raw),
            'status': 'marker_created', 'persistence_verified': False}


def check(path, expected):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError('Invalid probe marker')
    raw = path.read_bytes()
    if digest(raw) != expected:
        raise ValueError('Probe marker hash mismatch; do not recreate it')
    data = json.loads(raw)
    if data.get('schema') != 'ebook.runtime-probe.v1':
        raise ValueError('Unexpected probe schema')
    return {**describe(), 'status': 'marker_preserved', 'expected_sha256': expected,
            'marker_created_at': data['observed_at'], 'marker': str(path.resolve()),
            'requires_independent_task_and_schedule_evidence': True,
            'business_migration_ready': False}


def network():
    result = {}
    # Authentication is not attempted. HTTP errors prove only endpoint reachability.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    for name, url in URLS.items():
        try:
            with opener.open(urllib.request.Request(url, headers={'User-Agent': 'ebook-runtime-probe/1'}), timeout=8) as response:
                result[name] = {'http_response_received': True, 'http_status': response.status}
        except urllib.error.HTTPError as error:
            result[name] = {'http_response_received': True, 'http_status': error.code}
            error.close()
        except (OSError, urllib.error.URLError) as error:
            result[name] = {'http_response_received': False, 'error_type': type(error).__name__}
    return {**describe(), 'endpoints': result, 'authenticated_collection_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('begin')
    start.add_argument('--marker', required=True)
    verify = sub.add_parser('check')
    verify.add_argument('--marker', required=True)
    verify.add_argument('--expected-sha256', required=True)
    sub.add_parser('network')
    args = parser.parse_args()
    try:
        result = (begin(args.marker) if args.command == 'begin' else
                  check(args.marker, args.expected_sha256) if args.command == 'check' else network())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if args.command != 'network' or all(v['http_response_received'] for v in result['endpoints'].values()) else 2
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__, 'base_writes': 0}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
