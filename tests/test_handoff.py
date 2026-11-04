import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from scripts.local_settings import project_path
from tools.handoff import export_state, inspect_archive, restore_state, safe_name, state_files


def write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class HandoffTests(unittest.TestCase):
    def fixture(self, root):
        evidence = b'{"unaltered_evidence":true}\r\n'
        write(root, 'scripts/daily_run.py', b'# clone marker\n')
        write(root, 'config/runtime.json', b'{"notifications_enabled":true,"production_approved":false,"mode":"owner_only"}')
        write(root, 'config/local-system.json', b'{"owner_open_id":"ou_fixture"}')
        write(root, 'receipts/medium.json', evidence)
        write(root, 'runs/manual-project-registry.json', b'{"seen":["old-record"]}')
        write(root, 'runs/marketing-expense-registry.json', b'{"seen":["old-expense"]}')
        write(root, 'research/something/.env', b'NEVER_EXPORT=credential')
        write(root, 'research/installer.exe', b'not a dependency environment')
        write(root, 'research/.venv/private.json', b'not runtime state')
        write(root, 'receipts/cookies.json', b'NEVER_EXPORT')
        return evidence

    def test_round_trip_preserves_evidence_and_dedup_but_disables_notifications(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            source, target, archive = parent / 'source', parent / 'clone', parent / 'private.zip'
            evidence = self.fixture(source)
            write(target, 'scripts/daily_run.py', b'# clone marker\n')
            exported = export_state(source, archive)
            manifest = inspect_archive(archive)
            self.assertEqual(exported['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
            names = {entry['path'] for entry in manifest['files']}
            self.assertNotIn('research/something/.env', names)
            self.assertNotIn('research/installer.exe', names)
            self.assertNotIn('research/.venv/private.json', names)
            self.assertNotIn('receipts/cookies.json', names)
            restored = restore_state(archive, target)
            self.assertFalse(restored['notifications_enabled'])
            self.assertFalse(restored['scheduler_created'])
            self.assertEqual((target / 'receipts/medium.json').read_bytes(), evidence)
            self.assertEqual((target / 'runs/manual-project-registry.json').read_bytes(), b'{"seen":["old-record"]}')
            runtime = json.loads((target / 'config/runtime.json').read_text())
            self.assertEqual(runtime['mode'], 'shadow')
            self.assertFalse(runtime['production_approved'])
            self.assertEqual(project_path(source / 'receipts/medium.json', target), target / 'receipts/medium.json')
            self.assertTrue(json.loads((source / 'config/runtime.json').read_text())['notifications_enabled'])

    def test_conflicting_restore_is_rejected_before_any_new_file(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            source, target, archive = parent / 'source', parent / 'clone', parent / 'private.zip'
            self.fixture(source)
            write(target, 'scripts/daily_run.py', b'# clone marker\n')
            write(target, 'runs/marketing-expense-registry.json', b'newer state')
            export_state(source, archive)
            with self.assertRaises(FileExistsError):
                restore_state(archive, target)
            self.assertFalse((target / 'config/runtime.json').exists())
            self.assertEqual((target / 'runs/marketing-expense-registry.json').read_bytes(), b'newer state')

    def test_unfinished_guard_blocks_export(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            self.fixture(root)
            write(root, 'receipts/attempt.apply-state.json', b'{"phase":"requires_review"}')
            with self.assertRaises(RuntimeError):
                export_state(root, Path(directory) / 'private.zip')
            self.assertFalse((Path(directory) / 'private.zip').exists())

    def test_archive_paths_cannot_target_code_credentials_or_escape(self):
        for value in ('../private.json', '/receipts/a.json', 'C:/a.json', 'receipts/../../a',
                      'receipts\\a.json', '.codex/auth.json', 'scripts/daily_run.py'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                safe_name(value)

    def test_windows_path_is_rebased_without_changing_payload(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, 'config/workspace-origin.json', b'{"roots":["E:/project/ebook"]}')
            self.assertEqual(project_path(r'e:\project\ebook\receipts\a.json', root), root / 'receipts/a.json')
            with self.assertRaises(ValueError):
                project_path(r'E:\project\ebook\..\..\outside.json', root)

    def test_corruption_is_detected_before_restore(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            self.fixture(parent / 'source')
            export_state(parent / 'source', parent / 'private.zip')
            with zipfile.ZipFile(parent / 'private.zip') as src, zipfile.ZipFile(parent / 'bad.zip', 'w') as dst:
                for item in src.namelist():
                    dst.writestr(item, b'changed' if item == 'receipts/medium.json' else src.read(item))
            with self.assertRaises(ValueError):
                inspect_archive(parent / 'bad.zip')

    def test_different_code_revision_is_rejected(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            self.fixture(parent / 'source')
            write(parent / 'clone', 'scripts/daily_run.py', b'# clone marker\n')
            with patch('tools.handoff.code_commit', return_value='a' * 40):
                export_state(parent / 'source', parent / 'private.zip')
            with patch('tools.handoff.code_commit', return_value='b' * 40), self.assertRaises(ValueError):
                restore_state(parent / 'private.zip', parent / 'clone')
            self.assertFalse((parent / 'clone/config/runtime.json').exists())


if __name__ == '__main__':
    unittest.main()
