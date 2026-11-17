"""Offline v8/v9 comparison with identical synthetic raw evidence and stage changes."""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import random
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.benchmark_runtime import Transport, baseline_module, measure
from scripts.shared_runtime import Store, SCHEMA, digest, LEASE_ENV
from scripts.stage_checkpoint import StageStore, SCHEMA as STAGE_SCHEMA
from scripts.evidence_store import CACHE_ENV, lease_cache


def run(baseline='71fb304', files=64, bytes_per_file=65536, stages=12):
    rng=random.Random(1711)
    content={f'runs/synthetic-{i}.json':base64.b64encode(rng.randbytes(bytes_per_file)).decode() for i in range(files)}
    raw={k:{'text':v,'sha256':digest(v.encode())} for k,v in content.items()}
    before_store=baseline_module(baseline,'shared_runtime').Store
    before_stage=baseline_module(baseline,'stage_checkpoint').StageStore
    result={'baseline':baseline,'raw_file_count':files,'raw_total_bytes':sum(len(v.encode()) for v in content.values()),
            'stages':stages,'measurement':'offline CPU and in-memory transport counts; not production timing','results':{}}
    for name,store_class,stage_class,schema,journal_schema in (
        ('before',before_store,before_stage,'ebook-shared-checkpoint.v8','ebook-stage-journal.v1'),
        ('after',Store,StageStore,SCHEMA,STAGE_SCHEMA)):
        with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'synthetic-benchmark'},clear=True):
            if name=='after':os.environ[CACHE_ENV]=lease_cache(Path(d),'synthetic-benchmark')
            api=Transport();store=store_class(api)
            value={'schema':schema,'files':raw,'day':'2099-01-01'}
            initial=measure(api,lambda:store.publish(value))
            def phases():
                for i in range(stages):
                    # New instances model separate child processes sharing one lease cache.
                    stage=stage_class(api)
                    text=json.dumps({'completed_stage':i})
                    stage.publish({'schema':journal_schema,'phase':'completed','stage':str(i),
                        'files':{**raw,'runs/progress.json':{'text':text,'sha256':digest(text.encode())}}})
            changed=measure(api,phases)
            final={**value,'files':{**raw,'runs/progress.json':{'text':'finished','sha256':digest(b'finished')}}}
            final_commit=measure(api,lambda:store.publish(final))
            # A genuinely empty-cache instance verifies both current and archived evidence.
            os.environ.pop(CACHE_ENV,None)
            cold=measure(api,lambda:store_class(api).read())
            result['results'][name]={'initial_conversion':initial,'changing_stages':changed,
                                     'final_commit':final_commit,'cold_restore':cold}
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=run()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
