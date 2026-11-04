from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from scripts import daily_run
from scripts.daily_run import (atomic_json, collect_frozen, create_event_rows, event_candidates,
                               load_snapshot, process_lock, shanghai_day, tracked_projects)


class DailyTests(unittest.TestCase):
    def test_notifications_fail_closed_without_explicit_live_config(self):
        with TemporaryDirectory() as directory, patch.object(daily_run, 'ROOT', Path(directory)):
            self.assertFalse(daily_run.notification_runtime()['notifications_enabled'])
            config = Path(directory) / 'config' / 'runtime.json'
            atomic_json(config, {'mode': 'shadow', 'notifications_enabled': True})
            self.assertFalse(daily_run.notification_runtime()['notifications_enabled'])
            atomic_json(config, {'mode': 'live', 'notifications_enabled': True})
            self.assertTrue(daily_run.notification_runtime()['notifications_enabled'])
            self.assertFalse(daily_run.notification_runtime(True)['notifications_enabled'])

    def test_shanghai_calendar(self):
        self.assertEqual(shanghai_day(datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)), '2026-11-03')
        with self.assertRaises(ValueError):
            shanghai_day(datetime(2026, 11, 3))

    def test_departed_book_continues_through_day_29(self):
        project = {'作品唯一键': 'weread:a', '平台': ['微信读书'], '平台作品ID': 'a',
                   '监测范围': ['仅榜单样本'], '首次上榜采集日': '2026-11-03T00:00:00+08:00',
                   '作品主页': '[作品](https://weread.qq.com/web/reader/a)'}
        self.assertEqual(len(tracked_projects([project], '2026-12-02')[0]), 1)
        self.assertFalse(tracked_projects([project], '2026-12-03')[0])
        project['监测范围'] = ['营销项目']
        self.assertEqual(len(tracked_projects([project], '2026-12-03')[0]), 1)

    def test_finished_snapshot_is_frozen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'weread.json'
            snapshot = {'schema_version': 'weread.public.v1', 'finished_at': 'done',
                        'collection_date': '2026-11-03', 'books': [], 'charts': []}
            atomic_json(path, snapshot)
            before = path.read_bytes()
            data, reused = collect_frozen(Path(directory), '2026-11-03', [], 'scheduled')
            self.assertTrue(reused)
            self.assertEqual(data, snapshot)
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(ValueError):
                load_snapshot(path, '2026-11-04')

    def test_lock_is_exclusive_and_released(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'run.lock'
            with process_lock(path):
                with self.assertRaises(OSError):
                    with process_lock(path):
                        pass
            with process_lock(path):
                pass

    def test_event_outbox_replay_accepts_advanced_state(self):
        row = {'事件唯一键': 'e', '事件类型': 'board_enter', '关联项目': [{'id': 'r'}],
               '规则ID': '1', '判定证据': '{}', '通知标题': 'title', '通知正文': 'body',
               '收件人': [{'id': daily_run.RECIPIENTS[0]}], '采集批次': 'batch', '通知状态': ['待发送']}

        class FakeBase:
            def __init__(self):
                self.rows = []
                self.calls = 0

            def read(self, *_):
                return self.rows

            def call(self, command, table, payload):
                self.calls += 1
                self.rows.extend({**r, '通知状态': ['已发送']} for r in payload['create_records'])

        api = FakeBase()
        # This test exercises replay against a fake transport, independently
        # of the developer machine's real owner identity/configuration.
        with patch('scripts.notification_policy.OWNER_CONFIGURED', True):
            self.assertEqual(create_event_rows(api, [row], '2026-11-03'), 1)
            self.assertEqual(create_event_rows(api, [row], '2026-11-03'), 0)
        self.assertEqual(api.calls, 1)
        self.assertEqual(api.rows[0]['通知状态'], ['已发送'])

    def test_first_collection_does_not_emit(self):
        current = {'collection_date': '2026-11-03', 'books': [], 'charts': []}
        events, pending = event_candidates(None, current, [])
        self.assertFalse(events)
        self.assertFalse(pending)

    def test_only_marketing_projects_receive_recommendation_crossings(self):
        project = {'作品唯一键': 'weread:123', '平台': ['微信读书'], '平台作品ID': '123',
                   '监测范围': ['旧系统延续'], '旧系统首次监测日': '2026-10-01 00:00'}
        def snapshot(day, value):
            return {'collection_date': day, 'charts': [], 'books': [
                {'book_id': '123', 'status': 'ok', 'recommendation_percent': value}]}
        for scope, expected in [('旧系统延续', 0), ('仅榜单样本', 0), ('营销项目', 1)]:
            row = {**project, '监测范围': [scope]}
            with self.subTest(scope=scope):
                events, _ = event_candidates(snapshot('2026-10-29', 89), snapshot('2026-10-30', 90), [row])
                self.assertEqual(len(events), expected)
        marketing = {**project, '监测范围': ['营销项目']}
        events, _ = event_candidates(None, snapshot('2026-10-01', 91), [marketing])
        self.assertEqual(events, [])
        events, _ = event_candidates(snapshot('2026-10-29', 90), snapshot('2026-10-30', 89.9), [marketing])
        self.assertEqual(events[0]['kind'], 'recommendation_down')
        self.assertEqual(project['旧系统首次监测日'], '2026-10-01 00:00')
        self.assertFalse(daily_run.is_marketing(project))

    def test_old_metadata_does_not_expand_project_board_alert_scope(self):
        def snapshot(day, ids):
            return {'collection_date': day, 'books': [], 'charts': [
                {'chart_id': 'newbook', 'complete_top50': True, 'errors': [],
                 'records': [{'book_id': str(bid), 'rank': i + 1} for i, bid in enumerate(ids)]}]}
        project = {'作品唯一键': 'weread:1', '平台': ['微信读书'], '监测范围': ['旧系统延续'],
                   '旧系统首次监测日': '2026-11-01 00:00', '磨铁版权确认': ['待核实']}
        events, pending = event_candidates(snapshot('2026-11-03', range(2, 52)),
                                           snapshot('2026-11-04', range(1, 51)), [project])
        self.assertEqual(events, [])
        self.assertEqual(pending, [])


if __name__ == '__main__':
    unittest.main()
