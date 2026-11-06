"""Renew the explicitly authorized local account; never log authentication bodies."""
import hashlib
import json
import secrets
import time
import urllib.request

try:
    from .collect_weread_recent import NoRedirect, load_session
    from .session_store import protect, save_session
except ImportError:
    from collect_weread_recent import NoRedirect, load_session
    from session_store import protect, save_session


def refreshed(previous, result):
    if str(result.get('vid', previous['vid'])) != str(previous['vid']):
        raise ValueError('Renewal account mismatch')
    if not isinstance(result.get('accessToken'), str) or not result['accessToken']:
        raise ValueError('Renewal failed; explicit login required')
    refresh = result.get('refreshToken', previous['refreshToken'])
    if not isinstance(refresh, str) or not refresh:
        raise ValueError('Renewal returned invalid refresh token')
    return {**previous, 'accessToken': result['accessToken'], 'refreshToken': refresh}


def renew(path):
    """Caller holds the daily lock. One request, no redirects or blind retries."""
    previous = load_session(path)
    device = previous['deviceId']
    stamp, random = int(time.time()*1000), secrets.randbelow(1000)+1
    body = {'deviceId': device, 'deviceName': 'ebook monitor', 'deviceType': 3,
            'inBackground': 0, 'kickType': 1, 'random': random, 'refCgi': '',
            'refreshToken': previous['refreshToken'], 'timestamp': stamp, 'trackId': '',
            'signature': hashlib.sha256(f'{stamp}{device}{random}'.encode()).hexdigest()}
    headers = {'Content-Type': 'application/json; charset=UTF-8',
               'User-Agent': 'WeRead/2.1.2 WRBrand/Onyx wr_eink Dalvik/2.1.0 (Linux; U; Android 11; BOOX Build/onyx)',
               'baseapi': '30', 'appver': '2.1.2.10245900', 'basever': '2.1.2.10245900', 'osver': '11', 'channelId': '900'}
    request = urllib.request.Request('https://i.weread.qq.com/login', data=json.dumps(body).encode(), headers=headers)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
        result = json.load(response)
    session = refreshed(previous, result)
    save_session(path, session, replace=True)
    return session
