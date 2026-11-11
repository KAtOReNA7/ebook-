import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.shared_runtime import capture,restore,digest,LEASE_ENV
from scripts.stage_checkpoint import StageStore,StagePersistenceError,delta,reconstruct,run_stage,before_mutation,start,ENV,context,require_settled


class MemoryStageBase:
    def __init__(self):self.records={};self.fail=False;self.changes=0
    def read(self,table,conditions=None):
        rows=list(self.records.values())
        for _,op,key in conditions or []:
            rows=[r for r in rows if (r['状态键']==key if op=='==' else key in r['状态键'])]
        return copy.deepcopy(rows)
    def ensure(self,table,key,rows,conditions=None,update=False):
        if self.fail:raise RuntimeError('connection lost before ack')
        for row in rows:self.records[row[key]]=dict(row);self.changes+=1


class StageCheckpointTests(unittest.TestCase):
    def files(self,root,day,value):
        directory=root/'runs'/day;directory.mkdir(parents=True,exist_ok=True)
        (directory/'weread.json').write_text(json.dumps({'actual_time':value,'books':[{'book_id':'123'}]}))
        return directory

    def test_crash_before_write_restores_frozen_source_and_guard_on_other_machine(self):
        with TemporaryDirectory() as a,TemporaryDirectory() as b:
            a,b=Path(a),Path(b);base=capture(a,'2026-11-10')
            directory=self.files(a,'2026-11-11','10:02:13+08:00')
            (directory/'daily-state.json').write_text('{"phase":"sync_in_progress"}')
            current=capture(a,'2026-11-11');store=StageStore(MemoryStageBase())
            store.publish(delta(base,current,'parent','lease','weread','before_write'))
            recovered=reconstruct(b,base,'parent',store.read()[0]);restore(b,recovered,'2026-11-11')
            self.assertEqual((b/'runs/2026-11-11/weread.json').read_bytes(),(directory/'weread.json').read_bytes())
            self.assertEqual(json.loads((b/'runs/2026-11-11/daily-state.json').read_text())['phase'],'sync_in_progress')
            self.assertTrue(recovered['unresolved_guards'])
            self.assertFalse((b/'config/session.json').exists())

    def test_next_stage_failure_keeps_latest_committed_journal(self):
        api=MemoryStageBase();store=StageStore(api)
        value={'schema':'ebook-stage-journal.v1','phase':'completed'};store.publish(value)
        api.fail=True
        with self.assertRaises(RuntimeError):store.publish({**value,'phase':'before_write'})
        self.assertEqual(store.read()[0],value)

    def test_only_delta_is_stored_and_removed_files_apply(self):
        base={'files':{'runs/a.json':{'text':'large unchanged','sha256':'x'},'runs/b.json':{'text':'old','sha256':'y'}}}
        current={'schema':'ebook-shared-checkpoint.v8','files':{'runs/a.json':base['files']['runs/a.json']}}
        value=delta(base,current,'parent','lease','done','completed')
        self.assertEqual(value['files'],{});self.assertEqual(value['removed'],['runs/b.json'])
        self.assertEqual(reconstruct(Path('.'),base,'parent',value),current)
        with self.assertRaises(StagePersistenceError):reconstruct(Path('.'),base,'other',value)

    def test_corrupt_journal_is_rejected(self):
        api=MemoryStageBase();store=StageStore(api);store.publish({'schema':'ebook-stage-journal.v1'})
        for key,row in api.records.items():
            if key!='stage:HEAD':row['内容']='corrupt'
        with self.assertRaises(Exception):store.read()

    def test_bounded_rotating_slots_and_identical_noop(self):
        api=MemoryStageBase();store=StageStore(api)
        for n in range(20):store.publish({'schema':'ebook-stage-journal.v1','n':n})
        self.assertEqual(len(api.records),3)
        count=api.changes;store.publish({'schema':'ebook-stage-journal.v1','n':19})
        self.assertEqual(api.changes,count)

    def test_no_context_means_no_remote_calls(self):
        with patch.dict(os.environ,{},clear=True),patch('scripts.stage_checkpoint.persist') as persist:
            self.assertEqual(run_stage(Path('.'),'x',lambda:42),42)
            before_mutation(Path('.'),'项目总表');persist.assert_not_called()

    def test_subprocess_context_has_same_lease_and_before_write_once(self):
        with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'lease'},clear=True):
            root=Path(d);base=capture(root,'2026-11-11');start(root,base,{'generation':'g'},'lease')
            calls=[]
            def persist(root,phase):
                path,ctx=context(root);ctx['before_saved']=True;path.write_text(json.dumps(ctx));calls.append(phase)
            with patch('scripts.stage_checkpoint.persist',persist):
                before_mutation(root,'系统运行状态');self.assertEqual(calls,[])
                before_mutation(root,'项目总表');before_mutation(root,'每日数据')
                self.assertEqual(calls,['before_write'])
                run_stage(root,'next',lambda:before_mutation(root,'榜单快照'))
                self.assertEqual(calls,['before_write','before_write','completed'])
            os.environ[LEASE_ENV]='other'
            with self.assertRaises(StagePersistenceError):context(root)

    def test_failed_persistence_stops_function_before_business_write(self):
        with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'lease'},clear=True):
            root=Path(d);start(root,capture(root,'2026-11-11'),{'generation':'g'},'lease')
            writes=[]
            def action():before_mutation(root,'项目总表');writes.append('write')
            with patch('scripts.stage_checkpoint.persist',side_effect=StagePersistenceError('offline')):
                with self.assertRaises(StagePersistenceError):run_stage(root,'weread',action)
            self.assertEqual(writes,[])

    def test_invalid_path_or_checksum_not_restored(self):
        base={'files':{}};value=delta(base,{'files':{'runs/a.json':{'text':'ok','sha256':'bad'}}},'p','l','x','failed')
        with self.assertRaises(StagePersistenceError):reconstruct(Path('.'),base,'p',value)
        value['files']={'../secret.json':{'text':'ok','sha256':digest(b'ok')}}
        with self.assertRaises(ValueError):reconstruct(Path('.'),base,'p',value)

    def test_active_stage_blocks_new_run_until_reconciled(self):
        base={'schema':'ebook-shared-checkpoint.v8','files':{}}
        value=delta(base,base,'parent','lease','x','before_write')
        with self.assertRaises(StagePersistenceError):require_settled(Path('.'),base,'parent',value)
        value['phase']='completed';require_settled(Path('.'),base,'parent',value)
        value['checkpoint_meta']['day']='new-day'
        with self.assertRaises(StagePersistenceError):require_settled(Path('.'),base,'parent',value)
        # The final checkpoint supersedes its old parent. No replay of stages.
        require_settled(Path('.'),base,'new-committed-generation',value)

    def test_unknown_receipt_guard_and_plan_are_both_portable(self):
        with TemporaryDirectory() as d:
            root=Path(d);folder=root/'receipts/2026-11-11/manual';folder.mkdir(parents=True)
            (folder/'batch.apply-state.json').write_text('{"phase":"apply_in_progress"}')
            (folder/'plan.json').write_text('{"updates":{"id":"value"}}')
            snapshot=capture(root,'2026-11-11')
            self.assertIn('receipts/2026-11-11/manual/plan.json',snapshot['files'])
