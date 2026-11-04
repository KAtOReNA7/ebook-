from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from scripts import daily_run


class PipelineIsolation(unittest.TestCase):
    def test_source_read_failure_does_not_cancel_platforms(self):
        calls=[]
        def tencent(_):
            raise OSError('source HTTP unavailable before any write')
        def weread(_):
            calls.append('weread')
            return {'status':'partial'}
        def fanqie(_):
            calls.append('fanqie')
            return {'status':'partial'}
        with TemporaryDirectory() as directory,patch.object(daily_run,'ROOT',Path(directory)):
            result=daily_run.run_pipeline(SimpleNamespace(run_kind='local',shadow=True,dry_run=False),tencent,weread,fanqie)
        self.assertEqual(calls,['weread','fanqie'])
        self.assertEqual(result['modules']['tencent']['status'],'failed')

    def test_unknown_shared_identity_write_blocks_dependent_mutation(self):
        calls=[]
        with TemporaryDirectory() as directory,patch.object(daily_run,'ROOT',Path(directory)):
            daily_run.atomic_json(Path(directory)/'runs/2026-11-03/tencent-state.json',{'phase':'requires_review'})
            def tencent(_):
                raise RuntimeError('unknown outcome')
            def platform(_):
                calls.append('unexpected')
            result=daily_run.run_pipeline(SimpleNamespace(run_kind='local',shadow=True,dry_run=False),tencent,platform,platform)
        self.assertFalse(calls)
        self.assertEqual(result['modules']['fanqie']['status'],'blocked')

    def test_weread_failure_does_not_cancel_separate_fanqie_books(self):
        def weread(_):
            raise RuntimeError('weread failed')
        with TemporaryDirectory() as directory,patch.object(daily_run,'ROOT',Path(directory)):
            result=daily_run.run_pipeline(SimpleNamespace(run_kind='local',shadow=True,dry_run=True),
                lambda _: {'status':'partial'},weread,lambda _: {'status':'partial','observations':6})
        self.assertEqual(result['modules']['fanqie']['observations'],6)


if __name__=='__main__':
    unittest.main()
