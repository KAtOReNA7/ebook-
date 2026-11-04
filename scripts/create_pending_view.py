"""One explicit query view for unresolved marketing works."""
from datetime import datetime
import json
from feishu_io import Base, ROOT, save


def main():
    api = Base(ROOT / 'receipts/2026-11-03' / ('pending-view-' + datetime.now().strftime('%H%M%S')))
    name, table = '待核实作品与作者候选', '项目总表'
    views = api.call('+view-list', table)['data']['views']
    if any(v['name'] == name for v in views):
        raise RuntimeError('View already exists; read its receipt instead of recreating')
    api.call('+view-create', table, {'name': name, 'type': 'grid'})
    found = [v for v in api.call('+view-list', table)['data']['views'] if v['name'] == name]
    assert len(found) == 1
    view_id = found[0]['id']
    fields = ['书名', '作品唯一键', '平台', '作者', '平台匹配状态', '证据与备注',
              '营销服务明细', '源表服务费合计', '源表邀评人次', '采集状态']
    filters = {'logic': 'and', 'conditions': [['监测范围', 'intersects', ['营销项目']],
                                             ['采集状态', 'intersects', ['待接入']]]}
    api.call('+view-set-visible-fields', table, {'visible_fields': fields}, view_id=view_id)
    api.call('+view-set-filter', table, filters, view_id=view_id)
    actual_fields = api.call('+view-get-visible-fields', table, view_id=view_id)['data']
    actual_filter = api.call('+view-get-filter', table, view_id=view_id)['data']
    assert actual_fields['visible_fields'] == fields and actual_filter['filter'] == filters
    receipt = {'table': table, 'view_id': view_id, 'name': name, 'fields': actual_fields,
               'filter': actual_filter, 'readback': 'passed'}
    save(ROOT / 'config/pending-view.json', receipt)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
