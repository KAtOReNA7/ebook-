"""Read-only readiness checks. No lock acquisition, cache restore or Base mutation."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.shared_runtime import SCHEMA, Store, Mutex, configuration, load
from scripts.feishu_io import Base, resolve_cli
from scripts.initialize_manual_projects import unknown_guard_paths


def check(root=ROOT, online=False):
    root = Path(root)
    checks = []
    def add(name, ok, detail):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
    add('python', sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        resolve_cli()
        add('lark_cli', True, 'executable available; online Base read verifies access')
    except (ValueError, FileNotFoundError):
        add('lark_cli', False, 'install CLI or set EBOOK_LARK_CLI')
    add('github_cli', shutil.which('gh') is not None, 'gh login required for mutex')
    for name in ('base-manifest.json', 'local-system.json'):
        add(name, (root/'config'/name).is_file(), 'machine-local setup; never copied from shared state')
    guards = unknown_guard_paths(root)
    add('local_mutations', not guards, {'unresolved_count': len(guards)})
    publisher = root/'config/publisher-newbooks.json'
    policy = load(publisher) if publisher.exists() else {}
    if online:
        # Local receipts hold GET results only. No secrets are printed by this command.
        try:
            _, _, mutex = Mutex(configuration(root)['coordination_repository']).head()
            add('shared_mutex', mutex.get('phase') == 'idle', {'phase': mutex.get('phase')})
        except Exception as error:
            add('shared_mutex', False, {'error_type': type(error).__name__})
        try:
            api = Base(root/'receipts'/'runner-readiness'/uuid.uuid4().hex)
            checkpoint, meta = Store(api).discover().read()
            add('shared_checkpoint', not checkpoint.get('unresolved_guards'),
                {'schema': checkpoint['schema'], 'supported_writer_schema': SCHEMA,
                 'generation': meta['generation'], 'saved_at': meta.get('saved_at'),
                 'files': len(checkpoint['files']),
                 'unresolved_count': len(checkpoint.get('unresolved_guards', []))})
            shared = checkpoint['files'].get('config/publisher-business.json')
            if shared:
                policy = {**json.loads(shared['text']), 'session_file': policy.get('session_file')}
        except Exception as error:
            add('shared_checkpoint', False, {'error_type': type(error).__name__})
    if policy.get('enabled'):
        name = policy.get('session_file')
        session = Path(name) if name else None
        if session and not session.is_absolute(): session = root/session
        add('publisher_session_file', session is not None and session.is_file(),
            'file presence only; real expiry/authorization is verified by daily dry-run')
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'online': online,
            'status': 'checks_passed' if all(c['ok'] for c in checks) else 'needs_attention',
            'checks': checks, 'base_mutations': 0,
            'limitations': ['Does not prove scheduler activation or unattended account access.',
                            'Does not validate all source sessions or completeness; run daily dry-run.',
                            'Does not steal or release a busy lock.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true')
    result = check(online=parser.parse_args().online)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['status'] == 'checks_passed' else 2)
