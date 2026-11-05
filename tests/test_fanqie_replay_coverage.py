import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import fanqie_daily


def project(key, book_id=None):
    return {'作品唯一键': key, '平台作品ID': book_id,
            '平台': ['番茄小说'], '监测范围': ['营销项目']}


class ReplayCoverageTests(unittest.TestCase):
    def replay(self, projects, observation_day='2026-11-05', missing_fields=None):
        with TemporaryDirectory() as directory:
            root=Path(directory); run=root/'runs/2026-11-05'; (run/'fanqie').mkdir(parents=True)
            state=run/'fanqie-state.json'
            state.write_text(json.dumps({'phase':'finished','summary':{'status':'completed',
                'marketing_projects':1,'projects':1,'observations':1,'events_created':0}}),encoding='utf-8')
            original=state.read_bytes()
            (run/'fanqie/snapshot.json').write_text(json.dumps({'collection_date':observation_day,
                'finished_at':observation_day+'T10:01:00+08:00',
                'books':[{'book_id':'123456789012345','missing_fields':missing_fields or []}]}),encoding='utf-8')
            class ReadOnlyBase:
                def __init__(self,*_): pass
                def read(self,table):
                    self_table=table
                    assert self_table=='项目总表'
                    return projects
                def call(self,*_): raise AssertionError('Replay must not write')
            with patch.object(fanqie_daily,'ROOT',root), \
                    patch.object(fanqie_daily,'shanghai_day',return_value='2026-11-05'), \
                    patch.object(fanqie_daily,'Base',ReadOnlyBase), \
                    patch.object(fanqie_daily,'call_script') as collect:
                result=fanqie_daily.run(SimpleNamespace(dry_run=False,shadow=False))
                collect.assert_not_called()
                self.assertEqual(state.read_bytes(),original)
            return result

    def test_late_pending_project_is_in_current_coverage_without_new_observation(self):
        result=self.replay([project('fanqie:123456789012345','123456789012345'),
                            project('pending:fanqie:ebook:new')])
        self.assertEqual(result['marketing_projects'],2)
        self.assertEqual(result['observations'],1)
        self.assertEqual(result['writes_this_replay'],0)
        self.assertEqual(result['pending'],['pending:fanqie:ebook:new: platform identity pending'])
        self.assertEqual(result['status'],'partial')

    def test_new_official_id_is_not_falsely_counted_as_observed(self):
        result=self.replay([project('fanqie:123456789012345','123456789012345'),
                            project('fanqie:223456789012345','223456789012345')])
        self.assertEqual(result['projects'],2)
        self.assertEqual(result['observations'],1)
        self.assertIn('missing from frozen',result['pending'][0])

    def test_yesterdays_sample_cannot_satisfy_today_replay(self):
        with self.assertRaises(ValueError):
            self.replay([project('fanqie:123456789012345','123456789012345')],'2026-11-04')

    def test_confirmed_work_ownership_applies_to_replay_without_hiding_missing_readers(self):
        row=project('fanqie:123456789012345','123456789012345')
        for missing,expected in [(['copyright_provider'],[]),
                                 (['copyright_provider','cumulative_readers'],['123456789012345: missing cumulative_readers'])]:
            with self.subTest(missing=missing):
                result=self.replay([row],missing_fields=missing)
                self.assertEqual(result['pending'],expected)


if __name__=='__main__':
    unittest.main()
