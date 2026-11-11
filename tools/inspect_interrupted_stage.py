"""Read and export interrupted remote evidence, without acquiring/releasing a lease or writing Base."""
import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
import uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.feishu_io import Base,save
from scripts.shared_runtime import Store,Mutex,configuration,safe_path,digest
from scripts.stage_checkpoint import StageStore,reconstruct


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export-evidence',action='store_true',help='Export verified evidence into a new private receipts directory; never restore active caches')
    args=parser.parse_args()
    day=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    output=ROOT/'receipts'/day/('stage-recovery-'+uuid.uuid4().hex)
    api=Base(output);store=Store(api).discover()
    checkpoint,meta=store.read();journal,journal_meta=StageStore(api).read()
    _,_,lock=Mutex(configuration(ROOT)['coordination_repository']).head()
    result={'read_only':True,'business_writes':0,'messages_created':0,'mutex':lock['phase'],
            'base_generation':meta['generation'],'status':'no_interrupted_stage'}
    if journal and journal['parent_generation']==meta['generation']:
        recovered=reconstruct(ROOT,checkpoint,meta['generation'],journal)
        result.update(status='requires_reconciliation',stage=journal['stage'],phase=journal['phase'],
                      unresolved_guards=recovered.get('unresolved_guards',[]),files=len(recovered['files']),
                      lease_matches=lock.get('phase')=='busy' and journal['lease_sha256']==digest(lock.get('token','').encode()))
        if journal['phase']=='completed' and recovered==checkpoint:result['status']='already_committed'
        if args.export_evidence:
            target=output/'evidence'
            for name,value in recovered['files'].items():
                path=safe_path(target,name);path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(value['text'].encode())
            save(output/'reconstructed-checkpoint.json',recovered)
            result['evidence_directory']=str(target)
    save(output/'result.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
