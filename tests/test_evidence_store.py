import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.evidence_store import EvidenceStore, lease_cache, CACHE_ENV, pack_groups
from scripts.shared_runtime import Store, SCHEMA, digest, LEASE_ENV, restore, chunk_conditions
from scripts.stage_checkpoint import StageStore, delta, reconstruct
from tests.test_shared_runtime import MemoryBase


def checkpoint(files, day='2099-01-01'):
    return {'schema':SCHEMA, 'day':day, 'source_root':'E:/original', 'unresolved_guards':[],
            'files':{name:{'text':text,'sha256':digest(text.encode())} for name,text in files.items()}}


class CountingBase(MemoryBase):
    def __init__(self):
        super().__init__()
        self.writes, self.reads, self.filter_logics = [], [], []

    def call(self, command, table, payload):
        self.writes.extend(copy.deepcopy(payload.get('create_records') or list(payload['update_records'].values())))
        return super().call(command, table, payload)

    def read(self,table,conditions=None, *, filter_logic='and'):
        self.reads.append(conditions)
        self.filter_logics.append(filter_logic)
        return super().read(table,conditions,filter_logic=filter_logic)


class EvidenceTests(unittest.TestCase):
    def test_pack_groups_keep_large_packs_isolated_and_small_batches_in_one_page(self):
        sizes = {'a': 60, 'b': 40, 'c': 101, 'd': 40, 'e': 30, 'f': 31}
        self.assertEqual(list(pack_groups(sizes)), [
            {'a': 60, 'b': 40}, {'c': 101}, {'d': 40, 'e': 30}, {'f': 31}])

    def test_immutable_pack_uses_one_preflight_and_one_post_create_read(self):
        api = CountingBase()
        values = {digest(b'original'):'original'}
        store = EvidenceStore(api)
        ref = store.put_pack(values)
        self.assertEqual(len(api.reads), 2)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(store.put_pack(values), ref)
        self.assertEqual(len(api.reads), 3)
        self.assertEqual(len(api.writes), 1)
        api.rows[next(iter(api.rows))]['内容'] = 'changed remotely'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            store.put_pack(values)
        self.assertEqual(len(api.writes), 1)

    def test_cold_restore_groups_active_and_archived_packs_and_rejects_wrong_rows(self):
        api = CountingBase()
        evidence = EvidenceStore(api)
        paths = {}
        for index in range(11):
            text = f'original {index}\r\n'
            checksum = digest(text.encode())
            paths[f'runs/source-{index}.json'] = {**evidence.put_pack({checksum:text}), 'sha256':checksum}
        active_name = next(iter(paths))
        active = {active_name:paths.pop(active_name)}
        manifest = {'schema':SCHEMA, 'storage':'ebook-content-manifest.v1', 'value':{'schema':SCHEMA},
                    'files':active, 'archive':{name:[ref] for name,ref in paths.items()}}
        api.reads.clear()
        api.filter_logics.clear()
        self.assertEqual(EvidenceStore(api).thaw(manifest)['files'][active_name]['text'], 'original 0\r\n')
        self.assertEqual([len(conditions) for conditions in api.reads], [8, 3])
        self.assertEqual(api.filter_logics, ['or', 'or'])
        archive = next(iter(paths.values()))
        archive_key = 'cas:'+archive['pack']+':00000'
        original = api.rows[archive_key]
        for issue, row in [('duplicate', dict(original, record_id='another')),
                           ('unexpected', dict(original, 状态键=archive_key+'extra'))]:
            with self.subTest(issue=issue):
                api.rows['injected'] = row
                with self.assertRaisesRegex(ValueError, 'duplicate or unexpected'):
                    EvidenceStore(api).thaw(manifest)
                del api.rows['injected']
        del api.rows[archive_key]
        with self.assertRaisesRegex(ValueError, 'Missing'):
            EvidenceStore(api).thaw(manifest)

    def test_cached_pack_skips_identical_disk_writes_and_fresh_read_checks_remote(self):
        with TemporaryDirectory() as d, patch.dict(os.environ, {LEASE_ENV:'lease'}, clear=True):
            os.environ[CACHE_ENV] = lease_cache(d, 'lease')
            api = CountingBase()
            store = EvidenceStore(api)
            values = {digest(b'original'):'original'}
            ref = store.put_pack(values)
            cache = Path(os.environ[CACHE_ENV]) / (ref['pack']+'.json')
            with patch.object(Path, 'write_bytes', side_effect=AssertionError('redundant pack write')):
                self.assertEqual(EvidenceStore(api).pack_raw(ref, fresh=False), values)
                self.assertEqual(store.pack_raw(ref, fresh=True), values)
            cache.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                EvidenceStore(api).pack_raw(ref, fresh=False)
            self.assertEqual(store.pack_raw(ref, fresh=True), values)
            self.assertEqual(digest(cache.read_bytes()), ref['pack'])
            api.rows.clear()
            with self.assertRaisesRegex(ValueError, 'Missing'):
                store.pack_raw(ref, fresh=True)

    def test_manifest_predicate_keeps_every_active_chunk_at_decimal_boundaries(self):
        for count in (1,9,10,11,99,100,101,999,1000,1001,100000):
            prefix=chunk_conditions('slot0:',count)[0][2]
            self.assertTrue(all(f'slot0:{i:05}'.startswith(prefix) for i in range(count)))
        for bad in (0,-1,100001,True,1.5):
            with self.assertRaises(ValueError):chunk_conditions('slot0:',bad)

    def test_obsolete_large_slot_tail_is_retained_without_being_read(self):
        api=CountingBase();value=checkpoint({'runs/raw.json':'original'})
        meta=Store(api).publish(value)
        tail=f"slot{meta['slot']}:00786"
        api.rows[tail]={'状态键':tail,'内容':'obsolete','校验值':digest(b'obsolete'),'record_id':tail}
        journal=delta(value,value,meta['generation'],'lease','source','completed')
        jm=StageStore(api).publish(journal)
        jtail=f"stage:slot{jm['slot']}:00786"
        api.rows[jtail]={'状态键':jtail,'内容':'obsolete','校验值':digest(b'obsolete'),'record_id':jtail}
        api.reads=[]
        self.assertEqual(Store(api).read()[0],value)
        self.assertEqual(StageStore(api).read()[0],journal)
        for conditions in api.reads:
            if conditions and 'slot' in str(conditions):
                self.assertFalse(tail.startswith(conditions[0][2]))
                self.assertFalse(jtail.startswith(conditions[0][2]))
        self.assertIn(tail,api.rows);self.assertIn(jtail,api.rows)

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
