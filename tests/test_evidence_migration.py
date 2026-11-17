import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch,Mock

from tests.test_shared_runtime import MemoryBase
from scripts.shared_runtime import Store,Mutex,digest,SCHEMA
from tools.migrate_evidence_store import migrate,business_snapshot


class MigrationTests(unittest.TestCase):
    def test_business_pages_do_not_inherit_small_evidence_page_limit(self):
        api=Mock();api.manifest={'tables':{'business':{},'系统运行状态':{}}};api.read_page_size=100
        def read(table):
            self.assertEqual(api.read_page_size,2000)
            return [{'record_id':'r'}]
        api.read.side_effect=read;api.call.return_value={'data':{'status':'synthetic'}}
        business_snapshot(api,{'native_workflow':'test'})
        self.assertEqual(api.read_page_size,100)
        api.read.assert_called_once_with('business')
    def test_preview_no_state_mutation_and_conversion_preserves_every_file(self):
        with TemporaryDirectory() as d:
            api=MemoryBase();store=Store(api)
            raw=json.dumps({'native_workflow':'synthetic','recipient_open_ids':['test']})
            old={'schema':'ebook-shared-checkpoint.v8','day':'2099-01-01','unresolved_guards':[],
                 'files':{'config/runtime.json':{'text':raw,'sha256':digest(raw.encode())},
                          'runs/evidence.json':{'text':'original\r\n','sha256':digest(b'original\r\n')}}}
            store.publish(old);records=copy.deepcopy(api.rows);mutex=Mock(spec=Mutex)
            with patch('tools.migrate_evidence_store.business_snapshot',return_value={'business':'unchanged'}):
                result=migrate(api,store,mutex,False,Path(d))
                self.assertEqual(result['status'],'preview');self.assertEqual(api.rows,records)
                result=migrate(api,store,mutex,True,Path(d))
            actual,_=Store(api).read()
            self.assertEqual(actual['schema'],SCHEMA);self.assertEqual(actual['files'],old['files'])
            self.assertEqual(result['notifications_created'],0)
            self.assertEqual(json.loads((Path(d)/'conversion.apply-state.json').read_text())['phase'],'finished')

    def test_business_change_retains_review_guard(self):
        with TemporaryDirectory() as d:
            api=MemoryBase();store=Store(api);raw='{"native_workflow":"test"}'
            store.publish({'schema':'ebook-shared-checkpoint.v8','day':'2099-01-01','files':{
                'config/runtime.json':{'text':raw,'sha256':digest(raw.encode())}}})
            with patch('tools.migrate_evidence_store.business_snapshot',side_effect=[{'n':1},{'n':2}]):
                with self.assertRaisesRegex(RuntimeError,'Business state changed'):
                    migrate(api,store,Mock(spec=Mutex),True,Path(d))
            self.assertEqual(json.loads((Path(d)/'conversion.apply-state.json').read_text())['phase'],'requires_review')
