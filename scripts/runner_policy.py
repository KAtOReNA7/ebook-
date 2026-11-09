"""Accidental-write guard for development/standby checkouts; not a distributed lock."""
import getpass
import hashlib
import json
from pathlib import Path
import platform
import subprocess

READ_COMMANDS = {'+record-list', '+record-get', '+record-search', '+field-list',
                 '+field-get', '+field-search-options', '+table-list', '+table-get',
                 '+view-list', '+view-get', '+workflow-list', '+workflow-get'}


def host_fingerprint():
    # Device/account binding prevents accidental reuse of an imported config.
    # It is not intended to resist someone deliberately changing local code.
    identity = platform.system() + '\n' + platform.node().casefold() + '\n' + getpass.getuser().casefold()
    return hashlib.sha256(identity.encode()).hexdigest()


def revision(root):
    return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                   text=True, timeout=10).strip()


def enforce_writer(root, *, require_config=False, verify_commit=False):
    try:
        from .shared_runtime import configured, assert_shared_writer
    except ImportError:
        from shared_runtime import configured, assert_shared_writer
    if configured(root):
        # Shared authorization replaces a fixed machine as the writer. A
        # development checkout still cannot mutate without the acquired mutex.
        assert_shared_writer(root)
        return
    path = Path(root) / 'config/runner-host.json'
    if not path.exists():
        if require_config:
            raise RuntimeError('Runner has not been prepared and activated')
        return  # Preserve the existing operational checkout until controlled cutover.
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if config.get('schema') != 'ebook.runner-host.v1' or config.get('role') != 'active':
        raise PermissionError('Development/standby checkout cannot write business data; use --dry-run')
    if config.get('host_fingerprint') != host_fingerprint():
        raise PermissionError('Active runner belongs to another device/account')
    if config.get('previous_writer_stopped') is not True or config.get('target_dry_run_reviewed') is not True:
        raise PermissionError('Previous writer and target dry-run review are not confirmed')
    if verify_commit and config.get('pinned_commit') != revision(root):
        raise PermissionError('Code differs from the verified runner release')
    return config
