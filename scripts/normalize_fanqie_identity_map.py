"""Translate evidence-reviewed Fanqie matches to the shared source identity contract."""
import argparse,json
from pathlib import Path
from sync_marketing import normalize_title


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--input',required=True)
    p.add_argument('--source',required=True)
    p.add_argument('--output',required=True)
    a=p.parse_args()
    data=json.loads(Path(a.input).read_text(encoding='utf-8'))
    source=json.loads(Path(a.source).read_text(encoding='utf-8'))
    source_index={r['source_coordinate_key']:r for r in source['rows']}
    rows=[]
    for mapping in data['mappings']:
        if mapping.get('status')!='confirmed':
            continue
        candidate=next(c for c in mapping['candidates'] if str(c['book_id'])==str(mapping['book_id']))
        media={source_index[key]['normalized']['service_medium'] for key in mapping['source_row_keys']}
        for medium in media:
            rows.append({'platform':'fanqie','medium':medium,'verified':True,'source_title':mapping['source_title'],
                         'normalized_title':normalize_title(mapping['source_title']),**candidate,
                         'matching_note':mapping['match_basis'],'source_author_unavailable':mapping['source_author_unavailable'],
                         'evidence':mapping['source_identity_review'],'mapping_source':str(Path(a.input).resolve())})
    result={'schema_version':'project.identities.v1','rows':rows,'source':str(Path(a.input).resolve())}
    Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'verified':len(rows),'output':a.output},ensure_ascii=False))


if __name__=='__main__':
    main()
