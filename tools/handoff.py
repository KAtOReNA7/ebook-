"""Export/restore private state separately from the public Git repository.

No authentication caches, environment files or external account directories are
read. Restore never enables notifications, starts a scheduler or calls Feishu.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.daily_run import process_lock
from scripts.initialize_manual_projects import unknown_guard_paths
from tools.check_public_tree import PUBLIC_POLICIES

STATE_DIRS = {'config', 'runs', 'receipts', 'research', 'reference'}
EXTENSIONS = {'.json', '.jsonp', '.ndjson', '.txt', '.md', '.log', '.html', '.png', '.jpg', '.jpeg', '.csv', '.tsv'}
SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'site-packages', 'dist-info'}
MANIFEST = 'handoff-manifest.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def code_commit(root):
    try:
        reply = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'],
                               capture_output=True, text=True, timeout=10)
        return reply.stdout.strip() if reply.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def safe_name(name):
    path = PurePosixPath(name)
    if ('\\' in name or ':' in name or path.is_absolute() or '..' in path.parts
            or str(path) != name or not path.parts
            or (path.parts[0] not in STATE_DIRS and name != 'RUNBOOK.md')):
        raise ValueError('Invalid state archive path: ' + name)
    return path


def state_files(root):
    result = []
    if (root / 'RUNBOOK.md').is_file():
        result.append(root / 'RUNBOOK.md')
    for folder in sorted(STATE_DIRS):
        for directory, children, files in os.walk(root / folder):
            children[:] = sorted(d for d in children if d not in SKIP_DIRS and not d.endswith('.dist-info'))
            for name in sorted(files):
                path = Path(directory) / name
                # Exact-commit Git checkout supplies public policies. Omitting
                # them also avoids CRLF/LF conflicts during cross-OS restore.
                if path.parent == root / 'config' and name in PUBLIC_POLICIES:
                    continue
                if (path.suffix.lower() in EXTENSIONS and not name.endswith('.example.json')
                        and not path.is_symlink() and not name.startswith('.env')
                        and not any(word in name.lower() for word in ('cookie', 'credential', 'password', 'auth-cache'))):
                    if not path.resolve().is_relative_to(root):
                        raise ValueError('State file resolves outside project root')
                    result.append(path)
    return sorted(result)


def export_state(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('Refusing to replace an existing handoff archive')
    if output.is_relative_to(root) and output.parts[len(root.parts):][0] in STATE_DIRS:
        raise ValueError('Archive must be outside the state input directories')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.partial')
    # Both entry points use these locks. Never steal locks based on file age.
    with ExitStack() as stack:
        stack.enter_context(process_lock(root / 'runs/.daily.lock'))
        stack.enter_context(process_lock(root / 'runs/.tencent.lock'))
        if unknown_guard_paths(root):
            raise RuntimeError('Unresolved mutation guard; reconcile before exporting state')
        files = state_files(root)
        manifest = {'schema': 'ebook.private-handoff.v1', 'source_root': str(root),
                    'code_commit': code_commit(root),
                    'created_at': datetime.now(timezone.utc).isoformat(), 'files': [],
                    'warning': 'PRIVATE business state. Do not upload to GitHub or share publicly. No account login is included.'}
        try:
            with temporary.open('xb') as handle, zipfile.ZipFile(handle, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for path in files:
                    name = path.relative_to(root).as_posix()
                    safe_name(name)
                    data = path.read_bytes()
                    manifest['files'].append({'path': name, 'size': len(data), 'sha256': digest(data)})
                    archive.writestr(name, data)
                # Do not issue a valid snapshot if the source changed while copying.
                for entry in manifest['files']:
                    if digest((root / entry['path']).read_bytes()) != entry['sha256']:
                        raise RuntimeError('State changed during export; retry after the active writer stops')
                if state_files(root) != files:
                    raise RuntimeError('State inventory changed during export; retry after the active writer stops')
                archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
            if output.exists():
                raise FileExistsError('Output appeared during export')
            temporary.rename(output)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return {'archive': str(output), 'files': len(files), 'bytes': output.stat().st_size,
            'sha256': digest(output.read_bytes()), 'private': True}


def inspect_archive(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive member')
        manifest = json.loads(archive.read(MANIFEST))
        if manifest.get('schema') != 'ebook.private-handoff.v1':
            raise ValueError('Unsupported handoff schema')
        entries = manifest['files']
        expected = [entry['path'] for entry in entries]
        if len(expected) != len(set(expected)) or set(names) != {MANIFEST, *expected}:
            raise ValueError('Archive inventory does not match its manifest')
        for entry in entries:
            safe_name(entry['path'])
            data = archive.read(entry['path'])
            if len(data) != entry['size'] or digest(data) != entry['sha256']:
                raise ValueError('State checksum mismatch: ' + entry['path'])
        return manifest


def restore_state(archive_path, root):
    root = Path(root).resolve()
    if not (root / 'scripts/daily_run.py').is_file():
        raise ValueError('Restore into a cloned project, not an arbitrary directory')
    manifest = inspect_archive(archive_path)
    if manifest.get('code_commit') and code_commit(root) != manifest['code_commit']:
        raise ValueError('Restore requires the exact code commit recorded in the private archive: ' + manifest['code_commit'])
    # Preflight every target before writing anything. Existing identical public
    # policies are allowed; a different file or a symlink is never overwritten.
    for entry in manifest['files']:
        target = root / entry['path']
        if not target.resolve().is_relative_to(root) or target.is_symlink():
            raise ValueError('Restore path escapes checkout')
        if target.exists() and (not target.is_file() or digest(target.read_bytes()) != entry['sha256']):
            raise FileExistsError('Existing local state differs; restore into a fresh clone: ' + entry['path'])
    marker = root / 'config/handoff-restored.json'
    if marker.exists():
        raise FileExistsError('This checkout already restored state')
    with zipfile.ZipFile(archive_path) as archive:
        for entry in manifest['files']:
            target = root / entry['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                with target.open('xb') as handle:
                    handle.write(archive.read(entry['path']))
    runtime_file = root / 'config/runtime.json'
    runtime = json.loads(runtime_file.read_text(encoding='utf-8-sig')) if runtime_file.exists() else {}
    runtime.update({'mode': 'shadow', 'notifications_enabled': False, 'production_approved': False,
                    'historical_outboxes': 'never_replay', 'deployment_stage': 'handoff_review'})
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding='utf-8')
    # A restored snapshot never carries authority to become a second writer.
    runner_file = root / 'config/runner-host.json'
    runner_file.write_text(json.dumps({'schema': 'ebook.runner-host.v1', 'role': 'standby',
        'host_fingerprint': None, 'pinned_commit': manifest.get('code_commit'),
        'previous_writer_stopped': False, 'target_dry_run_reviewed': False}, indent=2), encoding='utf-8')
    origin_file = root / 'config/workspace-origin.json'
    origins = json.loads(origin_file.read_text(encoding='utf-8-sig')).get('roots', []) if origin_file.exists() else []
    origin_file.write_text(json.dumps({'roots': list(dict.fromkeys([manifest['source_root'], *origins]))}, indent=2), encoding='utf-8')
    marker.write_text(json.dumps({'schema': 'ebook.handoff-restored.v1',
        'restored_at': datetime.now(timezone.utc).isoformat(), 'archive_sha256': digest(Path(archive_path).read_bytes()),
        'source_root': manifest['source_root'], 'notifications_forced_off': True, 'scheduler_created': False,
        'next': 'Authenticate Feishu locally, run doctor and daily --dry-run, reconcile live Base. Stop the old scheduler before any new writer.'},
        ensure_ascii=False, indent=2), encoding='utf-8')
    return {'files': len(manifest['files']), 'destination': str(root), 'notifications_enabled': False,
            'scheduler_created': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('--root', type=Path, default=ROOT)
    export.add_argument('--output', type=Path, required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--archive', type=Path, required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('--archive', type=Path, required=True)
    restore.add_argument('--destination', type=Path, default=ROOT)
    args = parser.parse_args()
    if args.command == 'export':
        result = export_state(args.root, args.output)
    elif args.command == 'restore':
        result = restore_state(args.archive, args.destination)
    else:
        manifest = inspect_archive(args.archive)
        result = {'files': len(manifest['files']), 'checksums': 'passed', 'private': True}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
