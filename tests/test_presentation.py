from copy import deepcopy
import unittest
from unittest.mock import patch
from scripts import feishu_io
from scripts import presentation as p


def schema(table):
    names = ['书名','书名来源原文','作品唯一键','平台','版权来源原文','分类原文',
             '版权方','一级分类','二级分类','最新点评人数（微信）','最新点评人数（番茄）',
             '最新推荐值（微信）','最新今日阅读人数（微信）','最新累计阅读人数（番茄）',
             '最新书评数量（番茄）','服务摘要','上榜摘要','每日摘要','服务内容',
             '邀评人数','原始行总金额','已确认分摊金额','服务日期','采集日期','数据日期',
             '服务明细唯一键','榜单快照唯一键','日数据唯一键','榜单','名次','原始书名',
             '点评人数','推荐值','今日阅读人数','累计阅读人数','书评数量','关联项目',
             '源行指纹','原始内容','服务平台','服务对象']
    return {name:{'name':name,'type':'select' if name in ['版权方','一级分类','二级分类'] else 'text'} for name in names}


class PresentationTests(unittest.TestCase):
    def test_reviewed_category_survives_remote_round_trip(self):
        for table in ['项目总表', '每日数据']:
            with self.subTest(table=table):
                physical={'作品唯一键':'fanqie:123','分类原文':'精品小说',
                          '一级分类':['精品小说'],'二级分类':['女性小说']}
                logical=p.from_remote(table,physical,schema(table))
                self.assertEqual(p.to_remote(table,logical,schema(table)),physical)

    def test_changed_native_category_replaces_old_display_pair(self):
        physical={'作品唯一键':'fanqie:123','分类原文':'精品小说',
                  '一级分类':['精品小说'],'二级分类':['女性小说']}
        logical=p.from_remote('项目总表',physical,schema('项目总表'))
        logical['作品分类']='精品小说/科幻小说'
        result=p.to_remote('项目总表',logical,schema('项目总表'))
        self.assertEqual(result['分类原文'],'精品小说/科幻小说')
        self.assertEqual(result['二级分类'],['科幻小说'])

    def test_unopened_rating_label_keeps_numeric_rating_empty(self):
        row={'日数据唯一键':'2026-11-04:weread:1','作品唯一键':'weread:1','平台':['微信读书'],
             '点评人数':0,'推荐值':None,'今日阅读人数':3,'采集状态':['未开分'],'缺失字段与原因':'推荐值：未开分'}
        result=p.to_remote('每日数据',row,schema('每日数据'))
        self.assertIn('未开分',result['每日摘要'])
        self.assertIsNone(result['推荐值'])

    def test_nested_parentheses_and_book_marks_are_removed_only_for_display(self):
        for title, expected in [('《活着》（典藏版）','活着'), ('A（版本(修订)附录）B','AB'),
                                ('《一》《二》(新增内容)','一二')]:
            with self.subTest(title=title):
                self.assertEqual(p.clean_title(title),expected)
        row={'书名':'《活着》（典藏版）','作品唯一键':'weread:123'}
        before=deepcopy(row)
        remote=p.to_remote('项目总表',row,schema('项目总表'))
        self.assertEqual(row,before)
        self.assertEqual(remote['书名'],'活着')
        self.assertEqual(remote['书名来源原文'],row['书名'])
        self.assertEqual(remote['作品唯一键'],row['作品唯一键'])
        self.assertEqual(p.from_remote('项目总表',remote,schema('项目总表'))['书名'],row['书名'])

    def test_copyright_single_select_retains_raw_for_motie_rules(self):
        for raw,short in [('微信读书出品','微信读书'),('磨铁数盟','磨铁'),
                          ('读客熊猫君阅读','读客'),('理想国imaginist','理想国')]:
            with self.subTest(raw=raw):
                row={'版权方原文':raw,'作品唯一键':'weread:123'}
                remote=p.to_remote('项目总表',row,schema('项目总表'))
                self.assertEqual(remote['版权方'],[short])
                self.assertEqual(remote['版权来源原文'],raw)
                self.assertNotIn('版权方原文',remote)
                logical=p.from_remote('项目总表',remote,schema('项目总表'))
                self.assertEqual(logical['版权方原文'],raw)

    def test_categories_keep_full_raw_and_two_single_labels(self):
        cases=[('女生小说-现代言情','女生小说','现代言情'),
               ('精品小说/都市小说/双向奔赴/现代言情','精品小说','都市小说'),
               ('种田/古代言情/乡村/穿越/空间/发家致富','古代言情','种田'),
               ('宫斗宅斗/古代言情/重生/嫡女/HE/1v1','古代言情','宫斗宅斗')]
        for raw,first,second in cases:
            with self.subTest(raw=raw):
                row={'作品分类':raw,'作品唯一键':'fanqie:123'}
                remote=p.to_remote('项目总表',row,schema('项目总表'))
                self.assertEqual(remote['一级分类'],[first])
                self.assertEqual(remote['二级分类'],[second])
                self.assertEqual(remote['分类原文'],raw)
                self.assertEqual(p.from_remote('项目总表',remote,schema('项目总表'))['作品分类'],raw)

    def test_fanqie_reviewers_never_go_into_weread_column(self):
        remote=p.to_remote('项目总表',{'作品唯一键':'fanqie:123','最新点评人数':97,
                                     '最新累计阅读人数':10000,'最新书评数量':32},schema('项目总表'))
        self.assertEqual(remote['最新点评人数（番茄）'],97)
        self.assertNotIn('最新点评人数（微信）',remote)
        self.assertEqual(remote['最新累计阅读人数（番茄）'],10000)
        self.assertEqual(remote['最新书评数量（番茄）'],32)
        logical=p.from_remote('项目总表',remote,schema('项目总表'))
        self.assertEqual(logical['最新点评人数'],97)

    def test_weread_metrics_renamed_without_changing_numeric_values(self):
        row={'作品唯一键':'weread:123','最新点评人数':107,'最新推荐值':90.1,
             '最新今日阅读人数':500}
        remote=p.to_remote('项目总表',row,schema('项目总表'))
        self.assertEqual(remote['最新点评人数（微信）'],107)
        self.assertEqual(remote['最新推荐值（微信）'],90.1)
        self.assertEqual(remote['最新今日阅读人数（微信）'],500)
        self.assertNotIn('最新点评人数（番茄）',remote)

    def test_old_schema_does_not_activate_projection(self):
        fields={name:{'name':name,'type':'text'} for name in ['书名','版权方原文','作品分类']}
        row={'书名':'《旧书》（版）','版权方原文':'磨铁数盟','作品分类':'精品小说-都市小说'}
        self.assertEqual(p.to_remote('项目总表',row,fields),row)

    def test_summaries_exclude_title_and_date_but_keep_business_values(self):
        cases=[('营销服务明细','服务摘要',{'服务明细唯一键':'service:key','书名':'秘密书名',
                '服务日期':'2026-11-03 00:00','服务内容':'短评','邀评人数':20,'原始行总金额':100},['短评','20','100']),
               ('榜单快照','上榜摘要',{'榜单快照唯一键':'board:key','书名':'秘密书名',
                '采集日期':'2026-11-03 00:00','榜单':['微信新书Top50'],'名次':2},['微信新书','2']),
               ('每日数据','每日摘要',{'日数据唯一键':'day:key','书名':'秘密书名','平台':['微信读书'],
                '数据日期':'2026-11-03 00:00','点评人数':107,'推荐值':91.5,'今日阅读人数':600},['107','91.5','600'])]
        for table,name,row,expected in cases:
            with self.subTest(table=table):
                out=p.to_remote(table,row,schema(table))
                summary=out[name]
                self.assertNotIn('秘密书名',summary)
                self.assertNotIn('2026-11-03',summary)
                for item in expected:self.assertIn(item,summary)

    def test_partial_service_patch_uses_context_without_rewriting_source_data(self):
        context={'服务明细唯一键':'service:key','服务内容':'短评','邀评人数':20,
                 '原始行总金额':100,'源行指纹':'raw-sha','原始内容':'raw-json','书名':'《旧书》（版）'}
        patch={'关联项目':[{'id':'new-project'}]}
        out=p.to_remote('营销服务明细',patch,schema('营销服务明细'),context=context)
        self.assertEqual(out['关联项目'],patch['关联项目'])
        for key in ['源行指纹','原始内容','原始行总金额','邀评人数']:
            self.assertTrue(key not in out or out[key]==context[key])
        if '服务摘要' in out:
            self.assertIn('短评',out['服务摘要'])
            self.assertIn('20',out['服务摘要'])
            self.assertIn('100',out['服务摘要'])

    def test_raw_source_cost_and_keys_are_immutable_under_projection(self):
        row={'服务明细唯一键':'source:469:sha','原始书名':'《春风》（修订）','书名':'《春风》（修订）',
             '原始内容':'{"title":"《春风》（修订）"}','源行指纹':'sha','原始行总金额':180,
             '已确认分摊金额':180,'邀评人数':30,'服务内容':'评论','服务日期':'2026-11-01 00:00'}
        out=p.to_remote('营销服务明细',row,schema('营销服务明细'))
        for key in row.keys()-{'书名'}:
            self.assertEqual(out[key],row[key],key)

    def test_manual_title_edit_is_not_hidden_by_stale_source_title(self):
        physical={'书名':'人工更正书名','书名来源原文':'《旧书名》（版）','作品唯一键':'weread:123'}
        logical=p.from_remote('项目总表',physical,schema('项目总表'))
        self.assertEqual(logical['书名'],'人工更正书名')

    def test_projection_is_idempotent_for_physical_row(self):
        row={'书名':'《测试》（新版）','作品唯一键':'fanqie:123','版权方原文':'磨铁数盟',
             '作品分类':'精品小说/都市小说/其他标签','最新点评人数':10}
        first=p.to_remote('项目总表',row,schema('项目总表'))
        second=p.to_remote('项目总表',first,schema('项目总表'))
        self.assertEqual(second,first)


class PresentationBoundaryTests(unittest.TestCase):
    def memory_base(self, table, row):
        class MemoryBase(feishu_io.Base):
            def __init__(self):
                self.presentation_enabled=True
                self.manifest={'tables':{table:{'field_readback':{'fields':list(schema(table).values())}}}}
                self._record_cache={}
                self._project_titles={}
                self.physical={'r':{'record_id':'r',**p.to_remote(table,row,schema(table))}}
                self.changes=0

            def _ensure_display_options(self, table, rows):
                pass

            def read(self, table, conditions=None):
                rows=[p.from_remote(table,r,schema(table)) for r in self.physical.values()]
                self._record_cache[table]={r['record_id']:r for r in rows}
                return rows

            def call(self, command, table=None, payload=None, **flags):
                if command!='+record-batch-update':raise AssertionError(command)
                for rid,update in payload['update_records'].items():
                    presented=self._present(table,update,self._record_cache[table].get(rid))
                    self.physical[rid].update(presented)
                    self.changes+=1
                return {'ok':True}

        return MemoryBase()

    def test_ensure_partial_service_patch_compares_and_writes_same_summary(self):
        row={'服务明细唯一键':'service:key','作品唯一键':'weread:123','服务内容':'短评',
             '邀评人数':20,'原始行总金额':100}
        api=self.memory_base('营销服务明细',row)
        api.ensure('营销服务明细','服务明细唯一键',[{'服务明细唯一键':'service:key','邀评人数':25}],update=True)
        self.assertEqual(api.changes,1)
        summary=api.physical['r']['服务摘要']
        for part in ['短评','25','100']:self.assertIn(part,summary)
        self.assertEqual(api.physical['r']['原始行总金额'],100)

    def test_same_full_service_source_replay_is_zero_writes(self):
        row={'服务明细唯一键':'service:key','作品唯一键':'weread:123','服务内容':'短评',
             '邀评人数':20,'原始行总金额':100,'书名':'《测试》（典藏版）'}
        api=self.memory_base('营销服务明细',row)
        api.ensure('营销服务明细','服务明细唯一键',[row],update=True)
        self.assertEqual(api.changes,0)

    def test_future_select_option_is_appended_without_losing_existing_definition(self):
        original={'id':'fld-category','name':'一级分类','type':'select','multiple':False,
                  'description':'保留描述','options':[{'name':'文学','hue':'Green','lightness':'Lighter'}]}
        api=self.memory_base('项目总表',{'作品唯一键':'weread:123'})
        api.manifest['tables']['项目总表']['field_readback']['fields']=[deepcopy(original)]
        stored=deepcopy(original)
        calls=[]
        def transport(command,table=None,payload=None,**flags):
            calls.append((command,deepcopy(payload)))
            if command=='+field-get':return {'data':{'field':deepcopy(stored)}}
            if command=='+field-update':
                stored.update(payload)
                return {'ok':True}
            raise AssertionError(command)
        with patch.object(api,'call',side_effect=transport),patch.object(feishu_io,'save') as save:
            feishu_io.Base._ensure_display_options(api,'项目总表',[{'一级分类':['新分类']}])
        self.assertEqual(stored['multiple'],False)
        self.assertEqual(stored['description'],original['description'])
        self.assertEqual(stored['options'][0],original['options'][0])
        self.assertEqual({o['name'] for o in stored['options']},{'文学','新分类'})
        self.assertEqual([c[0] for c in calls],['+field-get','+field-update','+field-get'])
        save.assert_called_once()

    def test_daily_book_name_is_filled_from_cached_project_without_remote_read(self):
        row={'日数据唯一键':'day:weread:123','作品唯一键':'weread:123','点评人数':12}
        api=self.memory_base('每日数据',row)
        api._project_titles={'weread:123':'《书名》（修订版）'}
        with patch.object(api,'read',side_effect=AssertionError('No extra remote read')):
            actual=api._present('每日数据',row)
        self.assertEqual(actual['书名'],'书名')
        self.assertEqual(actual['书名来源原文'],'《书名》（修订版）')


class CompleteSelectOptionsTests(unittest.TestCase):
    def field(self, count=205):
        return {'id':'fld-options','name':'二级分类','type':'select','multiple':False,
                'description':'保留字段说明',
                'options':[{'id':f'opt{i}','name':f'类别{i}','hue':'Green','lightness':'Lighter'} for i in range(count)]}

    def page_base(self, pages):
        api=object.__new__(feishu_io.Base)
        calls=[]
        def transport(command,table=None,payload=None,**flags):
            self.assertEqual(command,'+field-search-options')
            calls.append(flags)
            return {'data':deepcopy(pages[len(calls)-1])}
        api.call=transport
        return api,calls

    def test_more_than_200_options_are_complete_and_preserve_metadata(self):
        complete=self.field(205)
        truncated={**complete,'options':complete['options'][:50]}
        api,calls=self.page_base([{'options':complete['options'][:200],'total':205},
                                  {'options':complete['options'][200:],'total':205}])
        self.assertEqual(api.complete_field('项目总表',truncated),complete)
        self.assertEqual([c['offset'] for c in calls],[0,200])
        self.assertEqual([c['limit'] for c in calls],[200,200])
        self.assertEqual(len(truncated['options']),50)

    def test_total_smaller_than_accumulated_options_is_rejected(self):
        complete=self.field(205)
        api,_=self.page_base([{'options':complete['options'][:200],'total':205},
                              {'options':complete['options'][200:],'total':204}])
        with self.assertRaisesRegex(RuntimeError,'count mismatch|total changed'):
            api.complete_field('项目总表',{**complete,'options':complete['options'][:50]})

    def test_empty_page_before_total_is_rejected_without_loop(self):
        complete=self.field(205)
        api,calls=self.page_base([{'options':complete['options'][:200],'total':205},
                                  {'options':[],'total':205}])
        with self.assertRaisesRegex(RuntimeError,'Incomplete|progress'):
            api.complete_field('项目总表',{**complete,'options':complete['options'][:50]})
        self.assertEqual(len(calls),2)

    def test_changing_total_cannot_certify_a_complete_snapshot(self):
        complete=self.field(205)
        api,_=self.page_base([{'options':complete['options'][:200],'total':205},
                              {'options':complete['options'][200:204],'total':204}])
        with self.assertRaises(RuntimeError):
            api.complete_field('项目总表',{**complete,'options':complete['options'][:50]})

    def test_append_preserves_options_beyond_the_first_50_and_200(self):
        stored=self.field(205)
        stored.update(remaining_options_count=155,truncated=True,truncated_options_count=155)
        original=deepcopy(stored)
        api=object.__new__(feishu_io.Base)
        api.presentation_enabled=True
        api.manifest={'tables':{'项目总表':{'field_readback':{'fields':[{**deepcopy(stored),'options':deepcopy(stored['options'][:50])}]}}}}
        updated=[]
        offsets=[]
        def transport(command,table=None,payload=None,**flags):
            if command=='+field-get':return {'data':{'field':{**deepcopy(stored),'options':deepcopy(stored['options'][:50])}}}
            if command=='+field-search-options':
                offsets.append(flags['offset'])
                start=flags['offset']
                return {'data':{'options':deepcopy(stored['options'][start:start+flags['limit']]),'total':len(stored['options'])}}
            if command=='+field-update':
                updated.append(deepcopy(payload))
                stored.update(payload)
                return {'ok':True}
            raise AssertionError(command)
        api.call=transport
        with patch.object(feishu_io,'save'):
            api._ensure_display_options('项目总表',[{'二级分类':['新增类']}])
        self.assertEqual(len(updated),1)
        self.assertFalse({'remaining_options_count','truncated','truncated_options_count'} & set(updated[0]))
        self.assertEqual(updated[0]['options'][:205],original['options'])
        self.assertEqual(len(stored['options']),206)
        self.assertEqual(stored['options'][-1]['name'],'新增类')
        self.assertEqual(stored['description'],original['description'])
        self.assertFalse(stored['multiple'])
        self.assertEqual(offsets,[0,200,0,200])
        self.assertEqual(len(api._fields('项目总表')['二级分类']['options']),206)


if __name__=='__main__':unittest.main()
