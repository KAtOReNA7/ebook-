from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import daily_run, initialize_manual_projects as init, manual_project_notifications as notifications
from scripts.feishu_io import Base


def manual(rid, **extra):
    return {'record_id': rid, '书名': '手工书', '作品形式': '电子书', '监测范围': ['营销项目'], '平台': ['微信读书'], **extra}


class FakeBase:
    def __init__(self, rows):
        self.rows, self.calls, self.reads = deepcopy(rows), [], []
    def read(self, table, *args):
        self.reads.append(table)
        if table != '项目总表':
            raise AssertionError('Unexpected real-event table read in fake')
        return deepcopy(self.rows)
    def call(self, cmd, table, payload):
        self.calls.append((cmd, table, payload))
        for row in self.rows:
            row.update(payload['update_records'].get(row['record_id'], {}))


def write_source(root, day, baseline=False, events=None, dry=False):
    p = root / 'runs' / day / ('tencent-dry-run-outbox.json' if dry else 'tencent-outbox.json')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'baseline': baseline, 'events': events or []}), encoding='utf-8')


class ManualInitializationTests(unittest.TestCase):
    def test_unconfirmed_or_audio_medium_is_never_inferred_as_ebook(self):
        rows = [manual('blank', 作品形式=None), manual('audio', 作品形式='有声书')]
        before = deepcopy(rows)
        plan = init.build_plan(rows, '2026-11-03')
        self.assertEqual(plan['updates'], {})
        self.assertEqual(len(plan['pending']), 2)
        self.assertEqual(rows, before)

    def test_only_complete_explicit_manual_rows_initialized_and_existing_fields_preserved(self):
        rows = [manual('new'), manual('prior', 首次进入项目总表='2026-10-01 00:00',
                    追踪截止日='2026-12-31 00:00', 作品形式='电子书', 平台匹配状态='人工备注', 平台作品ID='unverified', 采集状态=['部分缺失']),
                manual('noscope', 监测范围=[]), manual('multi', 平台=['微信读书', '番茄小说']),
                manual('untitled', 书名=' '), manual('old', 作品唯一键='already-keyed')]
        before = deepcopy(rows)
        p = init.build_plan(rows, '2026-11-03')
        self.assertEqual(set(p['updates']), {'new', 'prior'})
        self.assertEqual(p['updates']['prior'], {'作品唯一键': 'pending:weread:ebook:manual-prior'})
        self.assertEqual(p['updates']['new']['追踪截止日'], '2026-12-02 00:00')
        self.assertEqual(p['updates']['new']['采集状态'], ['待接入'])
        self.assertNotIn('平台作品ID', p['updates']['new'])
        self.assertEqual(len(p['pending']), 3)
        self.assertEqual(rows, before)
        with TemporaryDirectory() as tmp:
            api = FakeBase(rows)
            result = init.run(SimpleNamespace(dry_run=False), Path(tmp), api, '2026-11-03')
            self.assertEqual(api.reads, ['项目总表', '项目总表'])
            self.assertEqual(result['updated_records'], 2)
            self.assertEqual(api.rows[1]['平台作品ID'], 'unverified')
            self.assertEqual(api.rows[2], before[2])
            self.assertFalse(init.has_unknown_guard(Path(tmp)))

    def test_start_date_derives_deadline_without_reset_and_invalid_date_is_retained(self):
        p = init.build_plan([manual('date', 首次进入项目总表='2026-10-01 00:00'),
                             manual('bad', 首次进入项目总表='invalid')], '2026-11-03')
        self.assertEqual(p['updates']['date']['追踪截止日'], '2026-10-30 00:00')
        self.assertNotIn('首次进入项目总表', p['updates']['date'])
        self.assertNotIn('追踪截止日', p['updates']['bad'])
        self.assertEqual(p['pending'][0]['record_id'], 'bad')

    def test_dry_run_no_mutation_timeout_blocks_next_attempt_and_other_sources(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = FakeBase([manual('new')])
            init.run(SimpleNamespace(dry_run=True), root, api, '2026-11-03')
            self.assertEqual(api.calls, [])
            api.call = Mock(side_effect=TimeoutError('unknown'))
            with self.assertRaises(TimeoutError):
                init.run(SimpleNamespace(dry_run=False), root, api, '2026-11-03')
            with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                init.run(SimpleNamespace(dry_run=False), root, api, '2026-11-03')
            api.call.assert_called_once()
            runner = Mock()
            with patch.object(daily_run, 'ROOT', root):
                summary = daily_run.run_pipeline(SimpleNamespace(run_kind='local', shadow=True, dry_run=False),
                    runner, runner, runner, {'status': 'failed', 'identity_mutation_unknown': True})
            runner.assert_not_called()
            self.assertEqual(summary['modules']['weread']['status'], 'blocked')

    def test_multiple_unkeyed_rows_do_not_break_unrelated_base_sync(self):
        api = Base.__new__(Base)
        api.manifest = {'tables': {'项目总表': {'field_readback': {'fields': [
            {'name': '作品唯一键', 'type': 'text'}, {'name': '书名', 'type': 'text'}]}}}}
        rows = [manual('one'), manual('two', 作品唯一键=None), manual('known', 作品唯一键='weread:123')]
        api.read = Mock(return_value=deepcopy(rows))
        api.call = Mock(side_effect=AssertionError('No write expected'))
        index = api.ensure('项目总表', '作品唯一键', [{'作品唯一键': 'weread:123', '书名': '手工书'}], update=True)
        self.assertEqual(set(index), {'weread:123'})
        api.call.assert_not_called()
        tracked, pending = daily_run.tracked_projects(rows, '2026-11-03')
        self.assertEqual(len(pending), 3)
        self.assertEqual(tracked, [])

    def test_old_metadata_does_not_enroll_nonmarketing_project(self):
        row = manual('legacy', 作品唯一键='weread:123', 平台作品ID='123',
            作品主页='https://weread.qq.com/web/reader/id', 监测范围=['旧系统延续'], 旧系统首次监测日='2026-10-01 00:00')
        before = deepcopy(row)
        self.assertEqual(daily_run.tracked_projects([row], '2026-10-30')[0], [])
        self.assertEqual(daily_run.tracked_projects([row], '2026-10-31')[0], [])
        self.assertEqual(daily_run.tracked_projects([row], '2026-09-30')[0], [])
        self.assertFalse(daily_run.is_marketing(row))
        self.assertEqual(row, before)

    def test_unknown_identity_migrations_block_before_any_project_read_or_write(self):
        for name in ('legacy-migration-state.json', 'link-state.json', 'alias-merge-state.json', 'plan.apply-state.json'):
            with self.subTest(name=name), TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / ('runs' if name.endswith('apply-state.json') else 'receipts') / 'day' / 'attempt' / name
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'phase': 'requires_review'}), encoding='utf-8')
                api = FakeBase([manual('new')])
                self.assertTrue(init.has_unknown_guard(root))
                with self.assertRaisesRegex(RuntimeError, 'migration outcome unknown'):
                    init.run(SimpleNamespace(dry_run=False), root, api, '2026-11-03')
                self.assertEqual(api.reads, [])
                self.assertEqual(api.calls, [])
                path.write_text(json.dumps({'phase': 'finished', 'readback': 'passed'}), encoding='utf-8')
                self.assertFalse(init.has_unknown_guard(root))
                path.write_text('{damaged', encoding='utf-8')
                self.assertTrue(init.has_unknown_guard(root))


class ManualNotificationRuntimeTests(unittest.TestCase):
    def test_shadow_registry_and_source_history_baselines_never_queue(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(root, '2026-11-03')
            args = SimpleNamespace(dry_run=False, shadow=True)
            writer = Mock(side_effect=AssertionError('No send in shadow'))
            api = FakeBase([manual('old')])
            result = notifications.run(args, {'status': 'completed'}, {'mode': 'live', 'notifications_enabled': True},
                                       root, api, '2026-11-03', writer)
            self.assertTrue(result['baseline'])
            self.assertTrue(result['registry_committed'])
            api.rows.append(manual('new'))
            write_source(root, '2026-11-04', baseline=True)
            result = notifications.run(args, {'status': 'completed'}, {'mode': 'shadow'}, root, api, '2026-11-04', writer)
            self.assertEqual(result['candidate_events'], 0)
            self.assertTrue(result['source_baseline'])
            writer.assert_not_called()

    def test_live_failure_keeps_registry_and_unknown_guard_success_commits_after_writer(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(root, '2026-11-03')
            api = FakeBase([manual('old')])
            shadow = SimpleNamespace(dry_run=False, shadow=True)
            notifications.run(shadow, {'status': 'completed'}, {'mode': 'shadow'}, root, api, '2026-11-03')
            registry = root / 'runs' / 'manual-project-registry.json'
            before = registry.read_bytes()
            api.rows.append(manual('new'))
            write_source(root, '2026-11-04')
            live = SimpleNamespace(dry_run=False, shadow=False)
            def failed(api, rows, day):
                self.assertEqual(registry.read_bytes(), before)
                self.assertEqual(len(rows), 1)
                raise TimeoutError('unknown write')
            with self.assertRaises(TimeoutError):
                notifications.run(live, {'status': 'completed'}, {'mode': 'live', 'notifications_enabled': True},
                                  root, api, '2026-11-04', failed)
            self.assertEqual(registry.read_bytes(), before)
            with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                notifications.run(live, {'status': 'completed'}, {'mode': 'live', 'notifications_enabled': True},
                                  root, api, '2026-11-04', Mock())
            # Simulate operator read-only reconciliation, not an automatic retry.
            state = root / 'runs' / 'manual-project-events-state.json'
            state.write_text(json.dumps({'phase': 'finished'}), encoding='utf-8')
            def verified(api, rows, day):
                self.assertEqual(registry.read_bytes(), before)
                return 0  # Existing immutable event was fully read back.
            result = notifications.run(live, {'status': 'completed'}, {'mode': 'live', 'notifications_enabled': True},
                                       root, api, '2026-11-04', verified)
            self.assertTrue(result['registry_committed'])
            self.assertIn('new', json.loads(registry.read_text())['seen_record_ids'])

    def test_unready_source_and_dry_run_do_not_advance_registry(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = FakeBase([manual('new')])
            args = SimpleNamespace(dry_run=True, shadow=True)
            result = notifications.run(args, {'status': 'failed'}, {}, root, api, '2026-11-03')
            self.assertFalse(result['registry_committed'])
            self.assertEqual(api.reads, [])
            write_source(root, '2026-11-03', dry=True)
            notifications.run(args, {'status': 'completed'}, {}, root, api, '2026-11-03')
            self.assertFalse((root / 'runs' / 'manual-project-registry.json').exists())


if __name__ == '__main__':
    unittest.main()
