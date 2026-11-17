"""Controlled v8 -> v9 private-state conversion. Default is locked preview only."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.daily_run import atomic_json, process_lock, shanghai_day
from scripts.evidence_store import CACHE_ENV, lease_cache
from scripts.feishu_io import Base
from scripts.shared_runtime import Mutex, Store, SCHEMA, LEASE_ENV, configuration, digest, commit_checkpoint
from scripts.stage_checkpoint import StageStore, require_settled


def business_snapshot(api, runtime):
    tables = [name for name in api.manifest['tables'] if name != '系统运行状态']
    # Include every business table, including notification state, independent of
    # which tables happened to change in the latest daily run.
    rows = {name: sorted(api.read(name), key=lambda r:r['record_id']) for name in sorted(tables)}
    rows['__workflow__'] = api.call('+workflow-get',workflow_id=runtime['native_workflow'])['data']
    return rows


def migrate(api, store, mutex, execute, receipt):
    original, meta = store.read()
    journal, _ = StageStore(api).read()
    require_settled(ROOT, original, meta['generation'], journal)
    if original.get('unresolved_guards'):
        raise RuntimeError('Unresolved mutation blocks conversion')
    if original['schema'] == SCHEMA:
        return {'status':'already_current','business_writes':0,'state_writes':0}
    if original['schema'] != 'ebook-shared-checkpoint.v8':
        raise RuntimeError('Only reviewed v8 authority may be converted')
    runtime=json.loads(original['files']['config/runtime.json']['text'])
    before=business_snapshot(api,runtime)
    atomic_json(receipt/'business-before.json',before)
    target=copy.deepcopy(original);target['schema']=SCHEMA
    result={'status':'preview','source_generation':meta['generation'],'from_schema':original['schema'],
            'to_schema':SCHEMA,'files':len(target['files']),
            'raw_bytes':sum(len(v['text'].encode()) for v in target['files'].values()),
            'business_writes':0,'notifications_created':0}
    atomic_json(receipt/'preview.json',result)
    if not execute:return result
    atomic_json(receipt/'conversion.apply-state.json',{'phase':'requires_review','source_generation':meta['generation']})
    saved=commit_checkpoint(store,target,mutex)
    cold=Store(api)
    actual, observed=cold.read()
    if actual != target or observed != saved or actual['files'] != original['files']:
        raise RuntimeError('Converted state failed complete independent verification')
    after=business_snapshot(api,runtime)
    atomic_json(receipt/'business-after.json',after)
    if before != after:
        raise RuntimeError('Business state changed during evidence-only conversion')
    result.update(status='completed',generation=saved['generation'],
                  original_bytes_verified=True,business_readback_unchanged=True)
    atomic_json(receipt/'conversion.apply-state.json',{'phase':'finished',**result})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    receipt=ROOT/'receipts'/shanghai_day()/('evidence-conversion-'+uuid.uuid4().hex)
    receipt.mkdir(parents=True)
    from scripts.initialize_manual_projects import unknown_guard_paths
    if unknown_guard_paths(ROOT):raise RuntimeError('Local unfinished mutation requires reconciliation')
    mutex=Mutex(configuration(ROOT)['coordination_repository'],receipt_path=receipt/'mutex.json')
    previous={key:os.environ.get(key) for key in (LEASE_ENV,CACHE_ENV)}
    with process_lock(ROOT/'runs/.daily.lock'):
        lease=mutex.acquire()
        os.environ[LEASE_ENV]=lease;os.environ[CACHE_ENV]=lease_cache(ROOT,lease)
        try:
            api=Base(receipt);store=Store(api).discover()
            result=migrate(api,store,mutex,args.execute,receipt)
            mutex.release()
            atomic_json(receipt/'result.json',result)
            print(json.dumps({**result,'receipt':str(receipt)},ensure_ascii=False))
        except BaseException:
            # Preview never mutates private shared state. A conversion whose
            # guard exists retains its exact lease for read-only reconciliation.
            if not (receipt/'conversion.apply-state.json').exists():mutex.release()
            raise
        finally:
            for key,value in previous.items():
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value


if __name__=='__main__':main()
