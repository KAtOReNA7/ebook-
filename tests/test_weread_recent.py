import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.collect_weread_recent import collect, validate_recent


class RecentListTests(unittest.TestCase):
    def setUp(self):
        self.provider = {'name': 'Example publisher', 'user_vid': '123', 'copyright_id': 12}
        self.evidence = {'http_status': 200, 'observed_at': '2026-11-05T14:00:00+08:00'}
        self.row = {'bookInfo': {'bookId': '101', 'title': 'Example', 'author': 'Author',
                                'cpid': 12, 'format': 'epub', 'type': 0,
                                'newRating': 0, 'newRatingCount': 0},
                    'meta': {'type': 0, 'updateTime': 1789459047, 'todayCount': 5}}

    def validate(self, rows, total=None):
        return validate_recent({'data': rows, 'totalCount': len(rows) if total is None else total},
                               self.provider, self.evidence)

    def test_recent_list_keeps_older_months_and_unknown_metrics(self):
        result = self.validate([self.row])
        self.assertEqual(result['status'], 'complete')
        book = result['books'][0]
        self.assertTrue(book['recent_list_time'].startswith('2026-09-15'))
        self.assertIsNone(book['platform_listing_time'])
        self.assertEqual(book['reviewer_count'], 0)
        self.assertIsNone(book['recommendation_percent'])
        self.assertIsNone(book['cumulative_reader_count'])

    def test_duplicate_and_truncation_are_not_complete(self):
        self.assertEqual(self.validate([self.row, self.row])['status'], 'incomplete')
        self.assertEqual(self.validate([self.row], total=43)['status'], 'incomplete')

    def test_bad_media_identity_or_date_stays_excluded(self):
        for area, key, value in [('bookInfo', 'format', 'audio'), ('bookInfo', 'cpid', 99),
                                 ('bookInfo', 'type', True), ('meta', 'type', 3),
                                 ('meta', 'updateTime', None)]:
            with self.subTest(key=key, value=value):
                row = copy.deepcopy(self.row); row[area][key] = value
                result = self.validate([row])
                self.assertEqual(result['status'], 'incomplete')
                self.assertEqual(result['books'], [])

    def test_source_errors_never_become_zero_books(self):
        for body, status in [({'errcode': -2010}, 401), ({'errcode': -2012}, 200), ({}, 200)]:
            result = validate_recent(body, self.provider, dict(self.evidence, http_status=status))
            self.assertNotEqual(result['status'], 'complete')
            self.assertIsNone(result['total_count'])
        self.assertEqual(self.validate([])['status'], 'complete')

    def test_independent_failure_preserves_success_without_secrets(self):
        other = dict(self.provider, name='Other')
        def fetch(provider, session):
            if provider['name'] == 'Other':
                raise RuntimeError('secret-token-should-not-be-logged')
            return {'totalCount': 1, 'data': [self.row]}, self.evidence
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)/'observation'
            result = collect([self.provider, other], {}, out, fetch=fetch)
            self.assertFalse(result['complete'])
            self.assertEqual(result['providers'][0]['status'], 'complete')
            self.assertEqual(result['base_writes'], 0)
            self.assertNotIn('secret-token', (out/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(len(json.loads((out/'provider-0.json').read_text(encoding='utf-8'))['books']), 1)
            with self.assertRaises(ValueError):
                collect([self.provider], {}, out, fetch=fetch)


if __name__ == '__main__':
    unittest.main()
