from contextlib import nullcontext
from datetime import datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import daily_run
from scripts.notification_health import assess, with_changes


def at(value):
    return datetime.fromisoformat('2026-11-' + value + '+08:00')


def row(status='待发送', day='2026-11-03', key='e'):
    return {'record_id': 'rec-' + key, '事件唯一键': key, '通知状态': [status], '发生日期': day + 'T00:00:00+08:00'}


class NotificationHealthTests(unittest.TestCase):
    def test_first_seen_and_15_minute_wait_are_persistent(self):
        first, state = assess([row()], {}, at('03T10:00:00'))
        self.assertEqual(first['status'], 'waiting')
        before, state2 = assess([row('发送中')], state, at('03T10:14:59'))
        self.assertEqual(before['status'], 'waiting')
        elapsed, _ = assess([row('发送中')], state2, at('03T10:15:00'))
        self.assertEqual(elapsed['status'], 'requires_review')
        self.assertEqual(elapsed['attention'][0]['reason'], 'pending_at_least_15_minutes')
        self.assertEqual(state2['events']['e']['first_seen_at'], '2026-11-03T10:00:00+08:00')

    def test_old_event_needs_attention_on_its_first_observation(self):
        summary, _ = assess([row(day='2026-11-02')], {}, at('03T10:00:00'))
        self.assertEqual(summary['status'], 'requires_review')
        self.assertEqual(summary['attention'][0]['reason'], 'pending_across_calendar_day')

    def test_failure_unknown_and_invalid_states_require_review(self):
        for status in ('发送失败', '结果未知', '不存在的状态'):
            with self.subTest(status=status):
                result, _ = assess([row(status)], {}, at('03T10:00:00'))
                self.assertEqual(result['status'], 'requires_review')

    def test_sent_is_only_workflow_state_even_with_receipt_text(self):
        for fields in ({}, {'发送时间': '2026-11-03T10:00:00+08:00', '发送回执': 'unverified text'}):
            result, _ = assess([{**row('已发送'), **fields}], {}, at('03T10:00:00'))
            self.assertEqual(result['event_observations'][0]['classification'], 'workflow_state_only')
            self.assertFalse(result['recipient_delivery_verified'])

    def test_empty_shadow_is_normal(self):
        result, _ = assess([], {}, at('03T10:00:00'), 'shadow')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['messages_sent_by_check'], 0)
        self.assertFalse(with_changes(result, {})['should_notify'])

    def test_same_issue_stays_nonzero_but_quiet_and_recovery_is_reportable(self):
        issue, state = assess([row('发送失败')], {}, at('03T10:00:00'))
        first = with_changes(issue, {})
        repeated = with_changes(issue, first)
        self.assertTrue(first['should_notify'])
        self.assertFalse(repeated['should_notify'])
        self.assertEqual(daily_run.pipeline_exit_code({'notification_delivery': repeated}), 2)
        resolved, _ = assess([row('已发送')], state, at('03T10:01:00'))
        self.assertTrue(with_changes(resolved, repeated)['should_notify'])

    def test_read_error_cannot_prove_recovery_or_repeat_the_same_notice(self):
        previous = {'issue_fingerprints': ['existing_issue']}
        error = {'status': 'read_error', 'issue_fingerprints': ['read_failure']}
        first = with_changes(error, previous)
        again = with_changes(error, first)
        self.assertEqual(first['recovered_issue_count'], 0)
        self.assertFalse(again['should_notify'])
        self.assertIn('existing_issue', again['issue_fingerprints'])

    def test_same_stalled_event_does_not_renotify_only_because_day_changed(self):
        _, state = assess([row()], {}, at('03T10:00:00'))
        stalled, state = assess([row()], state, at('03T10:15:00'))
        tomorrow, _ = assess([row()], state, at('04T10:00:00'))
        self.assertFalse(with_changes(tomorrow, stalled)['should_notify'])

    def test_deleted_unresolved_row_does_not_masquerade_as_delivered(self):
        _, state = assess([row('结果未知')], {}, at('03T10:00:00'))
        result, _ = assess([], state, at('03T10:01:00'))
        self.assertEqual(result['status'], 'requires_review')
        self.assertEqual(result['attention'][0]['reason'], 'unresolved_event_missing_from_ledger')

    def test_checker_reads_all_dates_without_any_write_api(self):
        class ReadOnlyBase:
            def __init__(self):
                self.calls = []
            def read(self, *args):
                self.calls.append(args)
                return [row(day='2026-11-02')]
        api = ReadOnlyBase()
        with TemporaryDirectory() as directory, patch.object(daily_run, 'ROOT', Path(directory)):
            first = daily_run.run_notification_health(True, at('03T10:00:00'), api)
            again = daily_run.run_notification_health(True, at('03T10:01:00'), api)
            state = json.loads((Path(directory) / 'runs/notification-health.json').read_text(encoding='utf-8'))
        self.assertEqual(api.calls, [('通知事件',), ('通知事件',)])
        self.assertTrue(first['should_notify'])
        self.assertFalse(again['should_notify'])
        self.assertEqual(state['events']['e']['first_seen_at'], '2026-11-03T10:00:00+08:00')

    def test_read_failure_is_nonzero_without_retry(self):
        class FailingBase:
            calls = 0
            def read(self, *_):
                self.calls += 1
                raise OSError('read unavailable')
        api = FailingBase()
        with TemporaryDirectory() as directory, patch.object(daily_run, 'ROOT', Path(directory)):
            result = daily_run.run_notification_health(True, at('03T10:00:00'), api)
        self.assertEqual(api.calls, 1)
        self.assertEqual(result['status'], 'read_error')
        self.assertEqual(daily_run.pipeline_exit_code({'notification_delivery': result}), 1)

    def test_main_runs_health_after_sources_even_when_source_failed(self):
        calls = []
        fake_module = SimpleNamespace(run=lambda _: None)
        def pipeline(*_):
            calls.append('sources')
            return {'date': '2026-11-03', 'status': 'failed', 'modules': {'weread': {'status': 'failed'}}}
        def health(*_):
            calls.append('health')
            return {'status': 'requires_review'}
        def initialize(*_):
            calls.append('initialize')
            return {'status': 'completed'}
        def project_notifications(*_):
            calls.append('project_notifications')
            return {'status': 'completed'}
        def expense_notifications(*_):
            calls.append('marketing_expense_notifications')
            return {'status': 'requires_review', 'events_created': 0}
        with patch.dict('sys.modules', {'tencent_daily': fake_module, 'fanqie_daily': fake_module}), \
                patch('sys.argv', ['daily_run.py', '--shadow']), \
                patch('sys.stdout', new_callable=io.StringIO), \
                patch.object(daily_run, 'process_lock', return_value=nullcontext()), \
                patch.object(daily_run, 'run_project_initialization', side_effect=initialize), \
                patch.object(daily_run, 'run_project_notifications', side_effect=project_notifications), \
                patch.object(daily_run, 'run_expense_notifications', side_effect=expense_notifications), \
                patch.object(daily_run.Base, 'call', side_effect=AssertionError('No real Feishu transport in unit tests')), \
                patch.object(daily_run, 'run_pipeline', side_effect=pipeline), \
                patch.object(daily_run, 'run_notification_health', side_effect=health), \
                patch.object(daily_run, 'atomic_json') as write:
            code = daily_run.main()
        self.assertEqual(calls, ['initialize', 'sources', 'project_notifications', 'marketing_expense_notifications', 'health'])
        self.assertEqual(code, 1)
        summary = write.call_args.args[1]
        self.assertIn('notification_delivery', summary)
        self.assertEqual(list(summary['modules']), ['weread', 'project_notifications', 'marketing_expense_notifications'])


if __name__ == '__main__':
    unittest.main()
