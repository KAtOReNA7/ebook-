"""Read one retained evidence version from private shared authority; no active restore."""
import argparse
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.shared_runtime import Store,safe_path,digest
from scripts.feishu_io import Base
from scripts.daily_run import shanghai_day


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path',required=True)
    parser.add_argument('--sha256',required=True)
    args=parser.parse_args()
    destination=ROOT/'receipts'/shanghai_day()/('archive-export-'+uuid.uuid4().hex)
    store=Store(Base(destination)).discover()
    store.read()  # Complete cold verification includes archive availability.
    ref=store.evidence.versions.get(args.path,{}).get(args.sha256)
    if not ref:raise ValueError('Requested evidence version is not referenced by current authority')
    raw=store.evidence.pack_raw(ref,fresh=True)[args.sha256].encode()
    if digest(raw)!=args.sha256:raise ValueError('Archive hash mismatch')
    path=safe_path(destination/'evidence',args.path)
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    print(path)


if __name__=='__main__':main()
