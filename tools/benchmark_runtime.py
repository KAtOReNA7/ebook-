"""Offline state transport benchmark. No credentials, network or business writes.

Runs the actual baseline/current Store and StageStore with Base.ensure against
an in-memory paginated transport. CPU wall time is measured; network cost is
reported only as a model, not as a production SLA measurement.
"""
import argparse
import base64
import copy
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import time
import types
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.feishu_io import Base
from scripts.shared_runtime import Store, TABLE, FIELDS, digest
from scripts.stage_checkpoint import StageStore
from scripts import stage_checkpoint
from scripts.shared_runtime import LEASE_ENV


class Transport(Base):
    def __init__(self):
        self.records={};self.presentation_enabled=False;self._record_cache={}
        self.manifest={'tables':{TABLE:{'field_readback':{'fields':FIELDS}}}}
        self.changes=0;self.reset_counts()

    def reset_counts(self):
        self.counts={'read_calls':0,'full_table_reads':0,'page_calls':0,'write_calls':0,
                     'returned_bytes':0,'written_bytes':0,'written_rows':0}

    def read(self,table,conditions=None, *, filter_logic='and'):
        match = any if filter_logic == 'or' else all
        rows = [row for row in self.records.values() if match(
            row[field] == value if op == '==' else value in row[field]
            for field, op, value in conditions or [])]
        self.counts['read_calls']+=1
        self.counts['full_table_reads']+=int(not conditions)
        self.counts['page_calls']+=max(1,math.ceil(len(rows)/getattr(self,'read_page_size',100)))
        self.counts['returned_bytes']+=len(json.dumps(rows,ensure_ascii=False).encode())
        return copy.deepcopy(rows)

    def call(self,command,table,payload):
        assert command in {'+record-batch-create','+record-batch-update'}
        rows=payload.get('create_records') or list(payload['update_records'].values())
        self.counts['write_calls']+=1
        self.counts['written_rows']+=len(rows)
        self.counts['written_bytes']+=len(json.dumps(payload,ensure_ascii=False).encode())
        for row in rows:self.records[row['状态键']]={**row,'record_id':row['状态键']}
        self.changes+=len(rows)


def baseline_module(revision,name):
    result=subprocess.run(['git','show',f'{revision}:scripts/{name}.py'],cwd=ROOT,
                          capture_output=True,check=True,encoding='utf-8')
    module=types.ModuleType('benchmark_baseline_'+name)
    module.__file__=str(ROOT/'scripts'/f'{name}.py')
    exec(compile(result.stdout,module.__file__,'exec'),module.__dict__)
    return module


def measure(api,operation):
    api.reset_counts();started=time.perf_counter();operation()
    result={**api.counts,'cpu_wall_seconds':round(time.perf_counter()-started,4)}
    # Example scenario only: 1 second roundtrip plus 5 MiB/s transfer.
    result['modeled_network_seconds']=round(result['page_calls']+result['write_calls']+
        (result['returned_bytes']+result['written_bytes'])/(5*1024*1024),3)
    return result


def unchanged_stages(module, checkpoint):
    api=Transport();Store(api)
    with TemporaryDirectory() as d,patch.dict(os.environ,{LEASE_ENV:'synthetic-lease'},clear=True):
        root=Path(d)
        module.start(root,{**checkpoint,'files':{}},{'generation':'synthetic'},'synthetic-lease')
        with patch.object(module,'capture',return_value=checkpoint), \
             patch('scripts.feishu_io.Base',return_value=api),patch('scripts.feishu_io.ROOT',root), \
             patch.object(Store,'discover'):
            module.persist(root,'completed')
            def repeat():
                for name in ('readonly-comparison','readonly-validation'):
                    path,ctx=module.context(root);ctx['stage']=name;module.write(path,ctx)
                    module.persist(root,'completed')
            return measure(api,repeat)


def run(revision='8df98a9',file_count=64,bytes_per_file=32768):
    rng=random.Random(1638)
    files = {}
    for index in range(file_count):
        text = base64.b64encode(rng.randbytes(bytes_per_file)).decode()
        files[f'runs/synthetic-{index}.json'] = {'text':text, 'sha256':digest(text.encode())}
    checkpoint={'schema':'ebook-shared-checkpoint.v8','files':files,'day':'2099-01-01'}
    journal={'schema':'ebook-stage-journal.v1','files':dict(list(files.items())[:file_count//2]),'phase':'completed'}
    seed=Transport();s=Store(seed);s.publish(checkpoint);s.publish({**checkpoint,'version':1})
    t=StageStore(seed);t.publish(journal);t.publish({**journal,'stage':'last'})
    result={'baseline':revision,'synthetic_files':file_count,'source_bytes_per_file':bytes_per_file,
            'table_rows':len(seed.records),'network':'offline; latency/throughput model only','results':{}}
    old_stage=baseline_module(revision,'stage_checkpoint')
    for label,store_cls,stage_cls,module in [('before',baseline_module(revision,'shared_runtime').Store,
                                             old_stage.StageStore,old_stage),
                                            ('after',Store,StageStore,stage_checkpoint)]:
        api=Transport();api.records=copy.deepcopy(seed.records);store=store_cls(api);stage=stage_cls(api)
        result['results'][label]={
            'restore':measure(api,lambda:(store.read(),stage.read())),
            'publish_changed_checkpoint':measure(api,lambda:store.publish({**checkpoint,'version':2})),
            'publish_changed_stage':measure(api,lambda:stage.publish({**journal,'stage':'next'})),
            'two_unchanged_completed_stages':unchanged_stages(module,checkpoint),
        }
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',default='8df98a9');p.add_argument('--output',type=Path)
    args=p.parse_args();result=run(args.baseline)
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(text,encoding='utf-8')
    print(text)
