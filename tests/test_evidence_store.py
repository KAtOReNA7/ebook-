import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.evidence_store import EvidenceStore, lease_cache, CACHE_ENV
from scripts.shared_runtime import Store, SCHEMA, digest, LEASE_ENV, restore
from scripts.stage_checkpoint import StageStore, delta, reconstruct
from tests.test_shared_runtime import MemoryBase


def checkpoint(files, day='2099-01-01'):
    return {'schema':SCHEMA, 'day':day, 'source_root':'E:/original', 'unresolved_guards':[],
            'files':{name:{'text':text,'sha256':digest(text.encode())} for name,text in files.items()}}


class CountingBase(MemoryBase):
    def __init__(self): super().__init__(); self.writes=[]; self.reads=[]
    def ensure(self, table, key, rows, conditions=None, update=False):
        self.writes.extend(copy.deepcopy(rows))
        return super().ensure(table,key,rows,conditions,update)
    def read(self,table,conditions=None):
        self.reads.append(conditions)
        return super().read(table,conditions)


class EvidenceTests(unittest.TestCase):
    def test_final_store_merges_child_archives_without_uploading_original_again(self):
        with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'lease'},clear=True):
            os.environ[CACHE_ENV]=lease_cache(Path(d),'lease')
            api=CountingBase();original=Store(api)
            base=checkpoint({'runs/raw.json':'unchanged raw'})
            original.publish(base)
            changed=checkpoint({'runs/raw.json':'unchanged raw','runs/progress.json':'intermediate'})
            StageStore(api).publish(delta(base,changed,'parent','lease','first','completed'))
            original.publish(checkpoint({'runs/raw.json':'unchanged raw','runs/progress.json':'finished'}))
            os.environ.pop(CACHE_ENV)
            cold=Store(api);cold.read()
            self.assertIn(digest(b'intermediate'),cold.evidence.versions['runs/progress.json'])

    def test_changed_small_state_does_not_reupload_raw_and_cold_restore_exact(self):
        api=CountingBase();store=Store(api)
        initial=checkpoint({'runs/raw.json':'raw\r\n' * 10000,'runs/state.json':'{"n":1}'})
        store.publish(initial); prior=len(api.writes)
        updated=checkpoint({'runs/raw.json':'raw\r\n' * 10000,'runs/state.json':'{"n":2}'})
        store.publish(updated)
        packs=[r for r in api.writes[prior:] if r['状态键'].startswith('cas:')]
        self.assertEqual(len(packs),1)
        cold=Store(api); observed,_=cold.read();self.assertEqual(observed,updated)
        with TemporaryDirectory() as folder:
            restore(Path(folder),observed)
            self.assertEqual((Path(folder)/'runs/raw.json').read_bytes(),b'raw\r\n'*10000)
        self.assertTrue(cold.evidence.versions['runs/state.json'][digest(b'{"n":1}')])

    def test_missing_archived_pack_blocks_cold_read(self):
        api=CountingBase();store=Store(api)
        store.publish(checkpoint({'runs/old.json':'old'}))
        old=[k for k in api.rows if k.startswith('cas:')]
        store.publish(checkpoint({'runs/new.json':'new'},'2099-01-02'))
        del api.rows[old[0]]
        with self.assertRaisesRegex(ValueError,'Missing'):Store(api).read()

    def test_corruption_and_duplicate_chunks_fail_closed(self):
        api=CountingBase();store=Store(api);store.publish(checkpoint({'runs/x.json':'x'}))
        key=next(k for k in api.rows if k.startswith('cas:'))
        api.rows[key]['内容']='wrong'
        with self.assertRaisesRegex(ValueError,'checksum'):Store(api).read()

    def test_child_stage_reuses_lease_verified_pack_and_manifest_recovers(self):
        with TemporaryDirectory() as d, patch.dict(os.environ,{LEASE_ENV:'lease'},clear=True):
            os.environ[CACHE_ENV]=lease_cache(d,'lease')
            api=CountingBase();base=checkpoint({'runs/raw.json':'old raw'})
            meta=Store(api).publish(base)
            current=checkpoint({'runs/raw.json':'old raw','runs/state.json':'new'})
            journal=delta(base,current,meta['generation'],'lease','weread','before_write')
            StageStore(api).publish(journal)
            count=sum(r['状态键'].startswith('cas:') for r in api.writes)
            StageStore(api).publish({**journal,'phase':'completed'})
            self.assertEqual(sum(r['状态键'].startswith('cas:') for r in api.writes),count)
            with patch.dict(os.environ,{},clear=True):
                observed=StageStore(api).read()[0]
                self.assertEqual(reconstruct(Path(d),base,meta['generation'],observed),current)

    def test_wrong_cache_lease_and_old_reader_rejected(self):
        with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'original'},clear=True):
            os.environ[CACHE_ENV]=lease_cache(d,'original')
            os.environ[LEASE_ENV]='different'
            with self.assertRaisesRegex(ValueError,'lease'):EvidenceStore(CountingBase())
        api=CountingBase();Store(api).publish(checkpoint({}))
        with patch('scripts.shared_runtime.SCHEMA','ebook-shared-checkpoint.v8'):
            with self.assertRaisesRegex(RuntimeError,'schema'):Store(api).read()

    def test_failed_pack_does_not_advance_head(self):
        api=CountingBase();store=Store(api);meta=store.publish(checkpoint({'runs/a.json':'a'}))
        api.fail_chunks=True
        with self.assertRaises(RuntimeError):store.publish(checkpoint({'runs/a.json':'b'}))
        self.assertEqual(store.head(),meta)

    def test_partial_existing_immutable_pack_is_not_overwritten(self):
        api=CountingBase();e=EvidenceStore(api);text=os.urandom(50000).hex()
        values={digest(text.encode()):text};ref=e.put_pack(values)
        self.assertGreater(ref['chunks'],1)
        del api.rows['cas:'+ref['pack']+':00000']
        count=len(api.writes)
        with self.assertRaisesRegex(ValueError,'Missing'):e.put_pack(values)
        self.assertEqual(len(api.writes),count)
