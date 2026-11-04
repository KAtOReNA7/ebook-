"""Plan/review/apply Tencent marketing rows to Feishu, with source provenance.

Default invocation is read-only and writes only a local import-plan. External
writes require --apply-plan PATH. Source IDs are never treated as platform IDs.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import unicodedata

try:
    from .feishu_io import Base, ROOT, same, save
    from .local_settings import SOURCE_DOCUMENT_KEY, project_path
except ImportError:
    from feishu_io import Base, ROOT, same, save
    from local_settings import SOURCE_DOCUMENT_KEY, project_path

TZ = timezone(timedelta(hours=8))
DOC_KEY = SOURCE_DOCUMENT_KEY
FORMS = {'ebook': '电子书', 'audio': '有声书', 'unknown': '未确认'}
PLATFORMS = {'weread': '微信读书', 'fanqie': '番茄小说'}


def normalize_title(value):
    value = unicodedata.normalize('NFKC', str(value or '')).strip()
    if value.startswith('《') and value.endswith('》'):
        value = value[1:-1].strip()
    return re.sub(r'\s+', '', value)


def sha(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(value).hexdigest()


def identities_from_file(paths):
    result = {}
    for path in paths or []:
        body = json.loads(project_path(path, ROOT).read_text(encoding='utf-8-sig'))
        rows = body if isinstance(body, list) else body.get('rows', body.get('identities', []))
        for row in rows:
            if row.get('verified') is not True:
                continue
            platform = row.get('platform')
            medium = row.get('medium', row.get('service_medium'))
            title = normalize_title(row.get('source_title', row.get('normalized_title')))
            if platform not in PLATFORMS or medium not in FORMS or not title or not row.get('book_id') or not row.get('detail_url'):
                raise ValueError('Incomplete verified identity record')
            key = (platform, medium, title)
            tracking = identity_tracking_medium(row, medium)
            if key in result and (str(result[key]['book_id']) != str(row['book_id']) or
                                  identity_tracking_medium(result[key], medium) != tracking):
                raise ValueError('Conflicting verified platform IDs for one source identity')
            result[key] = row
    return result


def identity_tracking_medium(identity, service_medium):
    """The service bought and the platform edition measured are separate facts.

    Cross-medium mapping must be explicit in verified evidence. Preserve legacy
    audio identities rather than silently reinterpreting their numeric IDs.
    """
    medium = identity.get('tracking_medium', 'audio' if service_medium == 'audio' else 'ebook')
    if medium not in ('ebook', 'audio'):
        raise ValueError('Invalid verified tracking medium')
    return medium


def identity_project_key(platform, service_medium, identity):
    tracking = identity_tracking_medium(identity, service_medium)
    return platform + (':audio:' if tracking == 'audio' else ':') + str(identity['book_id'])


def project_identity(normalized, identities):
    platform = normalized['platform']
    medium = normalized['service_medium']
    title = normalize_title(normalized['title'])
    lookup = (platform, medium, title)
    verified = identities.get(lookup)
    if verified:
        key = identity_project_key(platform, medium, verified)
    else:
        key = 'pending:' + platform + ':' + medium + ':' + sha(title)[:24]
    return key, verified


def unique_index(rows, field):
    result = {}
    for row in rows:
        key = row.get(field)
        if not key or key in result:
            raise ValueError('Missing or duplicate key in existing Base table: ' + field)
        result[key] = row
    return result


def amount(value):
    return Decimal(str(value)).quantize(Decimal('0.01')) if isinstance(value, (int, float)) else Decimal('0.00')


def reviewed_source_rows(rows, override_path=None):
    """Apply evidence-backed interpretation without changing source cells or IDs."""
    path = Path(override_path) if override_path else ROOT / 'config' / 'source-review-overrides.json'
    body = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'overrides': []}
    if body.get('schema_version', 'source-review-overrides.v1') != 'source-review-overrides.v1':
        raise ValueError('Unexpected source-review override schema')
    overrides = {}
    for override in body['overrides']:
        fingerprint = override.get('source_content_sha256', '')
        if (override.get('source_document_key') != DOC_KEY or
                not re.fullmatch('[0-9a-f]{64}', fingerprint) or
                override.get('medium') not in ('audio', 'ebook') or
                not override.get('note') or fingerprint in overrides):
            raise ValueError('Invalid or duplicate source-review override')
        overrides[fingerprint] = override
    interpreted, applied = [], []
    for source_row in rows:
        row = {**source_row, 'normalized': deepcopy(source_row['normalized'])}
        override = overrides.get(row['source_content_sha256'])
        if override:
            if override.get('source_sheet_id') != row['source_sheet_id']:
                raise ValueError('Source-review override sheet does not match fingerprint')
            evidence = project_path(override['evidence_path'], ROOT)
            if sha(evidence.read_bytes()) != override['evidence_sha256']:
                raise ValueError('Source-review evidence changed; review again before importing')
            row['normalized']['service_medium'] = override['medium']
            row['review_override'] = {**override, 'evidence_path': str(evidence.resolve())}
            applied.append({**row['review_override'], 'source_coordinate_key': row['source_coordinate_key']})
        interpreted.append(row)
    return interpreted, applied


def plan_import(source, existing_projects, existing_services, identities=None, import_day=None, override_path=None, scope_policy=None):
    source = Path(source).resolve()
    source_bytes = source.read_bytes()
    data = json.loads(source_bytes)
    if data.get('schema_version') != 'tencent.marketing-rows.v1':
        raise ValueError('Unexpected source schema')
    verification = json.loads(source.with_name('verification.json').read_text(encoding='utf-8'))
    if verification.get('independent_mismatches') or verification.get('coordinate_conflicts'):
        raise ValueError('Source verification has unresolved conflicts')
    if not verification.get('independent_cell_comparisons'):
        raise ValueError('Independent source verification is required')
    rows, applied_overrides = reviewed_source_rows(data['rows'], override_path)
    if len(rows) != verification['target_rows']:
        raise ValueError('Source row count differs from verification')
    try:
        from .service_scope import policy, exclusion_reason
    except ImportError:
        from service_scope import policy, exclusion_reason
    scope=policy() if scope_policy is None else scope_policy
    excluded=[]
    if scope.get('enabled'):
        excluded=[{'source_coordinate_key':r['source_coordinate_key'],'source_fingerprint':r['source_content_sha256'],
                   'reason':exclusion_reason(r),'normalized':r['normalized']} for r in rows if exclusion_reason(r)]
        rows=[r for r in rows if not exclusion_reason(r)]
    identities = dict(identities or {})
    import_day = import_day or datetime.now(TZ).date().isoformat()
    today = date.fromisoformat(import_day)
    projects_existing = unique_index([p for p in existing_projects if p.get('作品唯一键')], '作品唯一键')
    services_existing = unique_index(existing_services, '服务明细唯一键')
    # Reuse only an existing, explicitly linked platform identity for the same
    # source title/platform/form. Source IDs alone still never establish it.
    for service in existing_services:
        linked = projects_existing.get(service.get('作品唯一键'), {})
        key = linked.get('作品唯一键', '')
        if not key or key.startswith('pending:') or not linked.get('平台作品ID') or not linked.get('作品主页'):
            continue
        platform = 'weread' if key.startswith('weread:') else 'fanqie' if key.startswith('fanqie:') else None
        medium = next((kind for kind, label in FORMS.items() if label == service.get('服务对象')), None)
        title = normalize_title(service.get('原始书名'))
        if platform and medium and title:
            identities.setdefault((platform, medium, title), {'book_id': linked['平台作品ID'],
                                  'detail_url': linked['作品主页'], 'title': linked.get('书名'),
                                  'author': linked.get('作者'), 'existing_relationship': True,
                                  'tracking_medium': 'audio' if ':audio:' in key else 'ebook'})
    grouped = defaultdict(list)
    project_map = {}
    persisted_identities = defaultdict(set)
    for service in existing_services:
        if service.get('源行指纹') and service.get('作品唯一键') in projects_existing:
            persisted_identities[service['源行指纹']].add(service['作品唯一键'])
    resolved_group_identities = {}
    identity_migrations = []
    for row in rows:
        normalized = row['normalized']
        if normalized['platform'] not in PLATFORMS:
            raise ValueError('Unexpected non-target platform in filtered source')
        key, verified = project_identity(normalized, identities)
        persisted = persisted_identities.get(row['source_content_sha256'], set())
        if verified and len(persisted) == 1 and next(iter(persisted)) != key:
            # A new identity-map entry must not recreate a persisted marketing
            # project, reset D0 or strand the old record. The explicit linking
            # tool upgrades that record in place after review.
            previous_key = next(iter(persisted))
            identity_migrations.append({'kind': 'identity_migration_requires_review',
                'source_coordinate_key': row['source_coordinate_key'],
                'source_fingerprint': row['source_content_sha256'],
                'existing_project_key': previous_key, 'verified_project_key': key,
                'required_action': 'Run link_project_identities to upgrade the existing project in place; then rebuild this plan.'})
            key, verified = previous_key, None
        if not verified and len(persisted) == 1:
            # Preserve an already reconciled relationship when a later routine
            # run does not carry the original one-off identity-map file.
            key = next(iter(persisted))
            old_project = projects_existing[key]
            if not key.startswith('pending:') and old_project.get('平台作品ID') and old_project.get('作品主页'):
                if (row.get('review_override') and old_project.get('作品形式') == '有声书'
                        and ':audio:' not in key):
                    raise ValueError('Reviewed medium conflicts with existing platform identity; reconcile linkage first')
                verified = {'book_id': old_project['平台作品ID'], 'detail_url': old_project['作品主页'],
                            'title': old_project.get('书名') or normalized['title'],
                            'author': old_project.get('作者'), 'existing_relationship': True,
                            'tracking_medium': 'audio' if ':audio:' in key else 'ebook'}
        grouped[key].append(row)
        resolved_group_identities[key] = verified
        project_map[row['source_coordinate_key']] = key
    projects, project_baselines = [], {}
    verified_count = 0
    for key, group in sorted(grouped.items()):
        normalized = group[0]['normalized']
        verified = resolved_group_identities[key]
        if len({r['normalized']['service_medium'] for r in group}) != 1 and (not verified or key.startswith('pending:')):
            raise ValueError('Mixed service media require a verified shared work identity')
        # Each source row already resolved its own service medium to this key.
        # A shared group can end with an ebook identity lacking an explicit
        # tracking_medium while its first service is audio. Do not borrow that
        # first service's medium to reinterpret the canonical platform key.
        tracking = ('audio' if ':audio:' in key else 'ebook') if verified else normalized['service_medium']
        existing = projects_existing.get(key, {})
        title = verified.get('title') if verified else normalized['title']
        project = {'作品唯一键': key, '书名': existing.get('书名') or title,
                   '平台': [PLATFORMS[normalized['platform']]], '作品形式': FORMS[tracking],
                   '监测范围': ['营销项目'],
                   '平台匹配状态': (existing.get('平台匹配状态') if verified and verified.get('existing_relationship') else None)
                                or ('已核实：外部官方来源唯一匹配' if verified else '待核实：源表ID仅为来源标识，尚未确认平台作品ID')}
        if not existing.get('首次进入项目总表'):
            project['首次进入项目总表'] = import_day + ' 00:00'
        if not existing.get('追踪截止日'):
            project['追踪截止日'] = (today + timedelta(days=29)).isoformat() + ' 00:00'
        if not existing:
            project['磨铁版权确认'] = ['待核实']
            project['采集状态'] = ['待接入']
        # The owner has confirmed that every admitted marketing work belongs
        # to Motie. This is work-level evidence, independent of platform fields.
        try:
            from .work_copyright import apply_work_copyright
        except ImportError:
            from work_copyright import apply_work_copyright
        ownership = apply_work_copyright({}, marketing=True)
        if ownership.get('copyright_provider'):
            project['版权方'] = [ownership['copyright_provider']]
            project['磨铁版权确认'] = ['是']
        if verified:
            verified_count += 1
            project['平台作品ID'] = str(verified['book_id'])
            project['作品主页'] = verified['detail_url']
            if not existing.get('作者') and verified.get('author'):
                project['作者'] = verified['author']
        note = ('营销台账源表真实导入；历史服务日期保留在明细。实际入库日期为监测开始日。'
                 + ('按已核实作品合并营销费用，追踪对应电子书指标；每笔原始服务形式保留在明细。'
                    if verified and tracking == 'ebook' and any(r['normalized']['service_medium'] == 'audio' for r in group)
                    else '有声服务形式保留；对应电子书身份尚未核实。' if normalized['service_medium'] == 'audio' else '')
                + '源表书名：' + '；'.join(sorted({str(r['normalized']['title']) for r in group})))
        review_notes = sorted({r['review_override']['note'] for r in group if r.get('review_override')})
        if review_notes:
            note += '\n留档人工复核：' + '；'.join(review_notes)
        old_note = existing.get('证据与备注', '')
        project['证据与备注'] = old_note if note in old_note else (old_note + '\n' + note).strip()
        projects.append(project)
        project_baselines[key] = {field: existing.get(field) for field in project} if existing else None
    occurrence = Counter()
    services, missing_numeric, withheld, source_changes = [], [], [], list(identity_migrations)
    fee_policy_path = ROOT / 'config' / 'marketing-fee-policy.json'
    fee_policy = json.loads(fee_policy_path.read_text(encoding='utf-8')) if fee_policy_path.exists() else {}
    current_fingerprints = {row['source_content_sha256'] for row in rows}
    previous_source = [row for row in existing_services if str(row.get('原始行唯一键', '')).startswith('tencent:' + DOC_KEY + ':')]
    previous_by_coordinate = {(row.get('原始工作表与行号'), row.get('源行号')): row for row in previous_source}
    for row in rows:
        n = row['normalized']
        fingerprint = row['source_content_sha256']
        base_key = 'tencent:' + DOC_KEY + ':' + row['source_sheet_id'] + ':' + fingerprint
        occurrence[base_key] += 1
        original_key = base_key + ':' + str(occurrence[base_key])
        service_key = original_key + ':service:1'
        project_key = project_map[row['source_coordinate_key']]
        description = row['source_sheet_name'] + ' [' + row['source_sheet_id'] + '] 第' + str(row['source_row']) + '行'
        old_coord = previous_by_coordinate.get((description, row['source_row']))
        if old_coord and old_coord.get('源行指纹') not in current_fingerprints:
            source_changes.append({'source_coordinate_key': row['source_coordinate_key'],
                                   'old_service_key': old_coord['服务明细唯一键'], 'kind': 'source_edit_requires_review'})
        source_total_authoritative = (fee_policy.get('enabled') is True
            and n['platform'] in fee_policy.get('authoritative_total_platforms', []))
        eligible = (isinstance(n.get('fee'), (int, float))
            and not row['fee_statistics'].get('exclusion_reasons')
            and (source_total_authoritative or (row['fee_statistics']['book_cost_eligible']
                                                and n['arithmetic_check_passed'] is True)))
        if row.get('confirmed_duplicate_of'):
            eligible = False
        people_is_review = bool(n.get('service') and re.search(r'开分|好评|点评|书评', n['service'])
                                and not re.search(r'推书|顶作者', n['service']))
        notes = list(row['ambiguities'])
        if source_total_authoritative:
            notes.append(fee_policy['basis'])
        notes.append('平台作品ID仅来自独立核实映射；源作品ID不作为平台ID。')
        if row.get('review_override'):
            review = row['review_override']
            notes.append('留档人工复核：' + review['note'] + '\n证据SHA256：' + review['evidence_sha256'])
        if n['service_medium'] == 'unknown':
            notes.append('源表未明示电子/有声服务对象，暂记未确认。')
        if not people_is_review and n.get('people') is not None:
            notes.append('原人数列可能是推广条数；保留原始数量，不纳入邀评人数汇总。')
        if row.get('confirmed_duplicate_of'):
            notes.append('已证实重复，仅保留原始记录，不重复计费；主记录：' + row['confirmed_duplicate_of'])
        original = {'source_sheet_id': row['source_sheet_id'], 'source_row': row['source_row'],
                    'cells': {col: cell['value'] for col, cell in row['cells'].items()},
                    'formulas': {col: cell['formula'] for col, cell in row['cells'].items() if cell.get('formula')},
                    'archive_inheritance': n.get('archive_inheritance'), 'source_pages': row['source_pages']}
        fee_value = float(amount(n['fee'])) if isinstance(n.get('fee'), (int, float)) else None
        fields = {'服务明细唯一键': service_key, '原始行唯一键': original_key, '作品唯一键': project_key,
                  '原始书名': n['title'], '服务平台': n['platform_raw'], '服务对象': FORMS[n['service_medium']],
                  '金额口径': n['fee_basis'], '源行号': row['source_row'], '源行指纹': fingerprint,
                  '原始文档链接': row['source_document_url'].split('?')[0] + '?tab=' + row['source_sheet_id'],
                  '原始工作表与行号': description, '原始内容': json.dumps(original, ensure_ascii=False, separators=(',', ':')),
                  '服务内容': n.get('service'), '源作品ID': n.get('source_book_id'),
                  '单价（元）': n.get('unit_price'), '原始行总金额': fee_value,
                  '邀评人数': n.get('people') if people_is_review else None,
                  '已确认分摊金额': fee_value if eligible else None,
                  '分摊状态': ['无需分摊' if eligible else '待核实'], '计入统计': ['是' if eligible else '否'],
                  '分摊依据': ('用户确认以原表服务费总额为准；单价仅供参考，保留原乘法校验。' if source_total_authoritative
                          else '单个原始书名行；未拆分、未分摊；单价×人数与总额核验通过。') if eligible
                          else '原始支出保留；数值异常、缺失、重复候选或合包未核实，不计入已确认统计。',
                  '服务留档链接': '\n'.join(link['url'] for link in n['archive_links']) or None,
                  '校验说明': '\n'.join(dict.fromkeys(notes))}
        if n.get('service_date'):
            fields['服务日期'] = n['service_date'] + ' 00:00'
        # Keep None values in the reviewed plan. apply uses them to clear stale
        # managed fields on existing rows, and omits them for newly created rows.
        services.append({'project_key': project_key, 'source_coordinate_key': row['source_coordinate_key'], 'fields': fields})
        if n.get('fee') is None:
            missing_numeric.append(row['source_coordinate_key'])
        if not eligible:
            withheld.append(row['source_coordinate_key'])
    keys = [service['fields']['服务明细唯一键'] for service in services]
    if len(keys) != len(set(keys)) or len(services) != len(rows):
        raise ValueError('Source/service one-to-one or uniqueness failure')
    current_service_keys = set(keys)
    for old in previous_source:
        if old['服务明细唯一键'] not in current_service_keys:
            source_changes.append({'old_service_key': old['服务明细唯一键'], 'kind': 'missing_or_modified_source_requires_review'})
    source_raw_total = sum((amount(row['normalized'].get('fee')) for row in rows), Decimal(0))
    confirmed_total = sum((amount(service['fields'].get('已确认分摊金额')) for service in services), Decimal(0))
    deferred_total = sum((amount(service['fields'].get('原始行总金额')) for service in services
                          if service['fields']['计入统计'] == ['否']), Decimal(0))
    if source_raw_total != confirmed_total + deferred_total:
        raise ValueError('Expense reconciliation failure')
    summary = {'source_rows': len(rows), 'source_rows_before_scope_filter':verification['target_rows'],
               'excluded_out_of_scope':len(excluded),'excluded_by_reason':dict(Counter(x['reason'] for x in excluded)),
               'service_rows': len(services), 'project_groups': len(projects),
               'verified_project_groups': verified_count, 'pending_project_groups': len(projects) - verified_count,
               'new_projects': sum(p['作品唯一键'] not in projects_existing for p in projects),
               'new_services': sum(key not in services_existing for key in keys),
               'eligible_service_rows': len(services) - len(withheld), 'withheld_service_rows': len(withheld),
               'source_rows_missing_fee': len(missing_numeric), 'source_raw_fee_total': str(source_raw_total),
               'confirmed_fee_total': str(confirmed_total), 'deferred_fee_total': str(deferred_total),
               'reconciliation': 'passed', 'source_changes_requires_review': len(source_changes),
               'identity_migrations_requires_review': len(identity_migrations),
               'source_rows_with_archive_urls': sum(bool(r['normalized']['archive_links']) for r in rows),
               'audio_rows': sum(r['normalized']['service_medium'] == 'audio' for r in rows),
               'source_review_overrides_applied': len(applied_overrides),
               'platform_project_counts': dict(Counter(p['平台'][0] for p in projects)),
               'source_id_policy': 'Never assigned to platform ID; identity-map must be explicitly verified.',
               'fee_basis': verification['fee_basis'], 'notifications': 'No direct messages; native workflows must remain shadow during migration.'}
    return {'schema_version': 'tencent.feishu-import-plan.v1', 'created_at': datetime.now(TZ).isoformat(),
            'source_file': str(source), 'source_sha256': sha(source_bytes), 'import_day': import_day,
            'safe_to_apply': not source_changes, 'summary': summary, 'projects': projects, 'services': services,
            'source_review_overrides': applied_overrides, 'scope_policy':scope, 'excluded_source_rows':excluded,
            'fee_policy': fee_policy,
            'source_changes': source_changes, 'withheld_source_rows': withheld,
            'project_baselines': project_baselines, 'plan_scope': 'Only marketing project fields and source-owned service fields; metadata metrics untouched.'}


def apply_plan(path, allow_live_incremental=False):
    path = Path(path).resolve()
    plan = json.loads(path.read_text(encoding='utf-8-sig'))
    if plan.get('schema_version') != 'tencent.feishu-import-plan.v1' or not plan.get('safe_to_apply'):
        raise ValueError('Plan is not eligible for application')
    try:
        from .service_scope import enforce_write
    except ImportError:
        from service_scope import enforce_write
    for service in plan.get('services',[]): enforce_write('营销服务明细',service['fields'])
    if sha(project_path(plan['source_file'], ROOT).read_bytes()) != plan['source_sha256']:
        raise ValueError('Source changed after review; rebuild and review the plan')
    for review in plan.get('source_review_overrides', []):
        if sha(project_path(review['evidence_path'], ROOT).read_bytes()) != review['evidence_sha256']:
            raise ValueError('Reviewed source evidence changed after planning')
    runtime_path = ROOT / 'config' / 'runtime.json'
    runtime = json.loads(runtime_path.read_text(encoding='utf-8-sig')) if runtime_path.exists() else {}
    if not allow_live_incremental and (runtime.get('mode') != 'shadow' or runtime.get('notifications_enabled') is not False):
        raise ValueError('Migration import requires explicit shadow runtime to avoid historical-notification storms')
    state_path = path.with_suffix('.apply-state.json')
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if state.get('phase') != 'finished':
            raise RuntimeError('Prior import has unknown or incomplete outcomes; reconcile read-only before retry')
    api = Base(ROOT / 'receipts' / plan['import_day'] / ('marketing-' + datetime.now(TZ).strftime('%H%M%S-%f')))
    existing = unique_index([p for p in api.read('项目总表') if p.get('作品唯一键')], '作品唯一键')
    for proposed in plan['projects']:
        key = proposed['作品唯一键']
        baseline = plan['project_baselines'].get(key)
        current = existing.get(key)
        if current:
            for field, value in proposed.items():
                if same(value, current.get(field)):
                    continue
                if baseline is None or not same(baseline.get(field), current.get(field)):
                    raise RuntimeError('Project changed since review: ' + key + '/' + field)
    state = {'phase': 'projects_in_progress', 'plan': str(path), 'plan_sha256': sha(path.read_bytes())}
    save(state_path, state)
    try:
        projects = api.ensure('项目总表', '作品唯一键', plan['projects'], update=True)
        state['phase'] = 'services_in_progress'
        save(state_path, state)
        existing_services = unique_index(api.read('营销服务明细'), '服务明细唯一键')
        service_rows = []
        for service in plan['services']:
            fields = {**service['fields'], '关联项目': [{'id': projects[service['project_key']]['record_id']}]}
            if fields['服务明细唯一键'] not in existing_services:
                fields = {key: value for key, value in fields.items() if value is not None}
            service_rows.append(fields)
        api.ensure('营销服务明细', '服务明细唯一键', service_rows, update=True)
        state.update({'phase': 'finished', 'write_count': api.changes, 'readback': 'passed',
                      'finished_at': datetime.now(TZ).isoformat(), 'summary': plan['summary']})
        save(state_path, state)
        return state
    except BaseException as exc:
        state.update({'phase': 'requires_review', 'error': str(exc)})
        save(state_path, state)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--identity-map', type=Path, action='append', default=[])
    parser.add_argument('--apply-plan', type=Path)
    parser.add_argument('--allow-live-incremental', action='store_true', help='Only for a reviewed post-baseline daily increment')
    parser.add_argument('--offline', action='store_true', help='Plan against an empty Base snapshot; does not prove live availability')
    args = parser.parse_args()
    if args.apply_plan:
        print(json.dumps(apply_plan(args.apply_plan, args.allow_live_incremental), ensure_ascii=False, indent=2))
        return
    if not args.source:
        parser.error('--source is required when preparing a plan')
    output = args.output or args.source.with_name('import-plan.json')
    day = datetime.now(TZ).date().isoformat()
    api = None if args.offline else Base(ROOT / 'receipts' / day / ('marketing-plan-' + datetime.now(TZ).strftime('%H%M%S-%f')))
    projects = api.read('项目总表') if api else []
    services = api.read('营销服务明细') if api else []
    plan = plan_import(args.source, projects, services, identities_from_file(args.identity_map), day)
    plan['existing_base_snapshot'] = 'offline_empty' if args.offline else 'fully_read'
    save(output, plan)
    print(json.dumps({'plan': str(output.resolve()), 'safe_to_apply': plan['safe_to_apply'],
                      'external_mutations': 0, **plan['summary']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
