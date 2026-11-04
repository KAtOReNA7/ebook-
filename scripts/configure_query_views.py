"""Create inspectable business query views; retain full request/ACK/readback receipts."""
from datetime import datetime
import json
from feishu_io import Base, ROOT, save


def main():
    plan = json.loads((ROOT/'config/query-views-plan.json').read_text(encoding='utf-8'))
    stamp = datetime.now().strftime('%Y-%m-%d/query-views-%H%M%S')
    api = Base(ROOT/'receipts'/stamp)
    results = []
    for item in plan:
        table = item['table']
        views = api.call('+view-list', table)['data']['views']
        existing = [v for v in views if v['name'] == item['name']]
        if existing:
            raise RuntimeError('Named view already exists; inspect its saved receipts instead of overwriting: '+item['name'])
        api.call('+view-create', table, {'name': item['name'], 'type': 'grid'})
        views = api.call('+view-list', table)['data']['views']
        created = [v for v in views if v['name'] == item['name']]
        if len(created) != 1:
            raise RuntimeError('View creation outcome needs reconciliation: '+item['name'])
        view_id = created[0]['id']
        api.call('+view-set-visible-fields', table, {'visible_fields': item['fields']}, view_id=view_id)
        if item['filter']['conditions']:
            api.call('+view-set-filter', table, item['filter'], view_id=view_id)
        api.call('+view-set-sort', table, item['sort'], view_id=view_id)
        result = {'table':table, 'name':item['name'], 'id':view_id, 'expected':item}
        for label, command in [('fields','+view-get-visible-fields'),('filter','+view-get-filter'),('sort','+view-get-sort')]:
            result[label] = api.call(command, table, view_id=view_id)['data']
        if result['fields']['visible_fields'] != item['fields'] or result['filter']['filter'] != item['filter'] or result['sort']['sort'] != item['sort']['sort_config']:
            save(api.receipt_dir/'unverified-view.json', result)
            raise RuntimeError('View readback differs: '+item['name'])
        result['readback_verified'] = True
        results.append(result)
        save(api.receipt_dir/'view-readbacks.json', results)
        print(json.dumps(result, ensure_ascii=False))
    save(ROOT/'config/query-views-readback.json', results)


if __name__ == '__main__':
    main()
