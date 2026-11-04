"""Initialize explicit marketing rows without inventing platform identities."""
from datetime import date, timedelta
import json

try:
    from .daily_run import Base, ROOT, atomic_json, now, options, shanghai_day
    from .feishu_io import same
    from .manual_project_notifications import project_index
except ImportError:
    from daily_run import Base, ROOT, atomic_json, now, options, shanghai_day
    from feishu_io import same
    from manual_project_notifications import project_index

PLATFORMS = {'微信读书': 'weread', '番茄小说': 'fanqie'}
FIELDS = ('作品唯一键', '书名', '平台', '监测范围', '平台作品ID', '作品主页', '作者',
          '首次进入项目总表', '追踪截止日', '作品形式', '平台匹配状态', '采集状态')


def empty(value):
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def build_plan(projects, day):
    date.fromisoformat(day)
    project_index(projects)
    existing = [p['作品唯一键'] for p in projects if p.get('作品唯一键')]
    if len(existing) != len(set(existing)):
        raise ValueError('Duplicate existing nonempty project identity')
    updates, before, pending = {}, {}, []
    for p in projects:
        if not empty(p.get('作品唯一键')):
            continue
        rid = p['record_id']
        scope, platforms = options(p.get('监测范围')), options(p.get('平台'))
        reasons = []
        if p.get('作品形式') != '电子书':
            reasons.append('仅允许电子书；有声或形式未确认项目不自动接入')
        if scope != {'营销项目'}:
            reasons.append('监测范围需明确为营销项目；未推断或改写')
        if not isinstance(p.get('书名'), str) or not p['书名'].strip():
            reasons.append('书名为空')
        if len(platforms) != 1 or not platforms <= set(PLATFORMS):
            reasons.append('平台需为单一微信读书或番茄小说')
        if reasons:
            pending.append({'record_id': rid, 'title': p.get('书名'), 'reasons': reasons})
            continue
        key = 'pending:' + PLATFORMS[next(iter(platforms))] + ':ebook:manual-' + rid
        if key in existing:
            raise ValueError('Generated manual pending key already belongs to another record')
        fields = {'作品唯一键': key}
        start = p.get('首次进入项目总表')
        if empty(start):
            start = day + ' 00:00'
            fields['首次进入项目总表'] = start
        if empty(p.get('追踪截止日')):
            try:
                fields['追踪截止日'] = (date.fromisoformat(str(start)[:10]) + timedelta(days=29)).isoformat() + ' 00:00'
            except ValueError:
                pending.append({'record_id': rid, 'title': p.get('书名'), 'reasons': ['原首次入表日格式无效；保留原值，未猜测截止日']})
        if empty(p.get('平台匹配状态')):
            fields['平台匹配状态'] = '待核实：手工新增作品，尚未确认对应电子书平台ID'
        if empty(p.get('采集状态')):
            fields['采集状态'] = ['待接入']
        updates[rid] = fields
        before[rid] = {field: p.get(field) for field in FIELDS}
    return {'date': day, 'updates': updates, 'before': before, 'pending': pending,
            'record_ids': sorted(p['record_id'] for p in projects)}


def unknown_guard_paths(root):
    candidates = {root / 'runs' / 'manual-project-init-state.json'}
    # Manual initialization precedes all other modules, so it must respect
    # identity migrations/imports interrupted outside the daily coordinator.
    for directory in (root / 'receipts', root / 'runs'):
        for pattern in ('**/legacy-migration-state.json', '**/legacy-rollback-state.json', '**/link-state.json',
                        '**/alias-merge-state.json', '**/*.apply-state.json'):
            candidates.update(directory.glob(pattern))
    unknown = []
    for path in sorted(candidates):
        if not path.exists():
            continue
        try:
            state = json.loads(path.read_text(encoding='utf-8-sig'))
            finished = isinstance(state, dict) and state.get('phase') == 'finished'
        except (ValueError, OSError):
            finished = False
        if not finished:
            unknown.append(str(path))
    return unknown


def has_unknown_guard(root):
    return bool(unknown_guard_paths(root))


def run(args, root=ROOT, api=None, day=None):
    day = day or shanghai_day()
    unknown = unknown_guard_paths(root)
    if unknown:
        raise RuntimeError('Shared project initialization/migration outcome unknown; reconcile full readback before retry: '
                           + '; '.join(unknown))
    api = api or Base(root / 'receipts' / day / ('manual-project-init-' + now().strftime('%H%M%S-%f')))
    projects = api.read('项目总表')
    plan = build_plan(projects, day)
    atomic_json(root / 'runs' / day / ('manual-project-init-dry-run.json' if args.dry_run else 'manual-project-init-plan.json'), plan)
    summary = {'status': 'partial' if plan['pending'] else 'completed', 'pending': plan['pending'],
               'planned_updates': len(plan['updates']), 'updated_records': 0, 'messages_sent': 0,
               'identity_mutation_unknown': False, 'dry_run': args.dry_run}
    if args.dry_run or not plan['updates']:
        return summary
    state_path = root / 'runs' / 'manual-project-init-state.json'
    state = {'phase': 'apply_in_progress', 'date': day, 'plan': plan}
    atomic_json(state_path, state)
    items = list(plan['updates'].items())
    for offset in range(0, len(items), 200):
        api.call('+record-batch-update', '项目总表', {'update_records': dict(items[offset:offset + 200])})
    actual = project_index(api.read('项目总表'))
    if sorted(actual) != plan['record_ids']:
        raise RuntimeError('Project record set changed during manual initialization; reconcile read-only')
    keys = [p['作品唯一键'] for p in actual.values() if p.get('作品唯一键')]
    if len(keys) != len(set(keys)):
        raise RuntimeError('Duplicate project identity after initialization')
    for rid, fields in plan['updates'].items():
        for field, expected in {**plan['before'][rid], **fields}.items():
            if not same(expected, actual[rid].get(field)):
                raise RuntimeError('Manual initialization full readback mismatch: ' + rid + '/' + field)
    summary.update(updated_records=len(items), readback='passed')
    atomic_json(state_path, {'phase': 'finished', 'date': day, 'summary': summary})
    return summary
