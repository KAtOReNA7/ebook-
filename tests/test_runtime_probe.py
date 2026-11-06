from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.runtime_probe import begin, check


class ProbeTests(unittest.TestCase):
    def test_marker_must_be_unchanged_and_is_not_migration_acceptance(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / 'probe.json'
            created = begin(marker)
            result = check(marker, created['expected_sha256'])
            self.assertFalse(result['business_migration_ready'])
            self.assertTrue(result['requires_independent_task_and_schedule_evidence'])
            self.assertEqual(result['base_writes'], 0)
            with self.assertRaises(FileExistsError):
                begin(marker)
            marker.write_text('{}')
            with self.assertRaises(ValueError):
                check(marker, created['expected_sha256'])

    def test_lost_marker_is_not_recreated(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory) / 'missing.json'
            with self.assertRaises(FileNotFoundError):
                check(marker, 'old-digest')
            self.assertFalse(marker.exists())
