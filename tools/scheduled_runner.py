"""OS-scheduled runner with private logs, pinned release and explicit writer gate.

No retries, no Git pull, no automatic failover or notification outside the pipeline.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.daily_run import atomic_json, process_lock
from scripts.initialize_manual_projects import unknown_guard_paths
from scripts.runner_policy import enforce_writer, host_fingerprint, revision

TZ = timezone(timedelta(hours=8))


def prepare(root, role):
    if role not in ('development', 'standby'):
        raise ValueError('Preparation cannot activate a writer')
    path = Path(root) / 'config/runner-host.json'
    data = {'schema': 'ebook.runner-host.v1', 'role': role,
            'host_fingerprint': host_fingerprint(), 'pinned_commit': revision(root),
            'previous_writer_stopped': False, 'target_dry_run_reviewed': False}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8-sig'))
        if previous.get('role') not in ('standby', 'development'):
            raise PermissionError('Active/unknown runner must be explicitly stopped before reconfiguration')
        backup = Path(root) / 'receipts' / datetime.now(TZ).date().isoformat() / ('runner-host-before-' + uuid.uuid4().hex + '.json')
        atomic_json(backup, previous)
    atomic_json(path, data)
    return data


def execute(root, dry_run, cli=None):
    root = Path(root).resolve()
    started = datetime.now(TZ)
    stamp = started.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    output = root / 'receipts' / started.date().isoformat() / 'native-runner' / stamp
    output.mkdir(parents=True)
    receipt = {'started_at': started.isoformat(), 'dry_run': dry_run, 'status': 'preflight',
               'pid': os.getpid(), 'automatic_retries': 0, 'scheduler_kind': 'operating_system'}
    atomic_json(output / 'result.json', receipt)
    try:
        # The child acquires the pipeline's normal lock. This lock serializes wrappers.
        with process_lock(root / 'runs/.native-runner.lock'):
            if not dry_run:
                config = enforce_writer(root, require_config=True, verify_commit=True)
                if config.get('previous_writer_stopped') is not True or config.get('target_dry_run_reviewed') is not True:
                    raise PermissionError('Cutover and target dry-run must be reviewed before activation')
                if unknown_guard_paths(root):
                    raise RuntimeError('Unknown/incomplete writes require read-only reconciliation')
            env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
            if cli:
                if not Path(cli).is_absolute() or not Path(cli).is_file():
                    raise ValueError('CLI must be an existing absolute path')
                env['EBOOK_LARK_CLI'] = str(cli)
            command = [sys.executable, str(root / 'scripts/daily_run.py'), '--run-kind', 'scheduled']
            if dry_run:
                command.append('--dry-run')
            receipt.update(status='running', code_commit=revision(root), python=sys.executable)
            atomic_json(output / 'result.json', receipt)
            with (output / 'pipeline.log').open('wb') as log:
                result = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
            receipt.update(status='completed' if result.returncode == 0 else 'needs_attention',
                           process_exit_code=result.returncode)
    except Exception as error:
        receipt.update(status='blocked', error_type=type(error).__name__,
                       reason=str(error)[:300], process_exit_code=3)
    receipt['finished_at'] = datetime.now(TZ).isoformat()
    receipt['pipeline_summary'] = str(root / 'runs' / started.date().isoformat() /
                                    ('pipeline-dry-run-summary.json' if dry_run else 'pipeline-summary.json'))
    receipt['note'] = 'Exit zero is not full coverage; inspect module statuses and full readback.'
    atomic_json(output / 'result.json', receipt)
    return receipt, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    config = sub.add_parser('prepare')
    config.add_argument('--role', choices=['development', 'standby'], default='standby')
    run = sub.add_parser('run')
    modes = run.add_mutually_exclusive_group(required=True)
    modes.add_argument('--dry-run', action='store_true')
    modes.add_argument('--execute', action='store_true')
    run.add_argument('--cli', type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        print(json.dumps(prepare(ROOT, args.role), indent=2))
        return 0
    result, output = execute(ROOT, args.dry_run, args.cli)
    print(json.dumps({'status': result['status'], 'receipt': str(output / 'result.json'),
                      'process_exit_code': result['process_exit_code']}))
    return result['process_exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
