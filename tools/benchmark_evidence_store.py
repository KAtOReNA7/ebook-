"""Compare checkpoint transport offline using identical synthetic evidence and stage changes."""
import argparse
import base64
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
from scripts.evidence_store import CACHE_ENV, EvidenceStore, lease_cache


def run(baseline='71fb304', files=64, bytes_per_file=65536, stages=12, *, same_v9=False):
    rng=random.Random(1711)
    content={f'runs/synthetic-{i}.json':base64.b64encode(rng.randbytes(bytes_per_file)).decode() for i in range(files)}
    raw={k:{'text':v,'sha256':digest(v.encode())} for k,v in content.items()}
    before_store=baseline_module(baseline,'shared_runtime').Store
    before_stage=baseline_module(baseline,'stage_checkpoint').StageStore
    before_evidence = baseline_module(baseline, 'evidence_store').EvidenceStore if same_v9 else EvidenceStore
    result={'baseline':baseline,'raw_file_count':files,'raw_total_bytes':sum(len(v.encode()) for v in content.values()),
            'stages':stages,'measurement':'offline CPU and in-memory transport counts; not production timing',
            'same_v9':same_v9,'results':{}}
    for name,store_class,stage_class,evidence_class,schema,journal_schema in (
        ('before',before_store,before_stage,before_evidence,
         SCHEMA if same_v9 else 'ebook-shared-checkpoint.v8', STAGE_SCHEMA if same_v9 else 'ebook-stage-journal.v1'),
        ('after',Store,StageStore,EvidenceStore,SCHEMA,STAGE_SCHEMA)):
        with (TemporaryDirectory() as d, patch.dict(os.environ,{LEASE_ENV:'synthetic-benchmark'},clear=True),
              patch('scripts.evidence_store.EvidenceStore', evidence_class)):
            if name=='after' or same_v9:os.environ[CACHE_ENV]=lease_cache(Path(d),'synthetic-benchmark')
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
            def cold_read():
                actual, _ = store_class(api).read()
                if actual != final:
                    raise AssertionError('Cold read altered the synthetic checkpoint')
            cold=measure(api,cold_read)
            result['results'][name]={'initial_conversion':initial,'changing_stages':changed,
                                     'final_commit':final_commit,'cold_restore':cold}
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--baseline', default='71fb304')
    parser.add_argument('--same-v9', action='store_true', help='Compare against a baseline that already writes v9')
    args=parser.parse_args();result=run(args.baseline, same_v9=args.same_v9)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
