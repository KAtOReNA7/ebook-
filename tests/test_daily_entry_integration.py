"""Documented executable in a disposable checkout, with only external adapters faked."""
import json,os,shutil,subprocess,sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from tests.test_report_publication import ready_pipeline,complete_evidence
from tests.test_operational_validation import workflow_fixture


class EntryIntegrationTests(unittest.TestCase):
    def setup_copy(self,root):
        repo=Path(__file__).resolve().parents[1]
        shutil.copytree(repo/'scripts',root/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(repo/'tests/fixtures/daily_entry_runtime.py',root/'fixture_runtime.py')
        for name in ('daily_run.py','collect_weread.py'):
            path=root/'scripts'/name;text=path.read_text(encoding='utf8')
            marker='if __name__ == "__main__":' if name=='collect_weread.py' else "if __name__ == '__main__':"
            self.assertIn(marker,text)
            # Test-only hook in the copied file; the repository entry has no fixture mode.
            path.write_text(text.replace(marker,'from fixture_runtime import configure\nconfigure(globals())\n\n'+marker),encoding='utf8')
        (root/'config').mkdir()
        (root/'config/runtime.json').write_text(json.dumps({'native_workflow':'fixture',
            'recipient_open_ids':['owner'],'report_publication_hold':{'active':False}}))
        (root/'fixture.json').write_text(json.dumps({'pipeline':ready_pipeline('2099-01-01'),
            'evidence':complete_evidence(),'workflow':workflow_fixture()}),encoding='utf8')

    def execute(self,root,scenario):
        env={k:v for k,v in os.environ.items() if not k.startswith(('EBOOK_','PYTHONPATH'))}
        env.update(EBOOK_TEST_SCENARIO=scenario,PYTHONIOENCODING='utf-8')
        return subprocess.run([sys.executable,'-I','-X','utf8',str(root/'scripts/daily_run.py'),'--run-kind','scheduled'],
            cwd=root,env=env,capture_output=True,text=True,encoding='utf8',timeout=40)

    def test_full_entry_child_collection_publication_last_and_second_entry_no_duplicates(self):
        with TemporaryDirectory() as d:
            root=Path(d);self.setup_copy(root)
            result=self.execute(root,'success')
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            trace=[json.loads(x) for x in (root/'trace.jsonl').read_text().splitlines()]
            steps=[x['step'] for x in trace]
            self.assertEqual(steps[0],'lock')
            self.assertLess(steps.index('sync_readback'),steps.index('independent_readback'))
            self.assertLess(steps.index('notification_comparison'),steps.index('independent_readback'))
            self.assertLess(steps.index('independent_readback'),steps.index('publish'))
            self.assertEqual(steps[-2:],['checkpoint','unlock'])
            self.assertEqual(len(json.loads((root/'fake-base.json').read_text())['events']),2)
            result=self.execute(root,'success')
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            trace2=[json.loads(x) for x in (root/'trace.jsonl').read_text().splitlines()]
            self.assertEqual(sum(r['step']=='publish' for r in trace2),1)
            self.assertEqual(sum(r['step']=='source_get' for r in trace2),sum(r['step']=='source_get' for r in trace))

    def test_failed_source_readback_copyright_or_roster_never_publishes(self):
        for scenario in ('source_failed','readback_failed','copyright_missing','roster_changed'):
            with self.subTest(scenario=scenario),TemporaryDirectory() as d:
                root=Path(d);self.setup_copy(root);result=self.execute(root,scenario)
                self.assertIn(result.returncode,(1,2),result.stdout+result.stderr)
                trace=[json.loads(x) for x in (root/'trace.jsonl').read_text().splitlines()]
                self.assertFalse(any(x['step']=='publish' for x in trace))
                self.assertEqual(trace[-1]['step'],'unlock')
