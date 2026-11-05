import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import feishu_io


class CompleteReadTests(unittest.TestCase):
    def read_pages(self, pages, conditions=None):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            class PageBase(feishu_io.Base):
                def __init__(self):
                    self.receipt_dir = root / 'receipts'
                    self.receipt_dir.mkdir()

                def call(self, command, table=None, payload=None, **flags):
                    calls.append(flags)
                    rows, metadata = pages[len(calls) - 1]
                    (root / flags['output']).write_text(
                        '\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
                    return metadata

            with patch.object(feishu_io, 'ROOT', root):
                rows = PageBase().read('项目总表', conditions)
            return rows, calls

    def test_more_than_2000_rows_and_filter_are_read_completely(self):
        first = [{'record_id': str(i)} for i in range(2000)]
        second = [{'record_id': '2000'}]
        rows, calls = self.read_pages([
            (first, {'has_more': True, 'records_count': 2000}),
            (second, {'has_more': False, 'records_count': 1}),
        ], [['平台', 'intersects', ['微信读书']]])
        self.assertEqual(len(rows), 2001)
        self.assertEqual([call['offset'] for call in calls], [0, 2000])
        self.assertEqual(calls[0]['filter_json'], calls[1]['filter_json'])
        self.assertNotEqual(calls[0]['output'], calls[1]['output'])

    def test_missing_has_more_cannot_certify_a_full_read(self):
        with self.assertRaisesRegex(RuntimeError, 'pagination metadata'):
            self.read_pages([([{'record_id': 'a'}], {'records_count': 1})])

    def test_duplicate_page_is_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, 'repeated record ID'):
            self.read_pages([
                ([{'record_id': 'a'}], {'has_more': True, 'records_count': 1}),
                ([{'record_id': 'a'}], {'has_more': False, 'records_count': 1}),
            ])

    def test_empty_continuation_and_count_mismatch_fail_closed(self):
        for rows, reply in [([], {'has_more': True, 'records_count': 0}),
                            ([{'record_id': 'a'}], {'has_more': False, 'records_count': 2})]:
            with self.subTest(reply=reply), self.assertRaises(RuntimeError):
                self.read_pages([(rows, reply)])

    def test_terminal_empty_page_is_valid(self):
        self.assertEqual(self.read_pages([([], {'has_more': False, 'records_count': 0})])[0], [])


class AcknowledgedReadbackTests(unittest.TestCase):
    def fake(self, pages, write_failure=False):
        class DelayedBase(feishu_io.Base):
            def __init__(self):
                self.presentation_enabled = False
                self.manifest = {'tables': {'test': {'field_readback': {'fields': [
                    {'name': 'key', 'type': 'text'}, {'name': 'value', 'type': 'text'}]}}}}
                self.read_calls = 0
                self.write_calls = 0

            def read(self, *_):
                page = pages[self.read_calls]
                self.read_calls += 1
                return page

            def call(self, *_):
                self.write_calls += 1
                if write_failure:
                    raise RuntimeError('unknown write outcome')
                return {'ok': True}
        return DelayedBase()

    def test_delayed_acknowledged_create_is_only_written_once(self):
        row = {'key': 'a', 'value': 'new'}
        api = self.fake([[], [], [{'record_id': 'r', **row}]])
        with patch.object(feishu_io.time, 'sleep') as wait:
            self.assertEqual(api.ensure('test', 'key', [row])['a']['record_id'], 'r')
        self.assertEqual(api.write_calls, 1)
        self.assertEqual(api.read_calls, 3)
        wait.assert_called_once_with(1)

    def test_persistent_missing_or_different_readback_stops_without_rewriting(self):
        for page, message in [([], 'missing readback'),
                              ([{'record_id': 'r', 'key': 'a', 'value': 'stale'}], 'readback differs')]:
            api = self.fake([[], page, page, page])
            with self.subTest(page=page), patch.object(feishu_io.time, 'sleep'), self.assertRaisesRegex(RuntimeError, message):
                api.ensure('test', 'key', [{'key': 'a', 'value': 'new'}])
            self.assertEqual(api.write_calls, 1)
            self.assertEqual(api.read_calls, 4)

    def test_unknown_write_is_not_retried_or_accepted(self):
        api = self.fake([[]], write_failure=True)
        with patch.object(feishu_io.time, 'sleep') as wait, self.assertRaisesRegex(RuntimeError, 'unknown write'):
            api.ensure('test', 'key', [{'key': 'a', 'value': 'new'}])
        self.assertEqual(api.write_calls, 1)
        self.assertEqual(api.read_calls, 1)
        wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
