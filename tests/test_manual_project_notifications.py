from copy import deepcopy
import unittest
from unittest.mock import patch

from scripts.manual_project_notifications import build_plan, covered_by_tencent, notification_rows
from scripts.notification_policy import OWNER_ID


def row(rid, scope='营销项目', key=None):
    return {'record_id': rid, '作品唯一键': key or 'pending:weread:ebook:' + rid,
            '书名': '书-' + rid, '作品形式': '电子书', '平台': ['微信读书'], '监测范围': [scope]}


class NewProjectNotificationTests(unittest.TestCase):
    def test_incomplete_or_audio_row_waits_until_explicit_ebook_then_notifies_once(self):
        registry = build_plan([], None, '2026-11-03')['next_registry']
        for field, value in [('作品形式', None), ('作品形式', '有声书'), ('平台', [])]:
            with self.subTest(field=field, value=value):
                p = row('new')
                p[field] = value
                pending = build_plan([p], registry, '2026-11-04')
                self.assertEqual(pending['events'], [])
                self.assertNotIn('new', pending['next_registry']['seen_record_ids'])
                ready = build_plan([row('new')], pending['next_registry'], '2026-11-05')
                self.assertEqual(len(ready['events']), 1)
                self.assertEqual(build_plan([row('new')], ready['next_registry'], '2026-11-06')['events'], [])

    def test_baseline_suppresses_history_and_does_not_mutate_input(self):
        projects = [row('a'), row('b', '仅榜单样本'), row('c', '别名留档')]
        before = deepcopy(projects)
        plan = build_plan(projects, None, '2026-11-03')
        self.assertTrue(plan['baseline'])
        self.assertEqual(plan['events'], [])
        self.assertEqual(plan['next_registry']['seen_record_ids'], ['a', 'b', 'c'])
        self.assertEqual(projects, before)

    def test_only_new_explicit_marketing_records_not_source_duplicates(self):
        old = [row('old')]
        previous = build_plan(old, None, '2026-11-03')['next_registry']
        projects = [row('old', key='weread:123'), row('manual'), row('source'),
                    row('board', '仅榜单样本'), row('alias', '别名留档'), row('unclassified', '')]
        covered = covered_by_tencent([{'project_key': row('source')['作品唯一键']}], projects)
        plan = build_plan(projects, previous, '2026-11-04', covered)
        self.assertEqual([e['record_id'] for e in plan['events']], ['manual'])
        self.assertEqual(len(plan['exclusions']), 4)
        with patch('scripts.manual_project_notifications.notification_recipients',return_value=[OWNER_ID]):
            rows = notification_rows(plan, 'test')
        self.assertEqual(rows[0]['关联项目'], [{'id': 'manual'}])
        self.assertEqual(rows[0]['规则ID'], 'v1:project_table.new_record')
        self.assertEqual(rows[0]['收件人'], [{'id': OWNER_ID}])
        self.assertEqual(build_plan(projects, plan['next_registry'], '2026-11-04', covered)['events'], [])

    def test_existing_scope_promotion_and_alias_identity_merge_are_not_new_records(self):
        old = [row('ranked', '仅榜单样本'), row('alias')]
        previous = build_plan(old, None, '2026-11-03')['next_registry']
        after = [row('ranked', key='weread:123'), row('alias', '别名留档')]
        self.assertEqual(build_plan(after, previous, '2026-11-04')['events'], [])

    def test_deleted_then_reappearing_id_is_not_resent_and_event_key_stable(self):
        registry = build_plan([row('a')], None, '2026-11-03')['next_registry']
        missing = build_plan([], registry, '2026-11-04')['next_registry']
        self.assertEqual(build_plan([row('a')], missing, '2026-11-05')['events'], [])
        first = build_plan([row('a'), row('b')], registry, '2026-11-04')['events'][0]
        replay = build_plan([row('a'), row('b')], registry, '2026-11-05')['events'][0]
        self.assertEqual(first['event_id'], replay['event_id'])

    def test_missing_project_key_does_not_guess_identity_or_prevent_new_record_notice(self):
        registry = build_plan([], None, '2026-11-03')['next_registry']
        p = row('manual')
        p.pop('作品唯一键')
        event = build_plan([p], registry, '2026-11-04')['events'][0]
        self.assertIsNone(event['project_key'])
        self.assertEqual(event['record_id'], 'manual')

    def test_broken_registry_duplicate_records_and_unknown_dedup_fail_without_rebaseline(self):
        valid = build_plan([row('a')], None, '2026-11-03')['next_registry']
        for projects, registry, day, covered in [([row('a')], {}, '2026-11-04', []),
                ([row('a'), row('a')], valid, '2026-11-04', []),
                ([row('a')], valid, '2026-11-02', []),
                ([row('a')], valid, '2026-11-04', ['absent'])]:
            with self.subTest(day=day), self.assertRaises(ValueError):
                build_plan(projects, registry, day, covered)
        with self.assertRaises(ValueError):
            covered_by_tencent([{'project_key': 'absent'}], [row('a')])


if __name__ == '__main__':
    unittest.main()
