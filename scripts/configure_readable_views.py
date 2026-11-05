"""Plan readable, platform-isolated views; --apply writes and verifies each view.

This changes view presentation only. Run after the schema/display migration.
Every mutation retains a before snapshot, request/ACK and immediate readback.
"""
import argparse
from datetime import datetime
import json

from feishu_io import Base, ROOT, save


WECHAT_BOARDS = ['微信飙升Top50', '微信新书Top50', '微信女生小说飙升Top50', '微信女生小说新书Top50']
PROJECT_CORE = ['书名', '作者', '平台', '一级分类', '二级分类', '字数']
PROJECT_FEES = ['源表服务费合计', '已确认营销支出', '源表邀评人次', '已确认邀评人次', '营销服务明细']
PROJECT_TAIL = ['上榜记录', '每日记录', '作品主页', '首次进入项目总表', '追踪截止日',
                '最新采集时间', '采集状态', '平台匹配状态', '证据与备注']
WECHAT_LATEST = ['最新点评人数（微信）', '最新推荐值（微信）', '最新今日阅读人数（微信）']
FANQIE_LATEST = ['最新累计阅读人数（番茄）', '最新点评人数（番茄）', '最新书评数量（番茄）', '指标精度说明']
SERVICE_FIELDS = ['服务摘要', '书名', '服务日期', '服务平台', '服务对象', '服务内容',
                  '邀评人数', '原始行总金额', '已确认分摊金额', '服务留档链接', '关联项目', '计入统计', '校验说明']
SERVICE_REVIEW = ['服务摘要', '书名', '服务日期', '服务平台', '服务对象', '邀评人数',
                  '单价（元）', '原始行总金额', '已确认分摊金额', '校验说明', '服务留档链接', '原始工作表与行号']
DAILY_COMMON = ['每日摘要', '书名', '数据日期', '平台', '关联项目']
DAILY_TAIL = ['一级分类', '二级分类', '字数', '采集时间', '采集状态', '缺失字段与原因', '数据来源']
DAILY_WECHAT = DAILY_COMMON + ['点评人数', '推荐值', '今日阅读人数'] + DAILY_TAIL
DAILY_FANQIE = DAILY_COMMON + ['累计阅读人数', '点评人数', '书评数量', '精度', '指标原文'] + DAILY_TAIL
BOARD_FIELDS = ['上榜摘要', '书名', '采集日期', '榜单', '名次', '关联项目', '采集时间', '榜单来源', '快照完整性', '证据与缺口']


def filter_of(*conditions):
    return {'logic': 'and', 'conditions': list(conditions)}


def sort_of(*items):
    return {'sort_config': [{'field': name, 'desc': desc} for name, desc in items]}


def plan_views():
    plans = []

    def add(table, name, fields, filters=None, sort=None):
        plans.append({'table': table, 'name': name, 'fields': fields,
                      'filter': filters or filter_of(), 'sort': sort or sort_of()})

    marketing = ['监测范围', 'intersects', ['营销项目']]
    by_title = sort_of(('书名', False))
    add('项目总表', '微信读书项目', PROJECT_CORE[:2] + ['加入趋势对比'] + PROJECT_CORE[2:] + WECHAT_LATEST + PROJECT_FEES + PROJECT_TAIL,
        filter_of(['平台', 'intersects', ['微信读书']], marketing), by_title)
    add('项目总表', '番茄小说项目', PROJECT_CORE[:2] + ['加入趋势对比'] + PROJECT_CORE[2:] + FANQIE_LATEST + ['基础资料来源'] + PROJECT_FEES + PROJECT_TAIL,
        filter_of(['平台', 'intersects', ['番茄小说']], marketing), by_title)
    add('项目总表', '仅榜单样本', PROJECT_CORE[:2] + ['加入趋势对比', '当前榜单', '榜单核验日期', '版权方'] + PROJECT_CORE[2:] + WECHAT_LATEST + ['上榜记录', '每日记录', '首次上榜采集日', '追踪截止日', '采集状态', '作品主页'],
        filter_of(['平台', 'intersects', ['微信读书']], ['监测范围', 'intersects', ['仅榜单样本']]), by_title)
    add('项目总表', '待核实作品与作者候选', ['书名', '作者', '平台', '作品形式', '一级分类', '二级分类', '基础资料来源', '平台匹配状态', '证据与备注', '营销服务明细', '源表服务费合计', '源表邀评人次', '采集状态'],
        filter_of(marketing, ['采集状态', 'intersects', ['待接入']]), by_title)
    add('项目总表', '表格', PROJECT_CORE + ['监测范围'] + WECHAT_LATEST + FANQIE_LATEST + PROJECT_FEES + PROJECT_TAIL,
        filter_of(['监测范围', 'intersects', ['营销项目', '仅榜单样本']]), by_title)

    service_sort = sort_of(('服务日期', True), ('书名', False))
    for name in ['表格', '按书查询服务与留档']:
        add('营销服务明细', name, SERVICE_FIELDS, sort=service_sort)
    add('营销服务明细', '费用待核对', SERVICE_REVIEW, filter_of(['计入统计', 'intersects', ['否']]), service_sort)
    add('营销服务明细', '微信服务明细', SERVICE_FIELDS, filter_of(['服务平台', '==', '微信读书']), service_sort)
    add('营销服务明细', '番茄服务明细', SERVICE_FIELDS, filter_of(['服务平台', '==', '番茄']), service_sort)

    daily_sort = sort_of(('数据日期', True), ('书名', False))
    add('每日数据', '微信每日指标', DAILY_WECHAT, filter_of(['平台', 'intersects', ['微信读书']]), daily_sort)
    add('每日数据', '番茄每日指标', DAILY_FANQIE, filter_of(['平台', 'intersects', ['番茄小说']]), daily_sort)
    add('每日数据', '表格', DAILY_COMMON + ['点评人数', '推荐值', '今日阅读人数', '累计阅读人数', '书评数量', '精度'] + DAILY_TAIL,
        sort=daily_sort)

    board_sort = sort_of(('采集日期', True), ('榜单', False), ('名次', False))
    complete = ['快照完整性', 'intersects', ['完整']]
    add('榜单快照', '表格', BOARD_FIELDS, sort=board_sort)
    add('榜单快照', '微信上榜记录', BOARD_FIELDS, filter_of(['榜单', 'intersects', WECHAT_BOARDS], complete), board_sort)
    add('榜单快照', '番茄上榜记录', BOARD_FIELDS, filter_of(['榜单', 'intersects', ['番茄出版飙升榜']], complete), board_sort)
    for board in WECHAT_BOARDS:
        add('榜单快照', board, BOARD_FIELDS, filter_of(['榜单', 'intersects', [board]], complete), board_sort)

    add('通知事件', '表格', ['事件唯一键', '通知标题', '关联项目', '事件类型', '发生日期', '通知正文', '通知状态', '收件人', '发送时间', '发送回执'],
        sort=sort_of(('发生日期', True)))
    add('运行日志', '表格', ['运行批次', '状态', '开始时间', '结束时间', '计划时间', '成功作品数', '失败作品数', '完整榜单数', '新增事件数', '缺口原因'],
        sort=sort_of(('开始时间', True)))
    add('规则配置', '表格', ['规则ID', '规则说明', '状态', '试运行收件人', '参数与边界', '依赖与验收'])
    return plans


def equivalent_filter(left, right):
    # Feishu returns null after clearing all conditions.
    def canonical(value):
        return None if not value or not value.get('conditions') else value
    return canonical(left) == canonical(right)


def read_view(api, table, view_id):
    return {label: api.call(command, table, view_id=view_id)['data']
            for label, command in [('fields', '+view-get-visible-fields'),
                                   ('filter', '+view-get-filter'), ('sort', '+view-get-sort')]}


def verify_view(actual, item):
    return (actual['fields']['visible_fields'] == item['fields']
            and equivalent_filter(actual['filter']['filter'], item['filter'])
            and actual['sort']['sort'] == item['sort']['sort_config'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    api = Base(ROOT / 'receipts' / datetime.now().strftime('%Y-%m-%d/readable-views-%H%M%S-%f'))
    plans = plan_views()
    existing = {name: api.call('+view-list', name)['data']['views'] for name in api.manifest['tables']}
    save(api.receipt_dir / 'plan.json', plans)
    save(api.receipt_dir / 'views-before.json', existing)
    if not args.apply:
        print(json.dumps({'mode': 'plan', 'views': len(plans), 'receipt_dir': str(api.receipt_dir)}, ensure_ascii=False))
        return
    # Fail before the first write if the display migration is incomplete.
    for item in plans:
        table = api.manifest['tables'][item['table']]
        fields = {f['name']: f for f in table['field_readback']['fields']}
        missing = set(item['fields']) - set(fields)
        if missing:
            raise RuntimeError(f"Schema migration incomplete for {item['table']}: {sorted(missing)}")
        primary = next(f['name'] for f in fields.values() if f['id'] == table['primary_field'])
        if item['fields'][0] != primary:
            raise RuntimeError(f"Expected readable primary field in {item['table']}: {primary}")
    results = []
    save(api.receipt_dir / 'state.json', {'phase': 'applying', 'applied': 0})
    for item in plans:
        table, name = item['table'], item['name']
        matches = [v for v in existing[table] if v['name'] == name]
        if len(matches) > 1:
            raise RuntimeError('Ambiguous existing view: ' + name)
        if not matches:
            api.call('+view-create', table, {'name': name, 'type': 'grid'})
            existing[table] = api.call('+view-list', table)['data']['views']
            matches = [v for v in existing[table] if v['name'] == name]
            if len(matches) != 1:
                raise RuntimeError('Unknown view creation outcome: ' + name)
        view_id = matches[0]['id']
        before = read_view(api, table, view_id)
        save(api.receipt_dir / (view_id + '-before.json'), before)
        if before['fields']['visible_fields'] != item['fields']:
            api.call('+view-set-visible-fields', table, {'visible_fields': item['fields']}, view_id=view_id)
        if not equivalent_filter(before['filter']['filter'], item['filter']):
            api.call('+view-set-filter', table, item['filter'], view_id=view_id)
        if before['sort']['sort'] != item['sort']['sort_config']:
            api.call('+view-set-sort', table, item['sort'], view_id=view_id)
        actual = read_view(api, table, view_id)
        result = {'table': table, 'name': name, 'view_id': view_id,
                  'expected': item, 'actual': actual, 'readback_verified': verify_view(actual, item)}
        results.append(result)
        save(api.receipt_dir / 'view-readbacks.json', results)
        if not result['readback_verified']:
            raise RuntimeError('Readback differs; reconcile before retry: ' + name)
        save(api.receipt_dir / 'state.json', {'phase': 'applying', 'applied': len(results)})
    save(api.receipt_dir / 'state.json', {'phase': 'finished', 'applied': len(results), 'readback': 'passed'})
    save(ROOT / 'config/readable-views.json', results)
    print(json.dumps({'mode': 'applied', 'views': len(results), 'readback': 'passed', 'receipt_dir': str(api.receipt_dir)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
