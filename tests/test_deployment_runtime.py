from contextlib import nullcontext
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import daily_run, feishu_io


class PortableCliTests(unittest.TestCase):
    def test_explicit_path_with_spaces_precedes_path_discovery(self):
        with TemporaryDirectory(prefix='ebook cli ') as directory:
            executable = Path(directory) / 'lark-cli'
            executable.touch()
            with patch.dict(os.environ, {'EBOOK_LARK_CLI': str(executable)}), \
                    patch.object(feishu_io.shutil, 'which') as lookup:
                self.assertEqual(feishu_io.resolve_cli(), str(executable.resolve()))
                lookup.assert_not_called()

    def test_invalid_explicit_command_cannot_fall_back(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / 'lark-cli'
            executable.touch()
            for invalid in ('', 'lark-cli', str(executable) + ' --as user'):
                with self.subTest(value=invalid), patch.dict(os.environ, {'EBOOK_LARK_CLI': invalid}), \
                        patch.object(feishu_io.shutil, 'which', return_value='another-cli') as lookup:
                    with self.assertRaises(ValueError):
                        feishu_io.resolve_cli()
                    lookup.assert_not_called()

    def test_path_install_is_used_without_windows_fallback(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(feishu_io.shutil, 'which', return_value='/opt/tools/lark-cli'):
            self.assertEqual(feishu_io.resolve_cli(), '/opt/tools/lark-cli')

    @unittest.skipUnless(os.name == 'nt', 'Compatibility fallback is Windows only')
    def test_windows_fallback_when_path_has_no_install(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / 'lark-cli.CMD'
            executable.touch()
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(feishu_io.shutil, 'which', return_value=None), \
                    patch.object(feishu_io, 'WINDOWS_CLI_FALLBACK', str(executable)):
                self.assertEqual(feishu_io.resolve_cli(), str(executable))

    def test_missing_install_fails_before_any_transport(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(feishu_io.shutil, 'which', return_value=None), \
                patch.object(feishu_io, 'WINDOWS_CLI_FALLBACK', 'missing-cli-does-not-exist'):
            with self.assertRaises(FileNotFoundError):
                feishu_io.resolve_cli()


class SchedulerStatusTests(unittest.TestCase):
    def test_known_gaps_do_not_masquerade_as_operational_failure(self):
        summary = {'status': 'partial', 'modules': {
            'weread': {'status': 'partial', 'gaps': ['known missing identity']},
            'fanqie': {'status': 'no_resolved_projects'}}}
        self.assertEqual(daily_run.pipeline_exit_code(summary), 0)

    def test_failed_blocked_and_review_are_nonzero(self):
        for status, expected in [('failed', 1), ('blocked', 1), ('source_review_required', 2)]:
            with self.subTest(status=status):
                self.assertEqual(daily_run.pipeline_exit_code({
                    'status': 'partial', 'modules': {'tencent': {'status': status}}}), expected)

    def test_source_review_keeps_other_sources_running_and_records_attention(self):
        calls = []
        def platform(_):
            calls.append('called')
            return {'status': 'partial'}
        with TemporaryDirectory() as directory, patch.object(daily_run, 'ROOT', Path(directory)):
            summary = daily_run.run_pipeline(SimpleNamespace(run_kind='local', shadow=True, dry_run=True),
                lambda _: {'status': 'partial', 'source_changes': ['edited row']}, platform, platform)
        self.assertEqual(len(calls), 2)
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['process_exit_code'], 2)
        self.assertEqual(summary['operational_status'], 'requires_review')

    def test_main_returns_scheduler_failure_without_running_real_sources(self):
        fake_module = SimpleNamespace(run=lambda _: None)
        with patch.dict('sys.modules', {'tencent_daily': fake_module, 'fanqie_daily': fake_module}), \
                patch('scripts.shared_runtime.session', return_value=nullcontext()), \
                patch('scripts.runner_policy.enforce_writer'), \
                patch('sys.argv', ['daily_run.py', '--shadow']), \
                patch('sys.stdout', new_callable=io.StringIO), \
                patch.object(daily_run, 'process_lock', return_value=nullcontext()), \
                patch.object(daily_run, 'run_project_initialization', return_value={'status': 'completed'}), \
                patch.object(daily_run, 'run_project_notifications', return_value={'status': 'completed'}), \
                patch.object(daily_run.Base, 'call', side_effect=AssertionError('No real Feishu transport in unit tests')), \
                patch.object(daily_run, 'run_notification_health', return_value={'status': 'ok'}), \
                patch.object(daily_run, 'atomic_json'), \
                patch.object(daily_run, 'run_pipeline', return_value={
                    'status': 'failed', 'modules': {'weread': {'status': 'failed'}}}):
            self.assertEqual(daily_run.main(), 1)


if __name__ == '__main__':
    unittest.main()
