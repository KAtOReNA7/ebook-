"""Machine-local identities and byte-preserving historical path resolution.

Local configuration is intentionally not versioned. Missing settings can support
offline tests but cannot authorize notification transport.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def load_settings(root=ROOT):
    path = Path(root) / 'config/local-system.json'
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}


SETTINGS = load_settings()
SOURCE_URL = SETTINGS.get('tencent_source_url', 'https://docs.qq.com/sheet/EXAMPLE_SOURCE_ID?tab=BB08J2')
SOURCE_DOCUMENT_KEY = urlsplit(SOURCE_URL).path.rstrip('/').split('/')[-1]
OWNER_ID = SETTINGS.get('owner_open_id', 'ou_unconfigured_owner')
OWNER_NAME = SETTINGS.get('owner_name', '项目所有者')
OWNER_CONFIGURED = OWNER_ID.startswith('ou_') and OWNER_ID != 'ou_unconfigured_owner'


def project_path(value, root=ROOT):
    """Rebase paths under recorded workspace roots; never edit hashed evidence.

    Unrelated absolute paths retain their normal meaning. Matching Windows
    prefixes on another OS is deliberate: a Windows snapshot can be inspected
    on Linux without creating a directory literally named ``E:``.
    """
    root = Path(root).resolve()
    raw = os.fspath(value)
    origin_file = root / 'config/workspace-origin.json'
    origins = json.loads(origin_file.read_text(encoding='utf-8-sig')).get('roots', []) if origin_file.exists() else []
    normalized = raw.replace('\\', '/')
    for origin in [str(root), *origins]:
        prefix = str(origin).replace('\\', '/').rstrip('/')
        windows = len(prefix) >= 2 and prefix[1] == ':'
        candidate, expected = (normalized.casefold(), prefix.casefold()) if windows else (normalized, prefix)
        if candidate == expected or candidate.startswith(expected + '/'):
            relative = normalized[len(prefix):].lstrip('/')
            result = (root / relative).resolve()
            if not result.is_relative_to(root):
                raise ValueError('Historical workspace path escapes project root')
            return result
    path = Path(raw)
    if path.is_absolute() or (len(normalized) >= 3 and normalized[1:3] == ':/'):
        return path
    result = (root / path).resolve()
    if not result.is_relative_to(root):
        raise ValueError('Relative project path escapes project root')
    return result
