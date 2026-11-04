import unittest
import json
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path
from scripts import service_scope as scope
from scripts.cleanup_ebook_only import plan_cleanup
from scripts.sync_marketing import plan_import
from tests import test_source_review as source_fixtures
from scripts.initialize_manual_projects import build_plan

class ElectronicScopeTests(unittest.TestCase):
    def test_authoritative_fee_total_keeps_rounding_difference_but_not_bundle_ambiguity(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            row,source,_,_=source_fixtures.SourceReviewTests().fixture(root)
            row['normalized'].update(service_medium='ebook',fee=100,unit_price=3.3,arithmetic_check_passed=False)
            row['fee_statistics']={'book_cost_eligible':False,'exclusion_reasons':[]}
            def write():
                source.write_text(json.dumps({'schema_version':'tencent.marketing-rows.v1','rows':[row]}),encoding='utf-8')
            write()
            (root/'config').mkdir()
            with patch('scripts.sync_marketing.ROOT',root):
                before=plan_import(source,[],[],override_path=root/'absent.json')
                self.assertEqual(before['summary']['deferred_fee_total'],'100.00')
                (root/'config/marketing-fee-policy.json').write_text(json.dumps({'enabled':True,
                    'authoritative_total_platforms':['fanqie'],'basis':'User confirmed source total'}),encoding='utf-8')
                approved=plan_import(source,[],[],override_path=root/'absent.json')
                fields=approved['services'][0]['fields']
                self.assertEqual(fields['原始行总金额'],100)
                self.assertEqual(fields['已确认分摊金额'],100)
                self.assertEqual(fields['单价（元）'],3.3)
                row['fee_statistics']['exclusion_reasons']=['疑似多书合包未明确分摊'];write()
                held=plan_import(source,[],[],override_path=root/'absent.json')
                self.assertEqual(held['summary']['deferred_fee_total'],'100.00')

    def test_unknown_is_quarantined_and_audio_override_is_applied_before_scope(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            row,source,_,config=source_fixtures.SourceReviewTests().fixture(root)
            for override,reason in [(root/'absent.json','medium_unconfirmed'),(config,'confirmed_audio')]:
                p=plan_import(source,[],[],override_path=override,scope_policy={'enabled':True})
                self.assertEqual(p['services'],[]);self.assertEqual(p['projects'],[])
                self.assertEqual(p['summary']['excluded_by_reason'],{reason:1})
                self.assertEqual(p['summary']['source_rows_before_scope_filter'],1)
                self.assertEqual(p['summary']['source_raw_fee_total'],'0')

    def test_writes_cannot_smuggle_audio_as_electronic_book_metrics(self):
        with patch.object(scope,'policy',return_value={'enabled':True}):
            for table,row in [('营销服务明细',{'服务对象':'有声书','作品唯一键':'weread:123'}),
                              ('营销服务明细',{'服务对象':'未确认'}),
                              ('营销服务明细',{'服务对象':'电子书','服务内容':'有声书开分'}),
                              ('项目总表',{'作品形式':'有声书'}),
                              ('每日数据',{'作品唯一键':'weread:audio:123'})]:
                with self.subTest(table=table,row=row),self.assertRaises(ValueError):scope.enforce_write(table,row)
            scope.enforce_write('营销服务明细',{'邀评人数':30},{'服务对象':'电子书','服务内容':'电子书开分'})
            for medium in [None,'','有声书']:
                with self.subTest(medium=medium),self.assertRaises(ValueError):
                    scope.enforce_write('项目总表',{'作品形式':medium},creating=True)
            scope.enforce_write('项目总表',{'作品形式':'电子书'},creating=True)

    def test_cleanup_keeps_mixed_work_ebook_service_and_independent_chart_book(self):
        projects=[{'record_id':k,'作品唯一键':k,'作品形式':'电子书','监测范围':['营销项目']} for k in ['mixed','audio-only','chart']]
        projects.append({'record_id':'draft','作品唯一键':None,'书名':'user draft'})
        services=[{'record_id':rid,'作品唯一键':key,'服务对象':medium,'计入统计':['是'],'原始行总金额':fee,'已确认分摊金额':fee,'邀评人数':3}
                  for rid,key,medium,fee in [('e','mixed','电子书',10),('a','mixed','有声书',20),('b','audio-only','有声书',30),('c','chart','未确认',40)]]
        rows={'项目总表':projects,'营销服务明细':services,'每日数据':[{'record_id':k,'作品唯一键':k} for k in ['mixed','audio-only','chart']],
              '榜单快照':[{'record_id':'rank','作品唯一键':'chart','名次':1}],'通知事件':[]}
        with patch.object(scope,'policy',return_value={'enabled':True}):p=plan_cleanup(rows)
        self.assertEqual(p['delete']['项目总表'],['audio-only'])
        self.assertEqual(p['delete']['每日数据'],['audio-only'])
        self.assertEqual(p['delete']['营销服务明细'],['a','b','c'])
        self.assertEqual(p['summary']['retained_raw_fee'],'10.00')
        self.assertEqual(p['project_updates']['chart']['监测范围'],['仅榜单样本'])
        self.assertEqual(p['summary']['manual_drafts_preserved'],1)

    def test_old_snapshot_cannot_recreate_removed_marketing_only_books(self):
        admitted=scope.admitted_weread_keys({'charts':[{'records':[{'book_id':'3'}]}]}, {'weread:1':{},'weread:audio:2':{}})
        self.assertEqual(admitted,{'weread:1','weread:3'})

    def test_manual_audio_cannot_be_defaulted_to_ebook(self):
        p=build_plan([{'record_id':'r','书名':'book','平台':['微信读书'],'监测范围':['营销项目'],'作品形式':'有声书'}],'2026-11-03')
        self.assertFalse(p['updates']);self.assertEqual(len(p['pending']),1)

if __name__=='__main__':unittest.main()
