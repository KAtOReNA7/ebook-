"""Initialize explicit marketing rows without inventing platform identities."""
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path

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


_GUARD_NAMES = {'legacy-migration-state.json', 'legacy-rollback-state.json',
                'link-state.json', 'alias-merge-state.json', 'publisher-recent-state.json',
                'fanqie-rising-state.json'}


def _guard_name(name):
    name = os.path.normcase(name)
    return name in _GUARD_NAMES or name.endswith('.apply-state.json')


def _nested_guards(directory):
    """Visit each directory once, matching all guard types, including in old runs."""
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except FileNotFoundError:
            if current == directory and not os.path.lexists(directory):
                continue  # A new checkout may not have a receipts or runs directory.
            raise
        with entries:
            for entry in entries:
                if _guard_name(entry.name):
                    yield Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)


def unknown_guard_paths(root):
    direct = (root / 'runs' / name for name in ('manual-project-init-state.json',
              'manual-project-events-state.json', 'marketing-expense-notification-state.json'))
    candidates = {path for path in direct if os.path.lexists(path)}
    # Manual initialization precedes all other modules, so it must respect
    # identity migrations/imports interrupted outside the daily coordinator.
    for directory in (root / 'receipts', root / 'runs'):
        candidates.update(_nested_guards(directory))
    unknown = []
    # These source guards use reconciled as an explicit audited resume state.
    # They were previously omitted by the shared report gate.
    for name in ('daily-state.json','tencent-state.json','fanqie-state.json'):
        for path in (root/'runs').glob('*/'+name):
            try:
                state=json.loads(path.read_text(encoding='utf-8-sig'))
                if not isinstance(state, dict) or state.get('phase') in {
                        'sync_in_progress','apply_in_progress','events_in_progress','requires_review'}:
                    unknown.append(str(path))
            except (ValueError,OSError):unknown.append(str(path))
    export_cache = {}
    for path in sorted(candidates):
        try:
            state = json.loads(path.read_text(encoding='utf-8-sig'))
            finished = isinstance(state, dict) and state.get('phase') == 'finished'
        except (ValueError, OSError):
            finished = False
        if not finished and not verified_inspection_copy(root, path, export_cache):
            unknown.append(str(path))
    return unknown


_recent_export_guards = {}


def _export_guard_bytes(manifest):
    # Rehash the whole export on every inspection. Only exact guard bytes from
    # the same content can be reused; never trust a file's timestamp or length.
    with manifest.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').digest()
        if checksum in _recent_export_guards:
            return _recent_export_guards[checksum]
        stream.seek(0)
        raw = stream.read()
    if hashlib.sha256(raw).digest() != checksum:
        return {}  # The export changed between hashing and parsing.
    files = json.loads(raw.decode('utf-8'))['files']
    if not isinstance(files, dict):
        return {}
    guards = {name: entry['text'].encode('utf-8') for name, entry in files.items()
              if _guard_name(name.rsplit('/', 1)[-1]) and isinstance(entry, dict)
              and isinstance(entry.get('text'), str)}
    if sum(map(len, guards.values())) <= 2 * 1024 * 1024:
        if len(_recent_export_guards) >= 4:
            del _recent_export_guards[next(iter(_recent_export_guards))]
        _recent_export_guards[checksum] = guards
    return guards


def verified_inspection_copy(root, path, cache):
    """Ignore only byte-verified read-only exports; never ignore active guards."""
    try:
        parts = path.relative_to(root / 'receipts').parts
        if len(parts) < 5 or not parts[1].startswith('stage-recovery-') or parts[2] != 'evidence':
            return False
        export = root / 'receipts' / parts[0] / parts[1]
        if export not in cache:
            result = json.loads((export / 'result.json').read_text(encoding='utf-8'))
            valid = (result.get('read_only') is True and result.get('business_writes') == 0
                     and result.get('messages_created') == 0
                     and result.get('evidence_directory') == str(export / 'evidence'))
            cache[export] = _export_guard_bytes(export / 'reconstructed-checkpoint.json') if valid else {}
        entry = cache[export].get('/'.join(parts[3:]))
        return entry is not None and path.read_bytes() == entry
    except (ValueError, OSError, KeyError, TypeError):
        return False


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
