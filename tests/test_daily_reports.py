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
            self.assertNotIn('实际采样：',body)
            self.assertNotIn('按每个榜单',body)
            snapshot['charts'][0]['records']=rows[:-1]
            with self.assertRaises(ValueError): copyright_summary(snapshot,Path(d))

    def test_zero_motie_limits_to_five_providers_even_at_tie_boundary(self):
        providers=['甲']*15+['乙']*10+['丙']*8+['丁']*7+['戊']*4+['己']*4+['庚']*2
        books=[{'book_id':str(i),'copyright_provider':p} for i,p in enumerate(providers)]
        rows=[{'book_id':str(i),'rank':i+1,'title':str(i)} for i in range(50)]
        snapshot={'books':books,'charts':[{'chart_id':c,'records':rows} for c in ['rising','newbook','novel_female_rising','newbook_novel_female']]}
        with TemporaryDirectory() as d:
            body,evidence=copyright_summary(snapshot,Path(d))
        self.assertIn('版权方前5名：',body)
        self.assertIn('No.5 己（4本）',body)
        self.assertNotIn('戊',body)
        self.assertNotIn('庚',body)
        self.assertIsNone(evidence[0]['motie_rank'])

    def test_marketing_section_preserves_details_without_duplicate_business_entry(self):
        events=[{'事件类型':'new_marketing_service','通知正文':'《书》于2026年11月10日，约评30条，花费100元。'},
                {'事件类型':'fanqie_milestone','通知正文':'另一书累计阅读突破1万'}]
        body=ebook_body({'date':'2026-11-10','modules':{}},events,[])
        self.assertEqual(body.count('花费100元'),1)
        self.assertIn('一、营销变化',body)
        self.assertIn('二、业务变化',body)
        for removed in ('日期：','今日采集与同步','待核实事项','已入表日数据'):
            self.assertNotIn(removed,body)
        self.assertIn('一、营销变化\n无',ebook_body({'modules':{}},[],[]))

    def test_unverified_marketing_cannot_be_reported_as_none(self):
        body=ebook_body({'modules':{'tencent':{'status':'requires_review'}}},[],[])
        self.assertIn('营销变化暂无法核实',body)

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
