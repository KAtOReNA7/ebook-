import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import resume_daily_notifications as resume
from scripts.shared_runtime import capture, restore

DAY = '2026-11-09'


def write(root,name,value):
    path = root/name; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')


def snapshot(day):
    return {'schema_version':'weread.public.v1','collection_date':day,
        'started_at':day+'T10:01:00+08:00','finished_at':day+'T10:02:00+08:00','books':[],
        'charts':[{'chart_id':cid,'complete_top50':True,'records':[
            {'book_id':str(n),'rank':n} for n in range(1,51)]}
            for cid in ['rising','newbook','novel_female_rising','newbook_novel_female']]}


class ResumeNotificationsTests(unittest.TestCase):
    def setup_root(self,root):
        write(root,'runs/'+DAY+'/notification-comparison-projects.json',{'date':DAY,'projects':[]})
        write(root,'runs/'+DAY+'/daily-state.json',{'phase':'finished','summary':{'baseline':True}})
        write(root,'runs/'+DAY+'/fanqie-rising-state.json',{'phase':'finished','summary':{'baseline':False}})
        write(root,'runs/'+DAY+'/weread.json',snapshot(DAY))

    def test_missing_yesterday_stays_pending_after_collection_finished(self):
        with TemporaryDirectory() as d:
            root=Path(d); self.setup_root(root)
            result=resume.plan(root,DAY,[],{})
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['modules']['weread']['status'],'waiting_evidence')
            write(root,'runs/2026-11-08/weread.json',snapshot('2026-11-08'))
            result=resume.plan(root,DAY,[],{})
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['modules']['weread']['status'],'compared')

    def test_incomplete_and_duplicate_rank_cannot_compare(self):
        with TemporaryDirectory() as d:
            root=Path(d); self.setup_root(root)
            data=snapshot('2026-11-08'); data['charts'][0]['records'][1]['rank']=1
            write(root,'runs/2026-11-08/weread.json',data)
            self.assertEqual(resume.plan(root,DAY,[],{})['status'],'partial')

    def test_pending_pair_survives_later_day_and_empty_other_computer(self):
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            a,b=Path(a),Path(b); self.setup_root(a)
            write(a,'runs/2026-11-08/weread.json',snapshot('2026-11-08'))
            write(a,'runs/'+DAY+'/notification-comparison.json',{'status':'partial'})
            checkpoint=capture(a,'2026-11-12'); restore(b,checkpoint)
            self.assertTrue((b/'runs/2026-11-08/weread.json').exists())
            self.assertEqual(resume.plan(b,DAY,[],{})['status'],'completed')

    def test_only_explicit_date_can_recover(self):
        with TemporaryDirectory() as d:
            root=Path(d)
            write(root,'config/notification-recovery.json',{'authorized_dates':{DAY:{'recompute_unsent_only':True}}})
            self.assertTrue(resume.authorized(root,DAY))
            with self.assertRaisesRegex(ValueError,'authorization'):
                resume.recover(SimpleNamespace(date='2026-11-08',dry_run=True),root,Mock(),{})

    def test_new_member_does_not_send_second_category(self):
        row={'事件唯一键':'new','事件类型':'board_enter','通知标题':'book',
             '通知正文':'book｜上榜','关联项目':[{'id':'p'}],'收件人':[{'id':'owner'}]}
        with TemporaryDirectory() as d:
            root=Path(d)
            write(root,'config/notification-recovery.json',{'authorized_dates':{DAY:{'recompute_unsent_only':True}}})
            api=Mock(); api.read.return_value=[{'事件唯一键':'digest:v1:'+DAY+':board_enter',
                '规则ID':'v1:daily_digest:board_enter','判定证据':json.dumps({'member_event_ids':['old']})}]
            with patch.object(resume,'plan',return_value={'status':'completed','modules':{},'rows':[row]}), \
                 self.assertRaisesRegex(RuntimeError,'Category already sent'):
                resume.recover(SimpleNamespace(date=DAY,dry_run=True),root,api,
                               {'notifications_enabled':True,'mode':'owner_only'})

    def test_no_write_without_matching_dry_run_and_sent_event_is_deduplicated(self):
        row={'事件唯一键':'detail','事件类型':'board_enter','通知标题':'book',
             '通知正文':'book｜上榜','关联项目':[{'id':'project'}], '收件人':[{'id':'owner'}]}
        result={'status':'completed','date':DAY,'modules':{},'rows':[row]}
        with TemporaryDirectory() as d:
            root=Path(d)
            write(root,'config/notification-recovery.json',{'authorized_dates':{DAY:{'recompute_unsent_only':True}}})
            api=Mock(); api.read.return_value=[]
            args=SimpleNamespace(date=DAY,dry_run=False)
            runtime={'mode':'owner_only','notifications_enabled':True}
            with patch.object(resume,'plan',return_value=copy.deepcopy(result)), \
                 patch.object(resume.daily,'enforce_recipients'), \
                 patch.object(resume.daily,'deliver_event_rows') as send:
                with self.assertRaisesRegex(RuntimeError,'dry-run'): resume.recover(args,root,api,runtime)
                send.assert_not_called()
                args.dry_run=True
                self.assertEqual(resume.recover(args,root,api,runtime)['new_messages'],1)
                args.dry_run=False
                self.assertEqual(resume.recover(args,root,api,runtime)['messages_created'],1)
                api.read.return_value=[{'事件唯一键':'digest:v1:'+DAY+':board_enter',
                    '规则ID':'v1:daily_digest:board_enter','判定证据':json.dumps({'member_event_ids':['detail']})}]
                self.assertEqual(resume.recover(args,root,api,runtime)['messages_created'],0)
                self.assertEqual(send.call_count,1)


if __name__ == '__main__': unittest.main()
