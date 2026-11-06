"""Fresh, explicit QR authorization on the target computer. No Base operations.

Never reads/copies another device's login. Authentication responses stay in memory.
The official login sequence is the one already used by the local research probe.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.collect_weread_recent import NoRedirect
from scripts.daily_run import process_lock
from scripts.session_store import backend, create_key, save_session, _cipher, _key

HEADERS = {
    'User-Agent': 'WeRead/2.1.2 WRBrand/Onyx wr_eink Dalvik/2.1.0 (Linux; U; Android 11; BOOX Build/onyx)',
    'baseapi': '30', 'appver': '2.1.2.10245900', 'basever': '2.1.2.10245900',
    'osver': '11', 'channelId': '900',
}


def request(url, data=None, timeout=25):
    headers = dict(HEADERS)
    if data is not None:
        headers['Content-Type'] = 'application/json; charset=UTF-8'
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(), headers=headers)
    with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
        raw = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 1024 * 1024:
            raise ValueError('Unexpected authentication response')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Unexpected authentication response shape')
        return result


def request_qr(fetch=request):
    ticket = fetch('https://i.weread.qq.com/wxticket?nonceStr=weread')
    if not isinstance(ticket.get('signature'), str) or not ticket['signature'] or ticket.get('timeStamp') is None:
        raise ValueError('QR ticket request failed')
    query = urllib.parse.urlencode({
        'appid': 'wxab9b71ad2b90ff34', 'noncestr': 'weread', 'timestamp': ticket['timeStamp'],
        'scope': 'snsapi_userinfo,snsapi_timeline,snsapi_friend', 'signature': ticket['signature'],
    })
    result = fetch('https://open.weixin.qq.com/connect/sdk/qrconnect?' + query)
    uuid = result.get('uuid')
    if result.get('errcode') != 0 or not isinstance(uuid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', uuid):
        raise ValueError('QR creation failed')
    return uuid


def poll(uuid, seconds=240, fetch=request, clock=time.monotonic, sleep=time.sleep):
    deadline, last = clock() + seconds, None
    while clock() < deadline:
        query = {'f': 'json', 'uuid': uuid}
        if last is not None:
            query['last'] = last
        try:
            body = fetch('https://long.open.weixin.qq.com/connect/l/qrconnect?' + urllib.parse.urlencode(query),
                         timeout=max(1, min(15, deadline - clock())))
        except TimeoutError:
            sleep(1)
            continue
        last = body.get('wx_errcode')
        if last == 405:
            if not isinstance(body.get('wx_code'), str) or not body['wx_code']:
                raise ValueError('Confirmation omitted login code')
            return body['wx_code']
        if last not in (404, 408):
            raise ValueError('QR expired, declined, or returned an unknown status')
        sleep(1)
    raise TimeoutError('QR confirmation timed out')


def exchange(code, fetch=request):
    device = 'eink334691225' + str(secrets.randbits(63)).zfill(19)
    stamp, random = int(time.time() * 1000), secrets.randbelow(1000)
    result = fetch('https://i.weread.qq.com/login', {
        'appFirstInstall': 1, 'code': code, 'deviceId': device, 'deviceName': 'ebook monitor',
        'installId': 'eink31' + ''.join(str(secrets.randbelow(10)) for _ in range(26)),
        'isAutoLogout': 0, 'isFromQrcode': 1, 'random': random,
        'signature': hashlib.sha256(f'{stamp}{device}{random}'.encode()).hexdigest(),
        'timestamp': stamp, 'trackId': '', 'deviceType': 3,
    })
    if any(result.get(k) not in (None, 0) for k in ('errCode', 'errcode')) or not result.get('vid'):
        raise ValueError('Login rejected')
    if any(not isinstance(result.get(k), str) or not result[k] for k in ('accessToken', 'refreshToken')):
        raise ValueError('Login did not supply a renewable session')
    return {**{k: result[k] for k in ('vid', 'accessToken', 'refreshToken')}, 'deviceId': device}


def login(path, seconds=240):
    path = Path(path)
    kind = backend(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError('Use a new session file; existing authorization is never overwritten by login')
    if kind == '.aesgcm':
        _cipher()
        _key()  # Fail before generating a QR if storage is not configured.
    else:
        import os
        if os.name != 'nt':
            raise RuntimeError('Use .aesgcm on Linux')
    import qrcode
    import qrcode.image.svg
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    qr_path = path.with_name('weread-login-' + secrets.token_hex(8) + '.svg')
    uuid = request_qr()
    confirm_url = 'https://open.weixin.qq.com/connect/confirm?' + urllib.parse.urlencode({'uuid': uuid})
    try:
        with qr_path.open('xb') as handle:
            qrcode.make(confirm_url, image_factory=qrcode.image.svg.SvgPathImage).save(handle)
        print(json.dumps({'status': 'awaiting_user_scan', 'qr_svg': str(qr_path.resolve()),
                          'base_writes': 0, 'notifications': 0}), flush=True)
        session = exchange(poll(uuid, seconds))
        save_session(path, session)
        print(json.dumps({'status': 'authorized_and_encrypted', 'backend': kind,
                          'base_writes': 0, 'notifications': 0}), flush=True)
    finally:
        qr_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init-key', help='Create a new key at EBOOK_WEREAD_KEY_FILE; never print it')
    start = sub.add_parser('login')
    start.add_argument('--session-file', required=True)
    start.add_argument('--timeout-seconds', type=int, default=240, choices=range(30, 601), metavar='30..600')
    args = parser.parse_args()
    try:
        if args.command == 'init-key':
            create_key()
            print('{"status":"key_created","base_writes":0}')
        else:
            with process_lock(ROOT / 'runs/.daily.lock'):
                login(args.session_file, args.timeout_seconds)
        return 0
    except Exception as error:
        # Never emit response bodies, headers, codes, keys or token-bearing URLs.
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
                          'hint': 'Check dependencies, private storage, network or QR expiry; no business writes'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
