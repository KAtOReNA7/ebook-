from copy import deepcopy
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from scripts.work_copyright import apply_work_copyright
from scripts.presentation import to_remote
from tests.test_presentation import schema


class WorkCopyrightTests(unittest.TestCase):
    def test_confirmation_evidence_is_not_itself_motie_ownership(self):
        from scripts import sync_weread
        with TemporaryDirectory() as d:
            root=Path(d);(root/'config').mkdir()
            (root/'config/copyright-aliases.json').write_text(json.dumps({'exact_aliases':['磨铁','磨铁数盟']}))
            captured={}
            class API:
                changes=0
                manifest={'base':{'url':'test'}}
                def _fields(self,_):return {}
                def read(self,table,*_):
                    return [{'作品唯一键':'weread:2','record_id':'r2','监测范围':['仅榜单样本'],'磨铁版权确认':['是']}] if table=='项目总表' else []
                def ensure(self,table,key,rows,*a,**kw):
                    captured[table]=rows
                    return {'weread:2':{'record_id':'r2'}}
            data={'schema_version':'weread.public.v1','collection_date':'2026-11-11','started_at':'2026-11-11T10:00:00+08:00',
                  'finished_at':'2026-11-11T10:01:00+08:00','charts':[],'field_mapping':{},
                  'books':[{'book_id':'2','title':'book','author':'author','copyright_provider':'中文在线','copyright_basis':'user confirmed publisher'}]}
            with patch.object(sync_weread,'ROOT',root),patch.object(sync_weread,'policy',return_value={'enabled':False}):
                sync_weread.sync(data,root/'source.json',api=API())
            self.assertEqual(captured['项目总表'][0]['磨铁版权确认'],['待核实'])

    def test_marketing_ownership_does_not_erase_platform_provider_or_other_missing_fields(self):
        raw={'copyright_provider':'阅文','missing_fields':['copyright_provider','reviewer_count']}
        before=deepcopy(raw)
        policy={'enabled':True,'marketing_owner':'磨铁','basis':'user-confirmed'}
        result=apply_work_copyright(raw,True,policy)
        self.assertEqual(result['copyright_provider'],'磨铁')
        self.assertEqual(result['platform_copyright_provider'],'阅文')
        self.assertEqual(result['missing_fields'],['reviewer_count'])
        self.assertEqual(raw,before)
        self.assertEqual(apply_work_copyright(result,True,policy),result)

    def test_chart_only_does_not_inherit_company_ownership(self):
        raw={'copyright_provider':'其他版权方'}
        self.assertEqual(apply_work_copyright(raw,False,{'enabled':True}),raw)

    def test_display_can_show_confirmed_owner_while_preserving_raw_or_missing_platform_text(self):
        for platform_raw in ('阅文',None):
            row={'版权方原文':platform_raw,'版权方':['磨铁']}
            display=to_remote('项目总表',row,schema('项目总表'))
            self.assertEqual(display['版权方'],['磨铁'])
            self.assertEqual(display['版权来源原文'],platform_raw)
            self.assertEqual(to_remote('项目总表',display,schema('项目总表')),display)


if __name__=='__main__':unittest.main()
