from copy import deepcopy
import unittest
from scripts.work_copyright import apply_work_copyright
from scripts.presentation import to_remote
from tests.test_presentation import schema


class WorkCopyrightTests(unittest.TestCase):
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
