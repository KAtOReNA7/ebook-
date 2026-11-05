import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import fanqie_rising_daily as daily
from scripts import daily_run
from tests.test_fanqie_rising import fixtures


def freeze(root, day='2026-11-05', replace_first=False, finished=False):
    path = root/'runs'/day/'fanqie-rising/snapshot.json'
    directory, ranking = fixtures(100)
    if replace_first:
        ranking['data']['cell_view']['book_data'][0]['book_id'] = '9234567890123456789'
    stamp = day+'T10:03:00+08:00'
    data = {'schema_version': 'fanqie.publication-rising.v1', 'started_at': stamp,
            'finished_at': stamp, **daily.normalize(ranking, daily.discover(directory))}
    for name, payload, endpoint in [('directory', directory, daily.DIRECTORY_PATH),
                                     ('ranking', ranking, daily.RANK_PATH)]:
        daily.atomic_json(path.with_name(name+'.json'), payload)
        data[name+'_source'] = {'http_status': 200, 'observed_at': stamp,
            'url': daily.HOST+endpoint+'?fixture=1',
            'response_sha256': hashlib.sha256(path.with_name(name+'.json').read_bytes()).hexdigest()}
    daily.atomic_json(path, data)
    if finished:
        daily.atomic_json(path.parent.parent/'fanqie-rising-state.json', {'phase': 'finished', 'summary': {}})
    return path, data


def project(bid='9234567890123456789', scope='营销项目', medium='电子书'):
    return {'record_id': 'synthetic-record', '作品唯一键': 'fanqie:'+bid, '平台作品ID': bid,
            '作品形式': medium, '平台': ['番茄小说'], '监测范围': [scope], '书名': '合成营销书'}


class FakeBase:
    def __init__(self, fail=False):
        self.changes = 0
        self.fail = fail
        self.tables = []

    def read(self, table):
        return [project()] if table == '项目总表' else []

    def ensure(self, table, key, rows, *args):
        self.tables.append(table)
        if self.fail:
            raise RuntimeError('unknown write outcome')
        self.changes += len(rows)


class RisingDailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = SimpleNamespace(dry_run=False, shadow=True, run_kind='local')
        daily.atomic_json(self.root/'config/fanqie-ranking.json', {'enabled': True, 'status': 'web_daily'})
        self.clock = lambda: datetime.fromisoformat('2026-11-05T11:00:00+08:00')

    def run_daily(self, api=None):
        return daily.run(self.args, self.root, api or FakeBase(), self.clock)

    def test_pause_causes_no_network_or_base_access(self):
        daily.atomic_json(self.root/'config/fanqie-ranking.json', {'enabled': False, 'status': 'user_paused'})
        with patch.object(daily, 'collect') as collect, patch.object(daily, 'Base') as base:
            self.assertEqual(self.run_daily()['status'], 'user_paused')
            collect.assert_not_called()
            base.assert_not_called()

    def test_conflicting_enable_pause_rejected(self):
        daily.atomic_json(self.root/'config/fanqie-ranking.json', {'enabled': True, 'status': 'user_paused'})
        with self.assertRaises(ValueError):
            self.run_daily()

    def test_source_hash_and_normalized_ranks_checked(self):
        path, data = freeze(self.root)
        self.assertEqual(daily.verified_snapshot(path, '2026-11-05')['count'], 100)
        data['books'][0]['rank'] = 2
        daily.atomic_json(path, data)
        with self.assertRaises(ValueError):
            daily.verified_snapshot(path, '2026-11-05')
        path, _ = freeze(self.root)
        path.with_name('ranking.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            daily.verified_snapshot(path, '2026-11-05')

    def test_incomplete_and_wrong_day_rejected(self):
        path, data = freeze(self.root)
        with self.assertRaises(ValueError):
            daily.verified_snapshot(path, '2026-11-06')
        data['complete'] = False
        daily.atomic_json(path, data)
        with self.assertRaises(ValueError):
            self.run_daily()

    def test_dry_run_does_not_write_or_commit_baseline(self):
        freeze(self.root)
        self.args.dry_run = True
        api = FakeBase()
        result = self.run_daily(api)
        self.assertTrue(result['baseline'])
        self.assertEqual(api.tables, [])
        self.assertFalse((self.root/'runs/2026-11-05/fanqie-rising-state.json').exists())

    def test_entry_only_marketing_no_first_day_flood_or_exit(self):
        _, old = freeze(self.root, '2026-11-04')
        _, current = freeze(self.root, replace_first=True)
        scope = daily.marketing_index([project()])
        self.assertEqual(len(daily.entry_events(old, current, scope)), 1)
        self.assertEqual(daily.entry_events(None, current, scope), [])
        self.assertEqual(daily.entry_events(old, current, {}), [])
        for p in [project(scope='仅榜单样本'), project(medium='有声书')]:
            self.assertEqual(daily.marketing_index([p]), {})
        with self.assertRaises(ValueError):
            daily.marketing_index([project(), project()])
        old['started_at'] = '2026-11-03T10:00:00+08:00'
        self.assertEqual(daily.entry_events(old, current, scope), [])

    def test_commit_readback_then_replay_without_writes(self):
        freeze(self.root, '2026-11-04', finished=True)
        freeze(self.root, replace_first=True)
        api = FakeBase()
        result = self.run_daily(api)
        self.assertEqual(result['candidate_events'], 1)
        self.assertFalse(result['notifications_enabled'])
        self.assertEqual(api.tables, ['榜单快照', '运行日志'])
        self.assertEqual(result['readback'], 'passed')
        result = self.run_daily(FakeBase(fail=True))
        self.assertTrue(result['replayed'])
        self.assertEqual(result['write_count'], 0)

    def test_unknown_write_blocks_next_day_and_initialization(self):
        freeze(self.root)
        with self.assertRaises(RuntimeError):
            self.run_daily(FakeBase(fail=True))
        self.clock = lambda: datetime.fromisoformat('2026-11-06T11:00:00+08:00')
        with patch.object(daily, 'collect') as collect:
            self.assertEqual(self.run_daily()['status'], 'requires_review')
            collect.assert_not_called()
        self.assertEqual(len(daily.unknown_guard_paths(self.root)), 1)

    def test_source_failure_preserves_previous_baseline(self):
        path, _ = freeze(self.root, '2026-11-04', finished=True)
        before = path.read_bytes()
        with patch.object(daily, 'collect', side_effect=ValueError('partial')):
            self.assertEqual(self.run_daily()['status'], 'failed')
        self.assertEqual(before, path.read_bytes())
        self.assertFalse((self.root/'runs/2026-11-05/fanqie-rising-state.json').exists())

    def test_rows_keep_rising_value_out_of_reader_fields(self):
        path, data = freeze(self.root, replace_first=True)
        rows = daily.snapshot_rows(data, daily.marketing_index([project()]), path)
        self.assertEqual(len(rows), 100)
        self.assertIn('关联项目', rows[0])
        self.assertNotIn('关联项目', rows[1])
        for row in rows:
            self.assertNotIn('累计阅读人数', row)
            self.assertNotIn('平台榜单日期', row)

    def test_pipeline_executes_ranking_after_details_under_existing_coordinator(self):
        names = []
        def runner(name):
            def run(args):
                names.append(name)
                return {'status': 'completed'}
            return run
        with patch.object(daily_run, 'ROOT', self.root):
            daily_run.run_pipeline(self.args, runner('tencent'), runner('weread'), runner('fanqie'),
                                   {}, runner('publisher'), runner('rising'))
        self.assertEqual(names, ['tencent', 'weread', 'fanqie', 'rising', 'publisher'])

    def test_current_attempt_unknown_write_blocks_later_modules_and_notifications(self):
        def broken(args):
            daily.atomic_json(self.root/'runs/2026-11-05/fanqie-rising-state.json', {'phase': 'requires_review'})
            raise RuntimeError('unknown write')
        with patch.object(daily_run, 'ROOT', self.root):
            with patch('scripts.publisher_recent_daily.run') as publisher:
                result = daily_run.run_pipeline(self.args, lambda a: {}, lambda a: {}, lambda a: {},
                                                {}, publisher, broken)
                publisher.assert_not_called()
            self.assertEqual(result['modules']['publisher_recent']['status'], 'blocked')
            self.assertTrue(result['modules']['project_initialization']['identity_mutation_unknown'])
            self.assertEqual(daily_run.run_project_notifications(self.args, result)['status'], 'requires_review')
            self.assertEqual(daily_run.run_expense_notifications(self.args, result)['events_created'], 0)


if __name__ == '__main__':
    unittest.main()
