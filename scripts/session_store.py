"""Encrypted WeRead sessions. DPAPI stays the default; portable storage is opt-in.

No plaintext import or automatic conversion of another computer's credentials.
The caller serializes login/renewal with the daily lock.
"""
import base64
import ctypes
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile

MAGIC = b'ebook.weread.aesgcm.v1\x00'
KEY_ENV = 'EBOOK_WEREAD_KEY_FILE'
MAX_SESSION = 1024 * 1024


def _dpapi(payload, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('DPAPI requires its original Windows user; authorize a new portable session')

    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(source), 'ebook WeRead session', None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise RuntimeError('Cannot protect/open the authorized Windows session')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


def protect(payload):
    """Compatibility with the existing Windows adapter."""
    return _dpapi(payload)


def _cipher():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise RuntimeError('Install requirements-dot.txt for portable session storage') from None
    return AESGCM


def _key_path():
    value = os.environ.get(KEY_ENV)
    if not value or not Path(value).is_absolute():
        raise ValueError('EBOOK_WEREAD_KEY_FILE must name an absolute, private file outside the checkout')
    path = Path(value)
    if path.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Session key must be outside the checkout and handoff archive')
    if path.is_symlink():
        raise ValueError('Session key cannot be a symlink')
    return path


def _private_file(path):
    if path.is_symlink():
        raise ValueError('Private file cannot be a symlink')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Private file must be a regular file')
    if os.name == 'posix' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise PermissionError('Private file must belong to this user with mode 0600')


def create_key():
    """Explicit first-time setup. Never print or replace the secret."""
    _cipher()
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(base64.b64encode(secrets.token_bytes(32)))
        handle.flush()
        os.fsync(handle.fileno())


def _key():
    path = _key_path()
    _private_file(path)
    try:
        raw = path.read_bytes()
        value = base64.b64decode(raw.strip(), validate=True) if len(raw) <= 128 else b''
    except (ValueError, OSError):
        raise ValueError('Invalid portable session key file') from None
    if len(value) != 32:
        raise ValueError('Portable session requires a 256-bit key')
    return value


def backend(path):
    suffix = Path(path).suffix.lower()
    if suffix not in ('.dpapi', '.aesgcm'):
        raise ValueError('Session file must end with .dpapi or .aesgcm; plaintext is not supported')
    return suffix


def validate_session(session):
    if not isinstance(session, dict) or not session.get('vid') or not isinstance(session.get('accessToken'), str) or not session['accessToken']:
        raise ValueError('Session lacks the required identity fields')
    return session


def load_session(path):
    path = Path(path)
    kind = backend(path)
    if kind == '.aesgcm':
        _private_file(path)
    if path.stat().st_size > MAX_SESSION:
        raise ValueError('Session exceeds size limit')
    payload = path.read_bytes()
    if kind == '.dpapi':
        plain = _dpapi(payload, decrypt=True)
    else:
        if not payload.startswith(MAGIC) or len(payload) < len(MAGIC) + 12 + 16:
            raise ValueError('Invalid encrypted session envelope')
        cipher = _cipher()(_key())
        from cryptography.exceptions import InvalidTag
        try:
            plain = cipher.decrypt(payload[len(MAGIC):len(MAGIC)+12], payload[len(MAGIC)+12:], MAGIC)
        except InvalidTag:
            raise ValueError('Session authentication failed; key or ciphertext mismatch') from None
    return validate_session(json.loads(plain))


def save_session(path, session, *, replace=False):
    """Encrypt before touching disk. Create exclusively, or replace after renewal."""
    path = Path(path)
    kind = backend(path)
    raw = json.dumps(validate_session(session), ensure_ascii=False).encode('utf-8')
    if len(raw) > MAX_SESSION - 256:
        raise ValueError('Session exceeds size limit')
    if kind == '.dpapi':
        encrypted = protect(raw)
    else:
        nonce = secrets.token_bytes(12)
        encrypted = MAGIC + nonce + _cipher()(_key()).encrypt(nonce, raw, MAGIC)
    if path.is_symlink():
        raise ValueError('Session target cannot be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not replace:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        return
    # Renewal must never silently recreate a lost session.
    if not path.is_file():
        raise FileNotFoundError('Renewal requires an existing session')
    fd, temporary = tempfile.mkstemp(prefix='.session-', suffix=kind, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
