import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import urllib.error

from scripts.feishu_io import Base
from scripts.run_performance import summarize
from scripts.sampling_reuse import eligible_snapshot, verified_copy
from scripts.shared_runtime import LEASE_ENV, digest, gh
from scripts.stage_checkpoint import start, run_stage, context, before_mutation
from scripts.shared_runtime import capture, restore
from scripts.read_retry import read_get


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        helper = patch("scripts.runner_policy.host_fingerprint", return_value="test-host")
        helper.start(); self.addCleanup(helper.stop)

    def test_documented_file_entry_bootstraps_package_without_pythonpath(self):
        with TemporaryDirectory() as d:
            root=Path(d)
            shutil.copytree(Path(__file__).resolve().parents[1]/'scripts',root/'scripts',
                            ignore=shutil.ignore_patterns('__pycache__'))
            result=subprocess.run([sys.executable,'-I',str(root/'scripts/daily_run.py'),'--help'],
                                  cwd=root,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--run-kind',result.stdout)

    def test_raw_sampling_bytes_survive_cold_restore_and_uncommitted_are_quarantined(self):
        with TemporaryDirectory() as source, TemporaryDirectory() as target:
            rel = Path('runs/2099-01-01/raw-weread-100000/detail.json')
            file = Path(source)/rel; file.parent.mkdir(parents=True)
            file.write_bytes(b'{\r\n"id": 123\r\n}')
            snapshot = capture(source, '2099-01-01')
            stale = Path(target)/'runs/2099-01-01/raw-weread-090000/stale.json'
            stale.parent.mkdir(parents=True); stale.write_text('{}')
            restore(target, snapshot, '2099-01-01')
            self.assertEqual((Path(target)/rel).read_bytes(), file.read_bytes())
            self.assertFalse(stale.exists())
            self.assertEqual(len(list((Path(target)/'backups').rglob('stale.json'))), 1)

    def test_incomplete_status_is_not_reusable_even_after_previous_success(self):
        with TemporaryDirectory() as d, patch.dict(os.environ, {LEASE_ENV:'lease'}, clear=True):
            root=Path(d); start(root,capture(root,'2099-01-01'),{'generation':'g'},'lease')
            with patch('scripts.stage_checkpoint.persist'):
                run_stage(root,'notification_health',lambda:{'status':'ok'})
                _, ctx = context(root)
                path = root/'runs'/ctx['day']/'stage-progress.json'
                self.assertIn('notification_health', json.loads(path.read_text())['completed'])
                for status in ('waiting','read_error','source_review_required','requires_review'):
                    run_stage(root,'notification_health',lambda:{'status':status})
                    self.assertNotIn('notification_health', json.loads(path.read_text())['completed'])

    def test_source_attempt_telemetry_preserves_retry_and_never_records_url(self):
        with TemporaryDirectory() as d, patch.dict(os.environ, {'EBOOK_PERFORMANCE_DIR':d}, clear=True):
            operation=Mock(side_effect=[urllib.error.HTTPError('https://secret.invalid/?token=private',503,'',{},None),(b'abc',200)])
            with patch('scripts.read_retry.time.sleep') as sleep:
                self.assertEqual(read_get(operation),(b'abc',200))
            sleep.assert_called_once_with(1)
            rows=[json.loads(p.read_text()) for p in Path(d).glob('*.json')]
            self.assertEqual(len(rows),2)
            self.assertNotIn('secret.invalid',json.dumps(rows))
            self.assertNotIn('private',json.dumps(rows))
            result=summarize(rows,10)['stages']['coordinator']
            self.assertEqual(result['source_http_calls'],2)
            self.assertEqual(result['source_body_bytes'],3)
            self.assertEqual(result['read_retries'],1)
            self.assertEqual(result['backoff_seconds'],1)

    def test_lost_ack_confirms_entire_batch_without_repeating_write(self):
        with TemporaryDirectory() as d:
            api = Base.__new__(Base)
            api.receipt_dir = Path(d)
            api.presentation_enabled = False
            planned = [{'key': 'a', 'n': 1}, {'key': 'b', 'n': 2}]
            api.call = Mock(side_effect=RuntimeError('lost ACK'))
            api.read = Mock(return_value=copy.deepcopy(planned))
            with patch.dict(os.environ,{LEASE_ENV:'test-lease'}), patch('scripts.runner_policy.enforce_writer') as owner:
                reply = api.confirmed_batch('+record-batch-create', 'T', {'create_records': planned}, 'key', planned)
            self.assertTrue(reply['reconciled']); api.call.assert_called_once(); owner.assert_called_once()
            api.read.return_value = planned[:1]
            with patch.dict(os.environ,{LEASE_ENV:'test-lease'}), patch('scripts.runner_policy.enforce_writer'), self.assertRaisesRegex(RuntimeError, 'unknown'):
                api.confirmed_batch('+record-batch-create', 'T', {'create_records': planned}, 'key', planned)
            self.assertEqual(api.call.call_count, 2)  # One original dispatch per separate attempt, no replay.

    def test_stolen_lease_prevents_readback_continuation(self):
        api = Base.__new__(Base); api.call = Mock(side_effect=RuntimeError('lost'))
        api.read = Mock()
        with patch.dict(os.environ,{LEASE_ENV:'test-lease'}), patch('scripts.runner_policy.enforce_writer', side_effect=PermissionError('wrong lease')):
            with self.assertRaises(PermissionError):
                api.confirmed_batch('+record-batch-create', 'T', {}, 'key', [{'key':'a'}])
        api.read.assert_not_called()

    def test_github_403_not_retried_and_get_503_bounded(self):
        def error(code): return urllib.error.HTTPError('https://api.github.com', code, '', {}, None)
        for status, expected in ((403, 1), (503, 3)):
            opener = Mock(); opener.open.side_effect = error(status)
            with patch('scripts.shared_runtime._github_token', 'test-not-real'), \
                 patch('scripts.shared_runtime.transport_env', return_value={}), \
                 patch('urllib.request.build_opener', return_value=opener), patch('scripts.read_retry.time.sleep'):
                with self.assertRaises(RuntimeError): gh('test/repo', 'ref/heads/test')
            self.assertEqual(opener.open.call_count, expected)
        opener = Mock(); opener.open.side_effect = error(503)
        with patch('scripts.shared_runtime._github_token', 'test-not-real'), \
             patch('scripts.shared_runtime.transport_env', return_value={}), \
             patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaises(RuntimeError): gh('test/repo', 'refs/heads/test', {'sha':'fake'})
        self.assertEqual(opener.open.call_count, 1)

    def test_read_only_preparations_defer_but_mutation_and_failure_flush(self):
        with TemporaryDirectory() as d, patch.dict(os.environ, {LEASE_ENV:'lease'}, clear=True):
            root=Path(d); start(root,capture(root,'2099-01-01'),{'generation':'g'},'lease')
            calls=[]
            def persist(root,phase):
                calls.append(phase)
                path, ctx=context(root)
                if phase=='before_write':ctx['before_saved']=True
                path.write_text(json.dumps(ctx))
            with patch('scripts.stage_checkpoint.persist', persist):
                run_stage(root,'project_notifications',lambda:42)
                run_stage(root,'marketing_expense_notifications',lambda:43)
                self.assertEqual(calls, [])
                run_stage(root,'notification_comparison',lambda:before_mutation(root,'每日数据'))
                self.assertEqual(calls,['before_write','completed'])
                with self.assertRaises(ValueError):
                    run_stage(root,'project_notifications',lambda: (_ for _ in ()).throw(ValueError('fail')))
                self.assertEqual(calls[-1], 'failed')

    def test_sampling_requires_same_execution_day_id_hash_and_metric(self):
        with TemporaryDirectory() as d, patch.dict(os.environ,{LEASE_ENV:'lease'},clear=True):
            root=Path(d); stamp='2099-01-01T10:00:00+08:00'
            metadata=root/'detail.json'; reading=root/'reading.json'
            metadata.write_text(json.dumps({'bookInfo':{'bookId':'123','newRatingCount':8,'newRating':900}}))
            reading.write_text(json.dumps({'books':[{'bookInfo':{'bookId':'123'},'readingCount':4}]}))
            book={'book_id':'123','status':'ok','reviewer_count':8,'recommendation_percent':90.0,
                  'today_reading_count':4, 'memberships':[],
                  'detail_source':{'url':'https://weread.qq.com/web/reader/real','observed_at':stamp,
                    'archive_path':str(metadata),'archive_sha256':digest(metadata.read_bytes())},
                  'today_reading_source':{'source_url':'https://weread.qq.com/web/search/global','observed_at':stamp,
                    'archive_path':str(reading),'response_sha256':digest(reading.read_bytes()),'value':4}}
            snapshot=root/'snapshot.json'
            snapshot.write_text(json.dumps({'collection_date':'2099-01-01','execution_lease_sha256':digest(b'lease'),'books':[book]}))
            self.assertIn('123',eligible_snapshot(snapshot,'2099-01-01'))
            self.assertEqual(eligible_snapshot(snapshot,'2099-01-02'),{})
            with patch.dict(os.environ,{LEASE_ENV:'other'}):self.assertEqual(eligible_snapshot(snapshot,'2099-01-01'),{})
            copied=verified_copy(book,{'book_id':'123'},root/'copied','2099-01-01')
            self.assertIsNotNone(copied)
            self.assertEqual(copied['detail_source']['observed_at'],stamp)
            self.assertEqual(Path(copied['detail_source']['archive_path']).read_bytes(),metadata.read_bytes())
            self.assertIsNone(verified_copy(book,{'book_id':'456'},root/'copied','2099-01-01'))
            changed=copy.deepcopy(book);changed['today_reading_count']=999
            self.assertIsNone(verified_copy(changed,{'book_id':'123'},root/'copied','2099-01-01'))
            metadata.write_text('corrupt')
            self.assertIsNone(verified_copy(book,{'book_id':'123'},root/'copied','2099-01-01'))

    def test_performance_does_not_sum_spans_as_wall_or_cli_as_http(self):
        rows=[{'kind':'cli','stage':'a','seconds':3,'attempt':2,'response_bytes':100},
              {'kind':'span','stage':'a','name':'a','outcome':'failed','seconds':10},
              {'kind':'coordination_http','stage':'a','seconds':2,'attempt':1}]
        result=summarize(rows,12)
        self.assertEqual(result['wall_seconds'],12)
        self.assertEqual(result['stages']['a']['cli_calls'],1)
        self.assertEqual(result['stages']['a']['coordination_http_calls'],1)
        self.assertEqual(result['stages']['a']['read_retries'],1)
