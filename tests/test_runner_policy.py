import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.runner_policy import enforce_writer, host_fingerprint
from scripts.feishu_io import Base
from tools.scheduled_runner import execute, prepare


class RunnerTests(unittest.TestCase):
    def config(self, root, **changes):
        value = {'schema': 'ebook.runner-host.v1', 'role': 'active',
                 'host_fingerprint': host_fingerprint(), 'pinned_commit': 'fixture',
                 'previous_writer_stopped': True, 'target_dry_run_reviewed': True, **changes}
        path = root / 'config/runner-host.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_existing_checkout_compatible_but_native_scheduler_requires_config(self):
        with TemporaryDirectory() as directory:
            self.assertIsNone(enforce_writer(directory))
            with self.assertRaises(RuntimeError):
                enforce_writer(directory, require_config=True)

    def test_developer_standby_and_other_device_cannot_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for value in ({'role': 'development'}, {'role': 'standby'},
                          {'host_fingerprint': 'other'}, {'previous_writer_stopped': False},
                          {'target_dry_run_reviewed': False}):
                self.config(root, **value)
                with self.assertRaises(PermissionError):
                    enforce_writer(root)

    def test_code_version_mismatch_blocks_runner(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.config(root)
            with patch('scripts.runner_policy.revision', return_value='new-unreviewed'):
                with self.assertRaises(PermissionError):
                    enforce_writer(root, verify_commit=True)

    def test_direct_base_write_is_blocked_before_cli_or_payload_processing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.config(root, role='development')
            api = Base.__new__(Base)
            with patch('scripts.feishu_io.ROOT', root), patch('scripts.feishu_io.subprocess.run') as run:
                with self.assertRaises(PermissionError):
                    api.call('+record-batch-create', payload={'create_records': []})
                run.assert_not_called()

    def test_preparation_never_activates_and_preserves_previous_nonactive_policy(self):
        with TemporaryDirectory() as directory, patch('tools.scheduled_runner.revision', return_value='fixture'):
            root = Path(directory)
            self.assertEqual(prepare(root, 'standby')['role'], 'standby')
            self.assertEqual(prepare(root, 'development')['role'], 'development')
            self.assertEqual(len(list((root / 'receipts').glob('*/runner-host-before-*.json'))), 1)
            self.config(root)
            with self.assertRaises(PermissionError):
                prepare(root, 'standby')

    def test_missing_policy_stops_native_live_job_without_starting_child(self):
        with TemporaryDirectory() as directory, patch('tools.scheduled_runner.subprocess.run') as run:
            result, output = execute(directory, False)
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(result['process_exit_code'], 3)
            self.assertTrue((output / 'result.json').is_file())
            run.assert_not_called()

    def test_dry_run_can_read_from_standby_and_propagates_child_failure(self):
        with TemporaryDirectory() as directory, patch('tools.scheduled_runner.revision', return_value='fixture'):
            root = Path(directory)
            self.config(root, role='standby')
            with patch('tools.scheduled_runner.subprocess.run') as run:
                run.return_value.returncode = 2
                result, output = execute(root, True)
                self.assertIn('--dry-run', run.call_args.args[0])
                self.assertEqual(run.call_count, 1)
            self.assertEqual(result['process_exit_code'], 2)
            self.assertEqual(result['automatic_retries'], 0)
            self.assertTrue((output / 'pipeline.log').is_file())

    def test_unknown_guard_stops_live_job_without_retry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.config(root)
            with patch('scripts.runner_policy.revision', return_value='fixture'), \
                 patch('tools.scheduled_runner.unknown_guard_paths', return_value=['pending']), \
                 patch('tools.scheduled_runner.subprocess.run') as run:
                result, output = execute(root, False)
                self.assertEqual(result['status'], 'blocked')
                run.assert_not_called()
