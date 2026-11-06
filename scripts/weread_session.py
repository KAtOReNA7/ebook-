"""Renew the explicitly authorized local account; never log authentication bodies."""
import ctypes
import hashlib
import json
import os
import secrets
import time
import urllib.request
from pathlib import Path

try:
    from .collect_weread_recent import NoRedirect, load_session
except ImportError:
    from collect_weread_recent import NoRedirect, load_session


def refreshed(previous, result):
    if str(result.get('vid', previous['vid'])) != str(previous['vid']):
        raise ValueError('Renewal account mismatch')
    if not isinstance(result.get('accessToken'), str) or not result['accessToken']:
        raise ValueError('Renewal failed; explicit login required')
    refresh = result.get('refreshToken', previous['refreshToken'])
    if not isinstance(refresh, str) or not refresh:
        raise ValueError('Renewal returned invalid refresh token')
    return {**previous, 'accessToken': result['accessToken'], 'refreshToken': refresh}


def protect(payload):
    if os.name != 'nt':
        raise RuntimeError('Local session requires current Windows user')
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(payload)
    src = Blob(len(payload), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dst = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), 'ebook WeRead session', None, None, None, 1, ctypes.byref(dst)):
        raise RuntimeError('Session encryption failed')
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.data)


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
    encrypted = protect(json.dumps(session).encode())
    path = Path(path)
    temporary = path.with_suffix('.renewing.dpapi')
    with temporary.open('wb') as handle:
        handle.write(encrypted); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
    return session
