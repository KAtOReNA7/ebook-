import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from scripts import publisher_recent_daily as module
from scripts import daily_run, sync_weread
from scripts.recent_entry_tracking import observe


class PublisherDailyTests(unittest.TestCase):
    def setup_root(self, root):
        provider = {'name': '测试版权', 'copyright_id': 7}
        snapshot = {'provider': provider['name'], 'status': 'complete', 'total_count': 1,
                    'returned_rows': 1, 'unique_ids': 1,
                    'source': {'observed_at': '2026-11-04T10:00:00+08:00'},
                    'books': [{'book_id': '1', 'copyright_id': 7}]}
        registry, _ = observe(None, [(provider, snapshot)], '2026-11-04')
        for path, data in [('config/publisher-newbooks.json', {'enabled': True, 'providers_file': 'providers.json',
                           'session_file': 'session.dpapi', 'entry_tracking': {'registry': 'runs/registry.json'}}),
                           ('providers.json', [provider]), ('runs/registry.json', registry)]:
            daily_run.atomic_json(root/path, data)
        snapshot.update(total_count=2, returned_rows=2, unique_ids=2)
        snapshot['source']['observed_at'] = '2026-11-05T10:00:00+08:00'
        snapshot['books'].append({'book_id': '2', 'copyright_id': 7, 'title': '新书',
                                  'reader_url': 'https://weread.qq.com/web/reader/example'})
        def collect(_, __, directory):
            daily_run.atomic_json(directory/'provider-0.json', snapshot)
            return {'complete': True}
        return registry, collect

    def exercise(self, dry=False, fail=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            before, collector = self.setup_root(root)
            api = Mock(changes=2)
            now = datetime.fromisoformat('2026-11-05T10:00:00+08:00')
            args = SimpleNamespace(dry_run=dry, run_kind='scheduled')
            metrics = {'finished_at': now.isoformat(), 'books': [], 'detail_errors': []}
            with patch.object(module, 'load_session', return_value={}), patch.object(module, 'collect', collector), \
                 patch.object(module, 'collect_metrics', return_value=metrics) as cm, \
                 patch.object(module, 'ensure_schema') as schema, \
                 patch.object(module, 'sync', side_effect=RuntimeError('unknown write') if fail else None, return_value={}) as sync:
                if fail:
                    with self.assertRaises(RuntimeError): module.run(args, root, api, lambda: now)
                    result = module.run(args, root, api, lambda: now)
                    self.assertEqual(result['status'], 'requires_review')
                    self.assertEqual(sync.call_count, 1)
                else:
                    result = module.run(args, root, api, lambda: now)
                    self.assertEqual(result['new_entries'], 1)
                    self.assertEqual(cm.call_args.args[0][0]['book_id'], '2')
                    if dry:
                        schema.assert_not_called(); sync.assert_not_called()
                    else:
                        replay = module.run(args, root, api, lambda: now)
                        self.assertTrue(replay['reused_finished'])
                        self.assertEqual(replay['base_writes'], 0)
                        self.assertEqual(sync.call_count, 1)
                state = module.read(root/'runs/registry.json')
                if dry or fail: self.assertEqual(state, before)
                else: self.assertEqual(state['tracks']['2']['end_date'], '2026-12-04')

    def test_dry_run_does_not_write_or_consume_entry(self): self.exercise(dry=True)
    def test_commit_after_readback_and_repeat_no_write(self): self.exercise()
    def test_unknown_write_blocks_retry_without_consuming_entry(self): self.exercise(fail=True)

    def test_auth_failure_keeps_registry_and_collects_existing_targets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _ = self.setup_root(root)
            registry['tracks']['2'] = {'book_id': '2', 'start_date': '2026-11-05', 'end_date': '2026-12-04'}
            daily_run.atomic_json(root/'runs/registry.json', registry)
            stamp = datetime.fromisoformat('2026-11-05T10:00:00+08:00')
            with patch.object(module, 'load_session', side_effect=ValueError('expired')), \
                 patch.object(module, 'collect_metrics', return_value={'finished_at':stamp.isoformat(),
                       'books':[], 'detail_errors':[]}) as metrics:
                result = module.run(SimpleNamespace(dry_run=True, run_kind='scheduled'), root, clock=lambda:stamp)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(metrics.call_args.args[0][0]['book_id'], '2')
            self.assertEqual(module.read(root/'runs/registry.json'), registry)

    def test_filter_readback_accepts_server_null_for_unary_condition(self):
        from scripts.configure_readable_views import equivalent_filter
        self.assertTrue(equivalent_filter({'logic':'and','conditions':[['date','non_empty',None]]},
                                          {'logic':'and','conditions':[['date','non_empty']]}))

    def test_pipeline_runs_provider_after_independent_source_failure(self):
        with TemporaryDirectory() as directory, patch.object(daily_run, 'ROOT', Path(directory)):
            result = daily_run.run_pipeline(SimpleNamespace(run_kind='local', shadow=True, dry_run=True),
                lambda _: {'status': 'failed'}, lambda _: {'status': 'partial'}, lambda _: {'status': 'partial'},
                publisher_runner=lambda _: {'status': 'completed', 'active_targets': 4})
        self.assertEqual(result['modules']['publisher_recent']['active_targets'], 4)

    def test_provider_sync_keeps_marketing_scope_and_dates_and_uses_same_daily_key(self):
        for marketing in (False, True):
            with self.subTest(marketing=marketing), TemporaryDirectory() as directory:
                root = Path(directory)
                captured = {}
                class API:
                    changes = 0
                    manifest = {'base': {'url': 'test'}}
                    def _fields(self, _): return {}
                    def read(self, table, *_):
                        if table == '项目总表' and marketing:
                            return [{'作品唯一键': 'weread:2', 'record_id': 'r2', '监测范围': ['营销项目'],
                                     '首次进入项目总表': '2026-10-01 00:00', '追踪截止日': '2026-10-30 00:00'}]
                        return []
                    def ensure(self, table, key, rows, *args, **kwargs):
                        captured[table] = rows
                        return {'weread:2': {'record_id': 'r2'}}
                data = {'schema_version': 'weread.public.v1', 'collection_date': '2026-11-05',
                        'started_at': '2026-11-05T10:00:00+08:00', 'finished_at': '2026-11-05T10:01:00+08:00',
                        'charts': [], 'field_mapping': {}, 'books': [{'book_id': '2', 'title': '新书',
                            'detail_source': {'observed_at': '2026-11-05T10:00:00+08:00'},
                            'reviewer_count': 3, 'today_reading_count': 9, 'recommendation_percent': None}]}
                with patch.object(sync_weread, 'ROOT', root):
                    sync_weread.sync(data, root/'metrics.json', api=API(), recent_tracks={'2': {
                        'start_date': '2026-11-05', 'end_date': '2026-12-04'}})
                project = captured['项目总表'][0]
                self.assertEqual(project['监测范围'], ['营销项目' if marketing else '版权方上新'])
                self.assertNotIn('首次上榜采集日', project)
                if marketing:
                    self.assertNotIn('追踪截止日', project)
                    self.assertNotIn('首次进入项目总表', project)
                daily = captured['每日数据'][0]
                self.assertEqual(daily['日数据唯一键'], '2026-11-05:weread:2')
                self.assertNotIn('累计阅读人数', daily)
                self.assertIn('累计阅读人数', daily['缺失字段与原因'])
                self.assertEqual(captured['榜单快照'], [])


if __name__ == '__main__': unittest.main()
