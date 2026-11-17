import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock,patch

from scripts.shared_runtime import capture, digest, LEASE_ENV, Mutex
from scripts.stage_checkpoint import delta, start, run_stage
from scripts.execution_recovery import prepare,verify_plans,assert_original_stopped


class RecoveryTests(unittest.TestCase):
    def evidence(self,root):
        day='2099-01-01';base=capture(root,day)
        directory=root/'runs'/day;directory.mkdir(parents=True)
        (directory/'stage-progress.json').write_text(json.dumps({'execution':{'host':'test','root':str(root),'pid':123},
            'completed':{'weread':{'status':'partial','observed_at':'actual-time'}}}))
        (directory/'stage-write-plans.json').write_text(json.dumps({'tables':{'T':{'key':'key','rows':{'a':{'key':'a','n':2},'b':{'key':'b','n':3}}}}}))
        current=capture(root,day)
        journal=delta(base,current,'g','lease','fanqie','failed')
        return base,journal

    def test_completed_sources_restored_only_after_all_plans_and_original_lease(self):
        with TemporaryDirectory() as d:
            root=Path(d);base,journal=self.evidence(root)
            api=Mock();api.read.return_value=[{'key':'a','n':2},{'key':'b','n':3}]
            api._present.side_effect=lambda table,row:row
            mutex=Mock(spec=Mutex);mutex.head.return_value=('sha',{}, {'phase':'busy','token':'lease'})
            with patch('scripts.execution_recovery.assert_original_stopped') as stopped:
                result,progress,counts=prepare(root,base,{'generation':'g'},journal,mutex,api,'2099-01-01')
            stopped.assert_called_once();self.assertEqual(mutex.assert_owned.call_count,2)
            self.assertEqual(counts['T']['verified_plan_rows'],2)
            self.assertEqual(progress['completed']['weread']['observed_at'],'actual-time')
            api.read.return_value=[{'key':'a','n':2}]
            with patch('scripts.execution_recovery.assert_original_stopped'),self.assertRaisesRegex(RuntimeError,'differs'):
                prepare(root,base,{'generation':'g'},journal,mutex,api,'2099-01-01')
            mutex.release.assert_not_called();api.call.assert_not_called()

    def test_unknown_guard_or_other_lease_cannot_resume(self):
        with TemporaryDirectory() as d:
            root=Path(d);base,journal=self.evidence(root)
            api=Mock();mutex=Mock(spec=Mutex);mutex.head.return_value=('sha',{}, {'phase':'busy','token':'other'})
            with self.assertRaisesRegex(RuntimeError,'lease'):
                prepare(root,base,{'generation':'g'},journal,mutex,api,'2099-01-01')
            mutex.head.return_value=('sha',{}, {'phase':'busy','token':'lease'})
            journal['checkpoint_meta']['unresolved_guards']=['unknown']
            with self.assertRaisesRegex(RuntimeError,'unknown mutation'):
                prepare(root,base,{'generation':'g'},journal,mutex,api,'2099-01-01')
            api.read.assert_not_called();mutex.release.assert_not_called()

    def test_original_process_alive_or_other_host_blocks(self):
        with TemporaryDirectory() as d,patch('scripts.runner_policy.host_fingerprint',return_value='here'):
            value={'host':'elsewhere','root':str(Path(d).resolve()),'pid':123}
            with self.assertRaisesRegex(RuntimeError,'liveness'):assert_original_stopped(value,Path(d))
            value['host']='here';value['pid']=os.getpid()
            with self.assertRaisesRegex(RuntimeError,'active'):assert_original_stopped(value,Path(d))

    def test_resume_skips_successful_source_but_never_fresh_publication_readback(self):
        with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'lease'},clear=True), \
             patch('scripts.runner_policy.host_fingerprint',return_value='test'):
            root=Path(d);base=capture(root,'2099-01-01')
            start(root,base,{'generation':'g'},'lease',{'completed':{'weread':{'status':'partial'},
                'report_validation':{'status':'completed'}}})
            source=Mock();validation=Mock(return_value={'status':'completed'})
            with patch('scripts.stage_checkpoint.persist'):
                run_stage(root,'weread',source)
                run_stage(root,'report_validation',validation)
            source.assert_not_called();validation.assert_called_once()
