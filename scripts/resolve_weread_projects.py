"""Resolve marketing ebook titles against observed official search results."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import urllib.parse
from collect_weread import get, detail_url, now

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'research/weread-project-identities-2026-11-03'


def normalize_title(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value))).strip('《》')


def resolve(title):
    slug = hashlib.sha256(title.encode()).hexdigest()[:16]
    output = {'platform':'weread','medium':'ebook','source_title':title,'normalized_title':normalize_title(title),
              'status':'unresolved','candidates':[], 'requests':[]}
    try:
        offset = 0
        candidates = {}
        ended = False
        for page in range(5):
            url = 'https://weread.qq.com/web/search/global?' + urllib.parse.urlencode(
                {'keyword':title,'maxIdx':offset,'fragmentSize':120,'count':20})
            body, evidence = get(url)
            path = DEST / f'{slug}-{page}.json'
            path.write_text(body,encoding='utf-8')
            evidence['archive_path'] = str(path)
            data = json.loads(body)
            output['requests'].append(evidence)
            if data.get('errcode') or not isinstance(data.get('books'),list):
                raise ValueError('Official search returned no valid books array')
            for row in data['books']:
                info = row.get('bookInfo',{})
                if normalize_title(info.get('title','')) != normalize_title(title):
                    continue
                bid = str(info.get('bookId',''))
                if bid:
                    candidates[bid] = {'book_id':bid,'title':info.get('title'),'author':info.get('author'),
                                       'detail_url':detail_url(info),'type':info.get('type'),
                                       'search_source':evidence,'reading_count':row.get('readingCount')}
            more = data.get('hasMore')
            if more not in (1,True):
                ended = True
                break
            following = max((r.get('searchIdx',0) for r in data['books']),default=offset)
            if following <= offset:
                break
            offset = following
        output['candidates'] = list(candidates.values())
        output['search_complete'] = ended
        if ended and len(candidates)==1:
            candidate = next(iter(candidates.values()))
            if candidate['detail_url']:
                output.update(candidate, status='unique_exact_title_official_search', verified=True,
                              matching_note='原表无作者；完整官方搜索中仅一个规范化书名精确匹配。保留版本信息供核对。')
        if len(candidates)>1:
            output['status']='ambiguous_same_title'
    except Exception as exc:
        output['error']=str(exc)
    (DEST / f'{slug}-resolution.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    return output


def main():
    DEST.mkdir(parents=True,exist_ok=True)
    source=json.loads((ROOT/'runs/2026-11-03/tencent-source/filtered-rows.json').read_text(encoding='utf-8'))
    titles=sorted({r['normalized']['title'] for r in source['rows'] if r['normalized']['platform']=='weread'
                   and r['normalized']['service_medium']=='ebook' and r['normalized']['title']})
    started=now()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        rows=list(executor.map(resolve,titles))
    result={'schema_version':'project.identities.v1','started_at':started,'finished_at':now(),'rows':rows}
    (DEST/'identity-map.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    tracked=[{'book_id':r['book_id'],'title':r['title'],'detail_url':r['detail_url']} for r in rows if r.get('verified')]
    (DEST/'tracked.json').write_text(json.dumps(tracked,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'titles':len(titles),'resolved':len(tracked),'pending':len(rows)-len(tracked),'output':str(DEST)},ensure_ascii=False))


if __name__=='__main__':
    main()
