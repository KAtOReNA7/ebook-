"""Read-only full audit evidence collection for the active acceptance directory."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
from feishu_io import Base, ROOT, save
from configure_readable_views import plan_views, read_view, verify_view


def main():
    out = Path(json.loads((ROOT/'config/whole-base-acceptance.json').read_text(encoding='utf-8'))['receipt_dir'])
    api = Base(out/'live-view-audit', presentation_enabled=False)
    tables = list(api.manifest['tables'])
    views = {t: api.call('+view-list', t)['data']['views'] for t in tables}
    save(out/'views-inventory.json', views)
    expected = {(p['table'], p['name']): p for p in plan_views()}
    def check(task):
        table, view = task
        actual = read_view(api, table, view['id'])
        item = expected.get((table, view['name']))
        return {'table': table, 'name': view['name'], 'view_id': view['id'],
                'matches_expected': verify_view(actual, item) if item else None,
                'actual': actual}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(check, [(t,v) for t,vs in views.items() for v in vs]))
    save(out/'views-audit.json', results)
    schemas = {t: api.call('+field-list', t)['data'] for t in tables}
    for t,data in schemas.items():
        data['fields'] = [api.complete_field(t, f) for f in data['fields']]
    save(out/'fields-complete.json', schemas)
    workflows = {}
    for wid in ['wkfaTJdSRTZJoYBn', 'wkfP90zBZI70NZYS']:
        workflows[wid] = api.call('+workflow-get', workflow_id=wid)['data']
    save(out/'workflows.json', workflows)
    print(json.dumps({'views':len(results), 'mismatches':[{'table':r['table'],'name':r['name']} for r in results if r['matches_expected'] is not True],
                      'schema_tables':len(schemas), 'workflow_count':len(workflows)},ensure_ascii=False))


if __name__=='__main__':
    main()
