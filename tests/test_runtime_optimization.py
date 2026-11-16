import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import subprocess
import urllib.error

from scripts.shared_runtime import Store, capture, LEASE_ENV
from scripts.stage_checkpoint import StageStore, start, persist, context
from scripts.read_retry import read_get
from scripts.coverage import weread_coverage
from tests.test_shared_runtime import MemoryBase
from tests.test_stage_checkpoint import MemoryStageBase
from tests import test_report_publication as publication_tests


class RuntimeOptimizationTests(unittest.TestCase):
    def cli_attempts(self,command,effects,payload=None):
        from scripts.feishu_io import Base
        with TemporaryDirectory() as d:
            root=Path(d);api=object.__new__(Base)
            api.receipt_dir=root;api.token='synthetic';api.changes=0;api.presentation_enabled=False
            api._scope_check=Mock()
            with patch('scripts.feishu_io.ROOT',root),patch('scripts.feishu_io.resolve_cli',return_value='mock-cli'), \
                 patch('scripts.shared_runtime.transport_env',return_value={}), \
                 patch('scripts.runner_policy.enforce_writer'),patch('scripts.stage_checkpoint.before_mutation'), \
                 patch('scripts.feishu_io.time.sleep'),patch('scripts.feishu_io.subprocess.run',side_effect=effects) as run:
                error=None
                try:api.call(command,payload=payload)
                except (RuntimeError,subprocess.TimeoutExpired) as exc:error=exc
                return run.call_count,error,len(list(root.glob('*-PENDING.json')))

    def test_cli_transient_read_retries_without_replaying_write(self):
        failed=SimpleNamespace(returncode=1,stdout='',stderr='TLS handshake timeout')
        success=SimpleNamespace(returncode=0,stdout='{"ok":true}',stderr='')
        count,error,pending=self.cli_attempts('+table-list',[failed,success])
        self.assertEqual((count,error,pending),(2,None,0))
        count,error,pending=self.cli_attempts('+record-batch-create',[failed],{'create_records':[{}]})
        self.assertEqual((count,pending),(1,1));self.assertIsInstance(error,RuntimeError)

    def test_cli_read_timeout_bounded_and_permission_error_not_retried(self):
        count,error,_=self.cli_attempts('+table-list',[subprocess.TimeoutExpired('mock',180)]*3)
        self.assertEqual(count,3);self.assertIsInstance(error,subprocess.TimeoutExpired)
        count,error,_=self.cli_attempts('+table-list',[SimpleNamespace(returncode=1,stdout='',stderr='permission denied')])
        self.assertEqual(count,1)

    def test_read_retry_keeps_failed_artifact_and_returns_successful_one(self):
        from scripts.feishu_io import Base
        with TemporaryDirectory() as d:
            root=Path(d);api=object.__new__(Base);api.receipt_dir=root;api.token='synthetic'
            calls=[]
            def invoke(args,**kwargs):
                output=root/args[args.index('--output')+1];calls.append(output)
                output.write_text('partial' if len(calls)==1 else '{"record_id":"ok"}',encoding='utf-8')
                return SimpleNamespace(returncode=1 if len(calls)==1 else 0,
                    stdout='' if len(calls)==1 else '{"ok":true}',stderr='timeout' if len(calls)==1 else '')
            with patch('scripts.feishu_io.ROOT',root),patch('scripts.feishu_io.resolve_cli',return_value='mock-cli'), \
                 patch('scripts.shared_runtime.transport_env',return_value={}), \
                 patch('scripts.feishu_io.time.sleep'),patch('scripts.feishu_io.subprocess.run',side_effect=invoke):
                api.call('+record-list',output='records.ndjson')
            self.assertNotEqual(calls[0],calls[1])
            self.assertEqual((root/'records.ndjson').read_text(),'{"record_id":"ok"}')
            self.assertEqual((root/'records.ndjson.before-retry-1').read_text(),'partial')

    def test_shared_head_never_reads_full_table(self):
        api=MemoryBase();store=Store(api)
        value={'schema':'ebook-shared-checkpoint.v8','files':{}}
        store.publish(value)
        original=api.read;calls=[]
        def read(table,conditions=None):
            self.assertTrue(conditions)
            calls.append(conditions)
            return original(table,conditions)
        api.read=read
        self.assertEqual(store.read()[0],value)
        self.assertEqual(len(calls),3)
        self.assertEqual(calls[0],calls[2])
        self.assertEqual(calls[0][0][2],'HEAD')

    def test_pointer_changed_mid_read_is_rejected(self):
        api=MemoryBase();store=Store(api)
        store.publish({'schema':'ebook-shared-checkpoint.v8','files':{}})
        meta=store.head()
        with patch.object(store,'head',side_effect=[meta,{**meta,'generation':'changed'}]):
            with self.assertRaisesRegex(RuntimeError,'changed during read'):store.read()

    def test_stage_noop_still_checks_payload_not_just_pointer_hash(self):
        api=MemoryStageBase();store=StageStore(api);value={'schema':'ebook-stage-journal.v1'}
        store.publish(value)
        api.records['stage:slot0:00000']['内容']='corrupt'
        with self.assertRaises(RuntimeError):store.publish(value)

    def test_completed_unchanged_stage_skips_remote_but_before_write_does_not(self):
        with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'lease'},clear=True):
            root=Path(d);start(root,capture(root,'2026-11-16'),{'generation':'g'},'lease')
            with patch('scripts.feishu_io.Base'),patch('scripts.shared_runtime.Store'), \
                 patch('scripts.stage_checkpoint.StageStore') as store:
                persist(root,'completed')
                persist(root,'completed')
                self.assertEqual(store.return_value.publish.call_count,1)
                persist(root,'before_write')
                persist(root,'completed')
                self.assertEqual(store.return_value.publish.call_count,3)
                self.assertEqual(context(root)[1]['unchanged_stages'],1)

    def test_changed_file_and_failure_are_never_skipped(self):
        with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'lease'},clear=True):
            root=Path(d);start(root,capture(root,'2026-11-16'),{'generation':'g'},'lease')
            with patch('scripts.feishu_io.Base'),patch('scripts.shared_runtime.Store'), \
                 patch('scripts.stage_checkpoint.StageStore') as store:
                persist(root,'completed')
                (root/'config').mkdir()
                (root/'config/runtime.json').write_text('{"changed":true}')
                persist(root,'completed');persist(root,'failed')
                self.assertEqual(store.return_value.publish.call_count,3)

    def test_get_retry_is_bounded_and_only_repeats_failed_request(self):
        good=Mock(return_value='good');bad=Mock(side_effect=[TimeoutError(), 'recovered'])
        with patch('scripts.read_retry.time.sleep'):
            self.assertEqual([read_get(good),read_get(bad)],['good','recovered'])
            self.assertEqual(good.call_count,1);self.assertEqual(bad.call_count,2)
            bad=Mock(side_effect=TimeoutError())
            with self.assertRaises(TimeoutError):read_get(bad)
            self.assertEqual(bad.call_count,3)
            bad=Mock(side_effect=urllib.error.HTTPError('https://test',403,'forbidden',{},None))
            with self.assertRaises(urllib.error.HTTPError):read_get(bad)
            self.assertEqual(bad.call_count,1)
            bad.side_effect.close()

    def test_metric_network_failure_blocks_both_reports(self):
        snapshot={'books':[{'book_id':'1','status':'ok','today_reading_search':{'requests':[{'error':'timeout'}]}}]}
        c=weread_coverage(snapshot,[{'book_id':'1'}],[])
        self.assertEqual(c['failed_metric_book_ids'],['1'])
        p=publication_tests.ready_pipeline();p['modules']['weread']['coverage']['failed_metric_book_ids']=['1']
        result,calls=publication_tests.ReportPublicationTests().run_flush(p)
        self.assertEqual(calls,0);self.assertEqual(result['status'],'requires_review')

    def test_legitimate_unrated_or_missing_label_is_not_network_failure(self):
        snapshot={'books':[{'book_id':'1','status':'ok','today_reading_search':{'requests':[{'http_status':200}]}}]}
        self.assertEqual(weread_coverage(snapshot,[{'book_id':'1'}],[])['failed_metric_book_ids'],[])

    def test_ranking_reuse_preserves_time_raw_bytes_and_rejects_old_day(self):
        from scripts.fanqie_rising_daily import reuse_snapshot,verified_snapshot
        from tests.test_fanqie_rising_daily import freeze
        with TemporaryDirectory() as d:
            root=Path(d);candidate,data=freeze(root/'attempt','2026-11-16')
            source=root/'runs/2026-11-16/fanqie-rising/snapshot.json'
            reuse_snapshot(candidate,source,'2026-11-16',root)
            self.assertEqual(verified_snapshot(source,'2026-11-16'),data)
            self.assertEqual(source.with_name('ranking.json').read_bytes(),candidate.with_name('ranking.json').read_bytes())
            with self.assertRaises(ValueError):reuse_snapshot(candidate,source,'2026-11-17',root)
            other,_=freeze(root/'other','2026-11-16',replace_first=True)
            with self.assertRaises(ValueError):reuse_snapshot(other,source,'2026-11-16',root)

    def test_fanqie_attempt_does_not_overwrite_interrupted_directory(self):
        from scripts import fanqie_daily
        with TemporaryDirectory() as d:
            root=Path(d);directory=root/'runs/2026-11-16';old=directory/'fanqie/old.json'
            old.parent.mkdir(parents=True);old.write_text('original partial evidence')
            payload={'schema_version':'fanqie.public.v1','collection_date':'2026-11-16',
                     'finished_at':'2026-11-16T11:00:00+08:00','books':[{'book_id':'123'}],'errors':[]}
            def collect(name,args,log):
                target=Path(args[1]);target.mkdir()
                (target/'snapshot.json').write_text(json.dumps(payload))
                (target/'raw.json').write_text('raw source')
            with patch.object(fanqie_daily,'call_script',side_effect=collect) as call:
                source=fanqie_daily.collect_frozen(directory,[{'平台作品ID':'123'}],'2026-11-16')
                self.assertEqual(json.loads(source.read_text()),payload)
                fanqie_daily.collect_frozen(directory,[{'平台作品ID':'123'}],'2026-11-16')
                self.assertEqual(call.call_count,1)
            self.assertEqual(old.read_text(),'original partial evidence')
            files=capture(root,'2026-11-16')['files']
            self.assertTrue(any(name.endswith('/raw.json') and data['text']=='raw source' for name,data in files.items()))
