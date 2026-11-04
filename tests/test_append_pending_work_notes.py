from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from scripts import append_pending_work_notes as notes


def fixture():
    project = {'record_id': 'record-1', '作品唯一键': 'pending:weread:audio:source',
               '书名': '源作品', '监测范围': ['营销项目'], '平台作品ID': '',
               '证据与备注': '原始备注\n链接 https://example.test/archive',
               '累计营销费用': 180, '邀评总人数': 30, '营销服务明细': [{'id': 'service-1'}]}
    entry = {'source_title': '源作品', 'medium': 'audio', 'status': '同名候选待核对作者',
             'explanation': '按书名和原著作者确认',
             'candidates': [{'book_id': '1', 'title': '源作品', 'author': '作者甲',
                             'detail_url': 'https://weread.qq.com/web/reader/1'}],
             'source_rows': [{'source_coordinate_key': 'BB08J2:row:1',
                              'archive_links': [{'url': 'https://docs.qq.com/sheet/source'}]}],
             'evidence_path': 'evidence.json', 'official_evidence_path': 'official.json'}
    return [project], [entry]


class PendingWorkNotesTests(unittest.TestCase):
    def test_append_preserves_original_and_is_idempotent(self):
        projects, entries = fixture()
        plan = notes.build_plan(projects, entries)
        self.assertEqual(set(plan['updates']['record-1']), {'证据与备注'})
        self.assertTrue(plan['updates']['record-1']['证据与备注'].startswith(projects[0]['证据与备注'] + '\n'))
        after = deepcopy(projects)
        after[0].update(plan['updates']['record-1'])
        notes.assert_readback(plan, after)
        repeat = notes.build_plan(after, entries)
        self.assertEqual(repeat['updates'], {})
        self.assertEqual(repeat['decisions'][0]['status'], 'skip_already_appended')
        self.assertEqual(projects[0]['累计营销费用'], 180)

    def test_archived_matched_or_absent_project_is_not_annotated(self):
        for mode in ('archived', 'matched', 'key-resolved', 'absent'):
            projects, entries = fixture()
            if mode == 'archived':
                projects[0]['监测范围'] = ['别名留档']
            elif mode == 'matched':
                projects[0]['平台作品ID'] = '99'
            elif mode == 'key-resolved':
                projects[0]['作品唯一键'] = 'weread:99'
            else:
                projects = []
            with self.subTest(mode=mode):
                self.assertEqual(notes.build_plan(projects, entries)['updates'], {})

    def test_readback_rejects_any_protected_field_change_or_record_change(self):
        projects, entries = fixture()
        plan = notes.build_plan(projects, entries)
        for field, value in [('累计营销费用', 181), ('邀评总人数', 29),
                             ('作品唯一键', 'weread:123'), ('监测范围', ['别名留档']),
                             ('营销服务明细', [{'id': 'wrong'}])]:
            after = deepcopy(projects)
            after[0].update(plan['updates']['record-1'])
            after[0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'protected field'):
                notes.assert_readback(plan, after)
        with self.assertRaisesRegex(RuntimeError, 'record IDs'):
            notes.assert_readback(plan, [])

    def test_marker_collision_is_not_overwritten(self):
        projects, entries = fixture()
        marker, _ = notes.note_block(entries[0])
        projects[0]['证据与备注'] += '\n' + marker + 'different text'
        with self.assertRaisesRegex(ValueError, 'different content'):
            notes.build_plan(projects, entries)

    def test_stale_preimage_prevents_writes_and_timeout_remains_unknown(self):
        projects, entries = fixture()
        approved = {'evidence_files': [], 'plan': notes.build_plan(projects, entries)}
        with TemporaryDirectory() as temp:
            class FakeAPI:
                receipt_dir = Path(temp)
                calls = 0
                records = deepcopy(projects)

                def read(self, table):
                    return deepcopy(self.records)

                def call(self, *args):
                    self.calls += 1
                    raise TimeoutError('Unknown transport outcome')

            api = FakeAPI()
            with patch.object(notes, 'evidence_files', return_value=[]), patch.object(notes, 'entries_from_evidence', return_value=entries):
                api.records[0]['累计营销费用'] = 181
                with self.assertRaisesRegex(RuntimeError, 'preimage'):
                    notes.apply_plan(api, approved)
                self.assertEqual(api.calls, 0)
                self.assertFalse((Path(temp) / 'link-state.json').exists())
                api.records = deepcopy(projects)
                with self.assertRaises(TimeoutError):
                    notes.apply_plan(api, approved)
                self.assertEqual(api.calls, 1)
                state = json.loads((Path(temp) / 'link-state.json').read_text(encoding='utf-8'))
                self.assertEqual(state['phase'], 'unknown_until_full_readback')


if __name__ == '__main__':
    unittest.main()
