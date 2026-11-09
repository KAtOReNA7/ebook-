import unittest
from unittest.mock import Mock

from scripts.sync_weread import sync, validate_recent_tracking_dates


class PublisherTrackingConflictTests(unittest.TestCase):
    def test_stale_device_stops_entire_sync_before_any_write(self):
        api = Mock()
        api.read.return_value = [{'作品唯一键': 'weread:2',
            '首次进入最近上架日': '2026-11-07T00:00:00.000+08:00',
            '版权方追踪截止日': '2026-12-06T00:00:00.000+08:00'}]
        data = {'schema_version': 'weread.public.v1', 'collection_date': '2026-11-09',
                'started_at': '2026-11-09T10:00:00+08:00',
                'finished_at': '2026-11-09T10:01:00+08:00',
                'charts': [], 'books': [{'book_id': '1'}, {'book_id': '2'}]}
        tracks = {i: {'start_date': '2026-11-09', 'end_date': '2026-12-08'} for i in ('1', '2')}
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            sync(data, 'unused.json', api=api, recent_tracks=tracks)
        self.assertEqual([call[0] for call in api.mock_calls], ['read'])

    def test_matching_remote_dates_and_new_scope_are_allowed(self):
        tracks = {'2': {'start_date': '2026-11-07', 'end_date': '2026-12-06'}}
        for row in ({}, {'首次进入最近上架日': '2026-11-07T00:00:00.000+08:00',
                         '版权方追踪截止日': '2026-12-06 00:00'}):
            validate_recent_tracking_dates({'weread:2': row}, [{'book_id': 2}], tracks)

    def test_partial_dates_and_changed_deadline_require_review(self):
        tracks = {'2': {'start_date': '2026-11-07', 'end_date': '2026-12-06'}}
        for end in (None, '2026-12-08 00:00'):
            with self.assertRaises(ValueError):
                validate_recent_tracking_dates({'weread:2': {
                    '首次进入最近上架日': '2026-11-07 00:00', '版权方追踪截止日': end}},
                    [{'book_id': 2}], tracks)


if __name__ == '__main__':
    unittest.main()
