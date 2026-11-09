import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from scripts.daily_reports import copyright_summary, event_row, ebook_body
from scripts.weread_copyright import apply_confirmed_copyright
from scripts.notification_digest import consumed_ids


class DailyReportTests(unittest.TestCase):
    def test_ranks_ties_and_zero(self):
        books=[{'book_id':str(n),'title':str(n),'copyright_provider':('磨铁数盟' if n<=10 else '甲' if n<=20 else '乙')} for n in range(1,51)]
        rows=[{'book_id':str(n),'title':str(n),'rank':n} for n in range(1,51)]
        snapshot={'books':books,'started_at':'actual','charts':[{'chart_id':c,'records':rows} for c in ['rising','newbook','novel_female_rising','newbook_novel_female']]}
        with TemporaryDirectory() as d:
            body,evidence=copyright_summary(snapshot,Path(d))
            self.assertEqual(evidence[0]['motie_count'],10)
            self.assertEqual(evidence[0]['motie_rank'],2)
            self.assertIn('No.2（并列）',body)
            self.assertIn('No.1 乙（30本）',body)
            snapshot['charts'][0]['records']=rows[:-1]
            with self.assertRaises(ValueError): copyright_summary(snapshot,Path(d))

    def test_confirmed_fallback_retains_platform_value_and_checks_identity(self):
        with TemporaryDirectory() as d:
            root=Path(d); (root/'config').mkdir()
            (root/'config/weread-copyright-confirmations.json').write_text(json.dumps({'books':{'1':{'title':'书','author':'人','source':'owner confirmation','confirmed_at':'2026-11-09','copyright_provider':'中文在线'}}}),encoding='utf-8')
            b={'book_id':'1','title':'书','author':'人','copyright_provider':None}
            self.assertEqual(apply_confirmed_copyright(b,root)['copyright_provider'],'中文在线')
            self.assertIsNone(apply_confirmed_copyright(b,root)['platform_copyright_provider'])
            with self.assertRaises(ValueError): apply_confirmed_copyright({**b,'author':'同名另一人'},root)

    def test_report_consumes_members_and_test_has_separate_key(self):
        row=event_row('2026-11-09','ebook','body',{'member_event_ids':['a','b']},['owner'])
        self.assertTrue({'a','b'}<=consumed_ids([row]))
        self.assertNotEqual(row['事件唯一键'],event_row('2026-11-09','ebook','body',{},['owner'],test=True)['事件唯一键'])

    def test_pending_comparison_not_claimed_as_no_changes(self):
        body=ebook_body({'date':'2026-11-09','modules':{'notification_comparison':{'modules':{'weread':{'status':'waiting_evidence'}}}}},[],[])
        self.assertIn('不能认定没有变化',body)

    def test_second_computer_does_not_send_existing_reports(self):
        from scripts.daily_reports import flush
        day='2026-11-09'
        records=[event_row(day,k,'saved',{'member_event_ids':[]},['owner']) for k in ['ebook','weread']]
        with TemporaryDirectory() as d, patch('scripts.daily_run.shanghai_day',return_value=day), \
             patch('scripts.daily_reports.complete_weread',return_value={}), \
             patch('scripts.daily_reports.copyright_summary',return_value=('body',[])):
            api=Mock(); api.read.side_effect=[records,[]]; writer=Mock()
            result=flush(SimpleNamespace(dry_run=False),{'date':day,'modules':{}},
                         {'notifications_enabled':True},Path(d),api,writer)
            self.assertEqual(result['messages_created'],0)
            writer.assert_not_called()


if __name__=='__main__': unittest.main()
