import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import notification_digest as digest
from scripts.daily_run import atomic_json


DAY = '2026-11-06'


def row(key, kind='board_enter'):
    return {'事件唯一键': key, '事件类型': kind, '发生日期': DAY + ' 00:00',
            '通知标题': '作品' + key, '通知正文': '《作品' + key + '》\n变化及留档链接',
            '关联项目': [{'id': 'book-' + key}], '收件人': [{'id': 'owner'}],
            '规则ID': 'v1:' + kind, '判定证据': '{}', '采集批次': 'source-batch'}


class FakeBase:
    def __init__(self):
        self.rows = []
        self.calls = 0

    def read(self, *_):
        return self.rows

    def write(self, api, rows, day):
        assert api is self
        self.calls += 1
        self.rows.extend(rows)
        return len(rows)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        atomic_json(self.root / 'config/notification-grouping.json', {'enabled': True})
        self.api = FakeBase()
        self.pipeline = {'date': DAY, 'modules': {'weread': {'status': 'completed'}}}
        self.args = SimpleNamespace(dry_run=False)
        self.runtime = {'notifications_enabled': True}
        patcher = patch.object(digest, 'helpers', return_value=(atomic_json, lambda: DAY, lambda *_: None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def flush(self, writer=None):
        return digest.flush(self.args, self.pipeline, self.runtime, self.root, self.api, writer or self.api.write)

    def stage(self, rows):
        return digest.stage(self.api, rows, DAY, self.root)

    def test_groups_all_four_categories_after_staging_and_replay_is_empty(self):
        kinds = ['board_enter', 'board_enter', 'board_exit', 'new_project_entry',
                 'marketing_expense_changed', 'recommendation_up', 'recommendation_down']
        self.stage([row(str(i), kind) for i, kind in enumerate(kinds)])
        self.assertEqual(self.api.calls, 0)
        result = self.flush()
        self.assertEqual(result['messages_created'], 4)
        self.assertEqual({r['事件类型'] for r in self.api.rows}, {'上榜汇总', '下榜汇总', '营销汇总', '推荐值90%汇总'})
        self.assertEqual(len(digest.consumed_ids(self.api.rows)), 11)
        self.assertEqual(self.flush()['write_count'], 0)
        self.assertEqual(self.api.calls, 1)

    def test_no_events_no_messages(self):
        self.assertEqual(self.flush()['write_count'], 0)
        self.assertEqual(self.api.calls, 0)

    def test_existing_individual_events_never_replayed(self):
        self.api.rows = [row('old')]
        self.stage([row('old'), row('new')])
        self.flush()
        self.assertEqual(json.loads(self.api.rows[-1]['判定证据'])['member_event_ids'], ['new'])

    def test_unknown_write_requires_reconciliation_not_retry(self):
        self.stage([row('a')])
        def interrupted(*_):
            raise RuntimeError('network lost after request')
        with self.assertRaises(RuntimeError):
            self.flush(interrupted)
        self.assertEqual(self.flush()['status'], 'requires_review')
        self.assertEqual(self.api.calls, 0)

    def test_dry_run_and_shadow_do_not_consume_or_send(self):
        self.stage([row('a')])
        self.args.dry_run = True
        self.assertEqual(self.flush()['planned_messages'], 1)
        self.args.dry_run = False
        self.runtime['notifications_enabled'] = False
        self.assertEqual(self.flush()['status'], 'shadow')
        self.assertFalse((self.root / 'runs' / DAY / 'notification-digest.apply-state.json').exists())
        self.assertEqual(self.api.calls, 0)

    def test_failed_source_delays_groups_until_resolved(self):
        self.stage([row('a')])
        self.pipeline['modules']['tencent'] = {'status': 'source_review_required'}
        self.assertEqual(self.flush()['status'], 'requires_review')
        self.pipeline['modules']['tencent']['status'] = 'completed'
        self.assertEqual(self.flush()['messages_created'], 1)

    def test_late_event_preserved_without_second_message_for_category(self):
        self.stage([row('a')])
        self.flush()
        self.stage([row('b')])
        self.assertEqual(self.flush()['status'], 'requires_review')
        self.assertEqual(self.api.calls, 1)
        queue = digest.load(self.root / 'runs' / DAY / 'notification-digest-queue.json', {})
        self.assertIn('b', queue['events'])

    def test_old_unsent_queue_not_replayed(self):
        atomic_json(self.root / 'runs/2026-11-05/notification-digest-queue.json', {'events': {'old': row('old')}})
        self.assertEqual(self.flush()['status'], 'requires_review')
        self.assertEqual(self.api.calls, 0)

    def test_staging_rejects_wrong_date_changed_event_and_unknown_type(self):
        self.stage([row('a')])
        for bad in ({**row('a'), '通知正文': 'changed'}, {**row('b'), '发生日期': '2026-11-05'}, row('b', 'unknown')):
            with self.assertRaises(ValueError):
                self.stage([bad])

    def test_mixed_recipients_and_oversized_message_fail_closed(self):
        with self.assertRaises(ValueError):
            digest.build([row('a'), {**row('b'), '收件人': [{'id': 'other'}]}], DAY)
        with self.assertRaises(ValueError):
            digest.build([{**row('a'), '通知正文': '长' * 9000}], DAY)

    def test_other_milestone_rule_is_preserved(self):
        self.stage([row('a', 'fanqie_milestone')])
        self.flush()
        self.assertEqual(self.api.rows[0]['事件类型'], '番茄阅读里程碑汇总')

    def test_public_writer_routes_to_queue_without_transport(self):
        from scripts import daily_run
        with patch.object(daily_run, 'ROOT', self.root), patch.object(daily_run, 'deliver_event_rows', side_effect=AssertionError('No individual send')):
            self.assertEqual(daily_run.create_event_rows(self.api, [row('a')], DAY), 0)
        self.assertEqual(self.api.calls, 0)
        self.assertIn('a', digest.load(self.root / 'runs' / DAY / 'notification-digest-queue.json', {})['events'])

    def test_format_no_none_or_markdown_noise_and_keeps_original_evidence(self):
        source = {**row('a', 'board_exit'), '通知正文': '书名（宣传）｜下榜\n本次名次：None\n作品：[链接](https://example.com/book)'}
        group = digest.build([source], DAY)[0]
        self.assertIn('《书名》｜下榜', group['通知正文'])
        self.assertIn('本次：已下榜', group['通知正文'])
        self.assertNotIn('None', group['通知正文'])
        self.assertIn('作品：https://example.com/book', group['通知正文'])
        self.assertEqual(json.loads(group['判定证据'])['events'][0], source)


if __name__ == '__main__':
    unittest.main()
