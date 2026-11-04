"""Select latest real per-book observations while retaining archived component evidence."""
import argparse,hashlib,json
from pathlib import Path


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',action='append',required=True)
    p.add_argument('--output',required=True)
    a=p.parse_args()
    values=[json.loads(Path(s).read_text(encoding='utf-8')) for s in a.source]
    days={v['collection_date'] for v in values}
    if len(days)!=1:
        raise ValueError('Only combine observations from the same Shanghai day')
    books={}
    for value in values:
        for book in value['books']:
            bid=str(book['book_id'])
            if bid not in books or book['detail_source']['observed_at']>books[bid]['detail_source']['observed_at']:
                books[bid]=book
    result={**values[-1],'books':list(books.values()),'run_kind':'composite_observations',
            'started_at':min(b['detail_source']['observed_at'] for b in books.values()),
            'finished_at':max(b['detail_source']['observed_at'] for b in books.values()),
            'component_evidence':[{'path':str(Path(s).resolve()),'sha256':hashlib.sha256(Path(s).read_bytes()).hexdigest()} for s in a.source]}
    Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'books':len(books),'output':a.output},ensure_ascii=False))


if __name__=='__main__':
    main()
