"""Register first real observations from a separately archived supplement; no notifications."""
import argparse,json
from datetime import datetime
from pathlib import Path
from feishu_io import Base,ROOT
from fanqie_daily import milestone_step
from daily_run import atomic_json,process_lock


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',required=True)
    a=p.parse_args()
    data=json.loads(Path(a.source).read_text(encoding='utf-8'))
    if data.get('schema_version')!='fanqie.public.v1' or not data.get('finished_at'):
        raise ValueError('Finished actual Fanqie snapshot required')
    api=Base(ROOT/'receipts'/data['collection_date']/('fanqie-baseline-'+datetime.now().strftime('%H%M%S')))
    projects=api.read('项目总表')
    index={str(p['平台作品ID']):p for p in projects if p.get('平台')==['番茄小说'] and p.get('平台作品ID')}
    with process_lock(ROOT/'runs'/'.daily.lock'):
        path=ROOT/'runs/fanqie-milestones.json'
        state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        added=[]
        for book in data['books']:
            project=index.get(str(book['book_id']))
            if not project:
                continue
            registered=str(project.get('首次进入项目总表',''))[:10]
            if not registered:
                continue
            key=str(book['book_id'])+':'+registered
            if key in state:
                continue
            result=milestone_step(book,registered,data['collection_date'])
            if result['events']:
                raise ValueError('First observation must never emit')
            state[key]=result['state']
            added.append(key)
        atomic_json(path,state)
        atomic_json(api.receipt_dir/'baseline-receipt.json',{'source':str(Path(a.source).resolve()),'added':added,'events':0})
    print(json.dumps({'added':len(added),'events':0,'source':a.source},ensure_ascii=False))


if __name__=='__main__':
    main()
