import copy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs

from scripts import collect_weread as collector
from scripts.weread_progress import Journal,allowed_files
from scripts.shared_runtime import capture,restore

DAY='2099-01-01'
STAMP=DAY+'T10:01:00+08:00'


def fixture_get(url):
    if '/web/reader/' in url:
        bid=url.rsplit('/',1)[1]
        data={'reader':{'bookInfo':{'bookId':bid,'title':'Book '+bid,'author':'Author',
            'category':'fiction','totalWords':123,'copyrightInfo':{'id':'cp','name':'Publisher'},
            'newRating':910,'newRatingCount':50},'readingStat':{'readingCount':999}}}
        body='window.__INITIAL_STATE__='+json.dumps(data)
    else:
        bid=parse_qs(urlparse(url).query)['keyword'][0].split()[-1]
        body=json.dumps({'books':[{'bookInfo':{'bookId':bid,'title':'Book '+bid},'readingCount':27}],'hasMore':False})
    return body,{'url':url,'http_status':200,'observed_at':STAMP,
                 'response_sha256':hashlib.sha256(body.encode()).hexdigest()}


class ProgressTests(unittest.TestCase):
    def test_resume_allowlist_comes_from_reconstructed_state_and_only_current_day(self):
        from scripts.stage_checkpoint import start,context
        from scripts.shared_runtime import LEASE_ENV
        with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'lease'},clear=True), \
             patch('scripts.daily_run.shanghai_day',return_value=DAY):
            root=Path(d)
            base={'day':DAY,'files':{}}
            current={'files':{'runs/'+DAY+'/raw-weread-new/sample-a.json':{'sha256':'new'},
                              'runs/2098-12-31/raw-weread-old/sample-b.json':{'sha256':'old'}}}
            start(root,base,{'generation':'g'},'lease',restored_checkpoint=current)
            _,ctx=context(root)
            self.assertEqual(ctx['sampling_allowed'],{'runs/'+DAY+'/raw-weread-new/sample-a.json':'new'})

    def run_collector(self,root,authorized=None,fail_final=False,fail_reading=False):
        output=root/'runs'/DAY/'weread.collecting.json';output.parent.mkdir(parents=True,exist_ok=True)
        tracked=output.parent/'tracked-projects.json'
        tracked.write_text(json.dumps([{'book_id':b,'title':'Book '+b,
                         'detail_url':'https://weread.qq.com/web/reader/'+b} for b in ('1','2')]))
        real_write=collector.write_json
        def write(path,value):
            if path==output and fail_final:raise RuntimeError('crash at final snapshot')
            return real_write(path,value)
        def get(url):
            if fail_reading and '/search/' in url:raise RuntimeError('reading transport failed')
            return fixture_get(url)
        with patch.dict(os.environ,{},clear=True),patch.object(collector,'ROOT',root), \
             patch.object(collector,'now',return_value=STAMP),patch.object(collector.time,'sleep'), \
             patch.object(collector,'get',side_effect=get) as network, \
             patch.object(collector,'write_json',side_effect=write), \
             patch('scripts.stage_checkpoint.context',return_value=(None,{'sampling_allowed':authorized or {}})), \
             patch('sys.argv',['collect_weread.py','--output',str(output),'--tracked-json',str(tracked),'--no-charts']):
            if fail_final:
                with self.assertRaisesRegex(RuntimeError,'final snapshot'):collector.main()
                return network.call_count,None
            collector.main()
            return network.call_count,json.loads(output.read_text(encoding='utf8'))

    def test_final_snapshot_crash_reuses_both_parts_only_after_checkpoint_restore(self):
        with TemporaryDirectory() as a,TemporaryDirectory() as b:
            source,target=Path(a),Path(b)
            calls,_=self.run_collector(source,fail_final=True)
            self.assertEqual(calls,4)
            saved=capture(source,DAY)
            restore(target,saved,DAY)
            calls,data=self.run_collector(target,allowed_files(saved))
            self.assertEqual(calls,0)
            self.assertEqual(data['sampling_progress']['detail_reused'],2)
            self.assertEqual(data['sampling_progress']['reading_reused'],2)
            for book in data['books']:
                self.assertEqual(book['detail_source']['observed_at'],STAMP)
                self.assertEqual(book['today_reading_source']['observed_at'],STAMP)
                self.assertEqual(book['today_reading_count'],27)
                self.assertTrue(Path(book['detail_source']['archive_path']).resolve().is_relative_to(target.resolve()))

    def test_failed_reading_retries_only_reading_not_successful_details(self):
        with TemporaryDirectory() as d:
            root=Path(d);self.run_collector(root,fail_final=True,fail_reading=True)
            saved=capture(root,DAY)
            calls,data=self.run_collector(root,allowed_files(saved))
            self.assertEqual(calls,2)
            self.assertEqual(data['sampling_progress']['detail_reused'],2)
            self.assertEqual(data['sampling_progress']['reading_reused'],0)

    def test_uncommitted_local_records_never_authorize_reuse(self):
        with TemporaryDirectory() as d:
            root=Path(d);self.run_collector(root,fail_final=True)
            calls,data=self.run_collector(root)
            self.assertEqual(calls,4)
            self.assertEqual(data['sampling_progress']['detail_reused'],0)

    def test_changed_raw_or_index_or_old_day_falls_back_without_guess(self):
        for mode in ('raw','index','day','id','value'):
            with self.subTest(mode=mode),TemporaryDirectory() as d:
                root=Path(d);self.run_collector(root,fail_final=True)
                saved=capture(root,DAY);allowed=allowed_files(saved)
                entry=next(p for p in root.rglob('sample-*.json') if json.loads(p.read_text())['kind']=='detail')
                row=json.loads(entry.read_text());bid=row['book_id']
                if mode=='raw':(root/row['evidence_path']).write_text('{}')
                elif mode=='index':entry.write_text('{}')
                else:
                    if mode=='day':row['day']='2098-12-31'
                    if mode=='id':row['result']['book_id']='999'
                    if mode=='value':row['result']['reviewer_count']=999
                    entry.write_text(json.dumps(row))
                    # Even a checksummed index must reparse and match its raw fields.
                    allowed[entry.relative_to(root).as_posix()]=hashlib.sha256(entry.read_bytes()).hexdigest()
                journal=Journal(root,root/'out',DAY,allowed)
                self.assertIsNone(journal.take('detail',bid,'https://weread.qq.com/web/reader/'+bid))

    def test_on_chart_reading_uses_new_chart_observation_not_old_search(self):
        book={'book_id':'1','memberships':[{'chart_id':'rising'}],
              'today_reading_observations':[{'value':40,'observed_at':STAMP}]}
        journal=unittest.mock.Mock()
        collector.finalize_today_reading(book,Path('unused'),journal)
        journal.take.assert_not_called()
        self.assertEqual(book['today_reading_count'],40)
