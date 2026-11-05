"""Build a private, read-only coverage report and reviewed identity import map.

Inputs are archived project rows, observed discovery candidates, collector
observations and visual reviews. This command never writes Base or infers IDs.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

try:
    from .resolve_fanqie_projects import choose, validate_review
    from .sync_marketing import normalize_title
    from .collect_fanqie import now, write_json
except ImportError:
    from resolve_fanqie_projects import choose, validate_review
    from sync_marketing import normalize_title
    from collect_fanqie import now, write_json


def build(projects, candidates, observations, reviews):
    keys = [p.get('作品唯一键') for p in projects]
    if not all(keys) or len(keys) != len(set(keys)):
        raise ValueError('Project keys missing or duplicate')
    review_index = {}
    for review in reviews:
        validate_review(review)
        title = review['source_title']
        if title in review_index:
            raise ValueError('Duplicate source review')
        review_index[title] = review
    books = {}
    for book in observations:
        bid = book['book_id']
        if (not isinstance(bid, str) or not re.fullmatch(r'\d{10,25}', bid)
                or book.get('detail_url') != 'https://fanqienovel.com/page/'+bid
                or not book.get('detail_source', {}).get('observed_at')):
            raise ValueError('Expected an archived official Fanqie observation')
        if bid in books and books[bid] != book:
            raise ValueError('Conflicting observations; select one evidence set per ID')
        books[bid] = book
    discovery = {}
    for candidate in candidates:
        if (not candidate.get('discovery_sources') or not isinstance(candidate.get('book_id'), str)
                or not re.fullmatch(r'\d{10,25}', candidate['book_id'])):
            raise ValueError('Candidate requires observed discovery sources')
        discovery.setdefault(candidate['source_title'], {})[candidate['book_id']] = candidate
    rows, identities = [], []
    for project in projects:
        key, title = project['作品唯一键'], project['书名']
        if project.get('作品形式') != '电子书':
            raise ValueError('Coverage scope must contain confirmed ebooks only')
        if key.startswith('fanqie:') and key == 'fanqie:' + str(project.get('平台作品ID')):
            rows.append({'source_title': title, 'record_id': project['record_id'],
                         'status': 'already_linked', 'book_id': project['平台作品ID']})
            continue
        if not key.startswith('pending:fanqie:ebook:'):
            raise ValueError('Unexpected project scope or identity')
        group = discovery.get(title, {})
        available = [books[bid] for bid in group if bid in books]
        review = review_index.get(title)
        result = choose(title, available, review)
        if result['status'] == 'confirmed' and any(bid not in books for bid in group):
            result.update(status='unresolved', reason='other_candidate_observation_missing')
        # Existing canonical projects and multiple pending aliases require the
        # separate reviewed merge workflow, never implicit rekeying.
        if result['status'] == 'confirmed' and 'fanqie:' + result['book_id'] in keys:
            result.update(status='requires_merge', reason='canonical_target_exists')
        result.update(source_title=title, record_id=project['record_id'], source_key=key,
                      discovery=list(group.values()), source_review=review,
                      unavailable_candidate_ids=[bid for bid in group if bid not in books])
        rows.append(result)
    target_counts = {}
    for row in rows:
        if row['status'] == 'confirmed':
            target_counts[row['book_id']] = target_counts.get(row['book_id'], 0) + 1
    for row in rows:
        if row['status'] != 'confirmed':
            continue
        if target_counts[row['book_id']] > 1:
            row.update(status='requires_merge', reason='multiple_pending_aliases_same_id')
            continue
        book = books[row['book_id']]
        identities.append({'platform': 'fanqie', 'medium': 'ebook', 'verified': True,
                           'source_title': row['source_title'],
                           'normalized_title': normalize_title(row['source_title']),
                           'book_id': book['book_id'], 'title': book['title'],
                           'author': book.get('author'), 'detail_url': book['detail_url'],
                           'matching_note': row['match_basis'],
                           'evidence': {'source_review': row['source_review'],
                                        'discovery': row['discovery'],
                                        'detail_source': book['detail_source']}})
    counts = {status: sum(r['status'] == status for r in rows) for status in
              ('already_linked', 'confirmed', 'ambiguous', 'requires_merge', 'unresolved')}
    return {'observed_at': now(), 'projects': len(projects), 'counts': counts,
            'base_writes': 0, 'rows': rows}, {'schema_version': 'project.identities.v1', 'rows': identities}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('projects', 'candidates', 'source-reviews', 'output-dir'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--observations', required=True, action='append', type=Path)
    args = parser.parse_args()
    def read(path):
        return json.loads(path.read_text(encoding='utf-8-sig'))
    report, identities = build(read(args.projects), read(args.candidates),
                               [b for p in args.observations for b in read(p)], read(args.source_reviews))
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report['inputs'] = [{'path': str(p.resolve()), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                        for p in [args.projects, args.candidates, args.source_reviews, *args.observations]]
    identities['mapping_source'] = str((args.output_dir/'coverage.json').resolve())
    write_json(args.output_dir/'coverage.json', report)
    write_json(args.output_dir/'identities.json', identities)
    lines = ['# 番茄营销作品核验', '',
             '仅本地证据核验；未修改飞书，未把历史截图数值用作当前指标。', '',
             '| 作品 | 结果 | 原因 |', '| --- | --- | --- |']
    lines += ['| ' + ' | '.join(str(r.get(k, '')).replace('|', '／') for k in
                               ('source_title', 'status', 'reason')) + ' |' for r in report['rows']]
    (args.output_dir/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({'projects': report['projects'], 'counts': report['counts'],
                      'output': str(args.output_dir.resolve()), 'base_writes': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
