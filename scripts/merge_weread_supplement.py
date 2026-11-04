"""Combine independently archived observations without rewriting original evidence."""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('Observation timestamp must include timezone')
    return parsed


def book_time(book, snapshot):
    return timestamp(book.get('detail_source', {}).get('observed_at') or
                     book.get('observed_at') or snapshot['finished_at'])


def chart_time(chart, snapshot):
    observations = [r['observed_at'] for r in chart.get('requests', []) if r.get('observed_at')]
    return max(map(timestamp, observations)) if observations else timestamp(snapshot['started_at'])


def merge(original, supplements):
    result = dict(original)
    charts = {c['chart_id']: c for c in original['charts']}
    books = {str(b['book_id']): b for b in original['books']}
    chart_times = {c['chart_id']: chart_time(c, original) for c in original['charts']}
    book_times = {str(b['book_id']): book_time(b, original) for b in original['books']}
    for addition in supplements:
        if addition['collection_date'] != original['collection_date']:
            raise ValueError('Supplement date differs')
        for chart in addition['charts']:
            if not chart['complete_top50']:
                raise ValueError('Supplement must contain complete charts')
            cid, observed_at = chart['chart_id'], chart_time(chart, addition)
            if cid not in charts or not charts[cid]['complete_top50'] or observed_at > chart_times[cid]:
                charts[cid], chart_times[cid] = chart, observed_at
        for book in addition['books']:
            bid, observed_at = str(book['book_id']), book_time(book, addition)
            if bid not in books or observed_at > book_times[bid]:
                books[bid], book_times[bid] = book, observed_at
    if 'newbook_novel_female' in charts:
        charts.pop('newbook_novel_female_store', None)
    result.update(charts=list(charts.values()), books=list(books.values()),
                  started_at=min([original['started_at']] + [s['started_at'] for s in supplements], key=timestamp),
                  finished_at=max([original['finished_at']] + [s['finished_at'] for s in supplements], key=timestamp),
                  run_kind='composite_observations',
                  gaps=[g for g in original.get('gaps', []) if 'newbook_novel_female_store' not in g],
                  composite_note='保留原始采集文件；组合不同实际采集时间的观察，用于后续日间比较，不声称全部在10点采集。')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--original', required=True)
    parser.add_argument('--supplement', action='append', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    paths = [Path(args.original), *map(Path, args.supplement)]
    values = [json.loads(p.read_text(encoding='utf-8')) for p in paths]
    result = merge(values[0], values[1:])
    result['component_evidence'] = [{'path':str(p.resolve()), 'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                                    'started_at':v['started_at'], 'finished_at':v['finished_at']} for p,v in zip(paths,values)]
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'books':len(result['books']), 'complete_boards':sum(c['complete_top50'] for c in result['charts']),
                      'output':args.output},ensure_ascii=False))


if __name__ == '__main__':
    main()
