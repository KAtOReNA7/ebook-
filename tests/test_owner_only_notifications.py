import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import daily_run
from scripts.feishu_io import Base
from scripts.notification_policy import OWNER_ID, enforce_recipients, resend_fingerprint


class OwnerOnlyNotifications(unittest.TestCase):
    def test_exact_authorized_copy_can_exclude_owner_without_changing_daily_roster(self):
        from scripts.daily_reports import event_row
        from scripts.notification_policy import notification_recipients
        from datetime import timezone,timedelta
        day=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        row=event_row(day,'ebook','same daily report',{},['ou_verified_a','ou_verified_b'])
        row['事件唯一键']+=':resend:explicit-extra-recipients'
        with TemporaryDirectory() as directory, patch('scripts.notification_policy.OWNER_CONFIGURED',True):
            root=Path(directory)
            with self.assertRaises(ValueError): enforce_recipients(row,root)
            entry={'explicit_owner_request':True,'date':day,'request':'Copy only to these two people',
                   'sha256':resend_fingerprint(row)}
            path=root/'config/authorized-report-resends.json'
            daily_run.atomic_json(path,{'events':{row['事件唯一键']:entry}})
            enforce_recipients(row,root)
            self.assertEqual(notification_recipients(root),[OWNER_ID])
            with self.assertRaises(ValueError):
                enforce_recipients({**row,'收件人':row['收件人']+[{'id':OWNER_ID}]},root)
            entry['completed']=True; daily_run.atomic_json(path,{'events':{row['事件唯一键']:entry}})
            with self.assertRaises(ValueError): enforce_recipients(row,root)

    def test_explicit_resend_is_exact_and_cannot_expand_future_reports(self):
        from scripts.daily_reports import event_row
        from datetime import timezone,timedelta
        import copy
        day=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        row=event_row(day,'ebook','approved content',{},[OWNER_ID,'ou_other'])
        row['事件唯一键']+=':resend:owner-request'
        with TemporaryDirectory() as directory, patch('scripts.notification_policy.OWNER_CONFIGURED',True):
            root=Path(directory)
            entry={'explicit_owner_request':True,'date':day,'request':'Owner requested this resend','sha256':resend_fingerprint(row)}
            policy={'events':{row['事件唯一键']:entry}}
            path=root/'config/authorized-report-resends.json'
            daily_run.atomic_json(path,policy)
            enforce_recipients(row,root)
            for field,value in [('通知正文','changed'),('事件唯一键','daily-report:v1:other'),
                                ('发生日期','2000-01-01 00:00'),('收件人',[{'id':OWNER_ID},{'id':'ou_third'}])]:
                changed=copy.deepcopy(row); changed[field]=value
                with self.subTest(field=field),self.assertRaises(ValueError): enforce_recipients(changed,root)
            entry['completed']=True; daily_run.atomic_json(path,policy)
            with self.assertRaises(ValueError): enforce_recipients(row,root)

    def test_unconfigured_owner_blocks_transport_even_with_matching_placeholder(self):
        with patch('scripts.notification_policy.OWNER_CONFIGURED', False), self.assertRaises(ValueError):
            enforce_recipients({'收件人': [{'id': OWNER_ID}]})

    def test_no_date_can_turn_debug_into_production(self):
        with TemporaryDirectory() as directory, patch.object(daily_run, 'ROOT', Path(directory)):
            daily_run.atomic_json(Path(directory) / 'config/runtime.json',
                                  {'mode': 'live', 'notifications_enabled': True,
                                   'effective_from': '2026-11-04', 'production_approved': False})
            with patch.object(daily_run, 'now', return_value=datetime.fromisoformat('2027-11-04T10:00:00+08:00')):
                status = daily_run.notification_runtime()
                self.assertEqual(status['mode'], 'owner_only')
                self.assertFalse(status['production_approved'])
                self.assertFalse(daily_run.notification_runtime(True)['notifications_enabled'])

    def test_missing_config_and_stale_two_person_outbox_cannot_notify_others(self):
        with TemporaryDirectory() as directory:
            for recipients in [[], [{'id': 'other'}], [{'id': OWNER_ID}, {'id': 'other'}]]:
                with self.subTest(recipients=recipients), self.assertRaises(ValueError):
                    enforce_recipients({'收件人': recipients}, directory)
            with patch('scripts.notification_policy.OWNER_CONFIGURED', True):
                enforce_recipients({'收件人': [{'id': OWNER_ID}]}, directory)

    def test_event_transport_rejects_before_any_cli_call(self):
        api = object.__new__(Base)
        with patch('scripts.feishu_io.subprocess.run') as cli, patch('scripts.runner_policy.enforce_writer'):
            with self.assertRaises(ValueError):
                api.call('+record-batch-create', '通知事件',
                         {'create_records': [{'收件人': [{'id': OWNER_ID}, {'id': 'other'}]}]})
            cli.assert_not_called()

    def test_existing_sent_history_is_not_changed_on_replay(self):
        row = {'事件唯一键': 'old', '收件人': [{'id': OWNER_ID}, {'id': 'other'}],
               **{k: '' for k in ['事件类型', '关联项目', '规则ID', '判定证据', '通知标题', '通知正文', '采集批次']}}
        class History:
            def read(self, *_): return [{**row, '通知状态': ['已发送']}]
            def call(self, *_): raise AssertionError('No repeat write allowed')
        self.assertEqual(daily_run.deliver_event_rows(History(), [row], '2026-11-04'), 0)


if __name__ == '__main__':
    unittest.main()
