import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime
from unittest.mock import patch
from scripts import daily_run


class CutoverDateTests(unittest.TestCase):
    def test_no_shadow_history_delivery_before_explicit_business_start(self):
        with TemporaryDirectory() as directory, patch.object(daily_run, 'ROOT', Path(directory)):
            config = Path(directory) / 'config'
            config.mkdir()
            (config / 'runtime.json').write_text(json.dumps({'mode': 'live', 'notifications_enabled': True,
                                                           'effective_from': '2026-11-04'}), encoding='utf-8')
            with patch.object(daily_run, 'now', return_value=datetime.fromisoformat('2026-11-03T23:59:59+08:00')):
                self.assertFalse(daily_run.notification_runtime()['notifications_enabled'])
            with patch.object(daily_run, 'now', return_value=datetime.fromisoformat('2026-11-04T10:00:00+08:00')):
                self.assertTrue(daily_run.notification_runtime()['notifications_enabled'])
                self.assertFalse(daily_run.notification_runtime(True)['notifications_enabled'])


if __name__ == '__main__':
    unittest.main()
