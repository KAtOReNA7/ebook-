from copy import deepcopy
from scripts.notification_policy import OWNER_ID
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from scripts.marketing_notice_text import service_sentence, service_content
from scripts.marketing_expense_notifications import build_plan, notification_rows, run, validate_current_source
from scripts.sync_marketing import DOC_KEY, sha
from scripts.tencent_daily import new_entry_candidates, event_rows


DAY = '2026-11-05'
PROJECTS = [{'作品唯一键': 'weread:123', 'record_id': 'book1', '书名': '某书', '监测范围': ['营销项目']}]


def service(people=30, fee=180, rid='s1'):
    return {'record_id': rid, '原始行唯一键': 'tencent:' + DOC_KEY + ':BB08J2:' + rid,
            '服务明细唯一键': 'service:' + rid + ':' + str(people) + ':' + str(fee),
            '作品唯一键': 'weread:123', '关联项目': [{'id': 'book1'}], '服务对象': '电子书',
            '原始书名': '《某书》（测试版）', '服务内容': '电子书开分', '服务平台': '微信读书',
            '服务日期': '2026-10-27 00:00', '邀评人数': people, '原始行总金额': fee,
            '计入统计': ['是'], '源行指纹': str(people) + ':' + str(fee),
            '服务留档链接': 'https://example.invalid/archive', '原始文档链接': 'https://docs.qq.com/sheet/source'}


class MemoryAPI:
    def __init__(self, rows):
        self.services = rows
        self.writes = []

    def read(self, table):
        return deepcopy(self.services if table == '营销服务明细' else PROJECTS)


def fixture(root, api):
    (root / 'config').mkdir()
    (root / 'config/marketing-expense-notifications.json').write_text('{"enabled":true}', encoding='utf-8')
    update_source(root, api)


def update_source(root, api):
    directory = root / 'runs' / DAY
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / 'source.json'
    source.write_text('{}', encoding='utf-8')
    plan = {'safe_to_apply': True, 'source_file': str(source), 'source_sha256': sha(source.read_bytes()),
            'services': [{'fields': s, 'source_coordinate_key': s['record_id']} for s in api.services]}
    (directory / 'tencent-daily-import-plan.json').write_text(json.dumps(plan), encoding='utf-8')
    return plan


class ExpenseTests(unittest.TestCase):
    def test_requested_sentence_uses_service_date_people_and_amount(self):
        event = {'source_title': '《某书》（测试版）', 'service_date': '2026-10-27T00:00:00+08:00',
                 'review_count': 30.0, 'raw_fee': 180.0, 'service': '电子书开分'}
        self.assertEqual(service_sentence(event), '《某书》于2026年10月27日，约评30条，花费180元。')
        event.update(review_count=None, raw_fee=None, statistics_eligible=['否'])
        self.assertIn('约评条数待核实，金额待核实', service_sentence(event))
        self.assertIn('暂不计入已确认支出', service_content(event))

    def test_new_source_event_includes_count_and_reuses_its_stable_key(self):
        s = service()
        plan = {'safe_to_apply': True, 'import_day': DAY,
                'services': [{'fields': s, 'project_key': 'weread:123', 'source_coordinate_key': 'BB08J2:row:1'}]}
        previous = [{'服务明细唯一键': 'old', '原始行唯一键': 'tencent:' + DOC_KEY + ':old'}]
        events, baseline = new_entry_candidates(plan, previous, PROJECTS)
        self.assertFalse(baseline)
        rows = event_rows(events, PROJECTS, 'test')
        self.assertTrue(rows[0]['通知正文'].startswith('《某书》于2026年10月27日，约评30条，花费180元。'))
        self.assertEqual(rows[0]['事件唯一键'], sha(['marketing.new_service', 'v1', s['服务明细唯一键']]))
        self.assertEqual(rows[0]['收件人'], [{'id': OWNER_ID}])

    def test_silent_baseline_new_service_dedup_and_expense_revision(self):
        initial = build_plan([service()], PROJECTS, None, DAY)
        self.assertTrue(initial['baseline']); self.assertFalse(initial['events'])
        new = build_plan([service(), service(rid='s2')], PROJECTS, initial['next_registry'], DAY)
        self.assertEqual(new['new_services_handled_by_tencent'], 1); self.assertFalse(new['events'])
        changed = build_plan([service(40, 240)], PROJECTS, initial['next_registry'], DAY)
        self.assertEqual(len(changed['events']), 1)
        row = notification_rows(changed, PROJECTS, DAY)[0]
        self.assertIn('约评40条，花费240元', row['通知正文'])
        self.assertIn('约评：30 → 40条；费用：180 → 240元', row['通知正文'])
        self.assertFalse(build_plan([service(40, 240)], PROJECTS, changed['next_registry'], DAY)['events'])

    def test_archive_and_eligibility_changes_are_not_new_expenses(self):
        first = build_plan([service()], PROJECTS, None, DAY)
        row = service(); row.update(服务留档链接='https://example.invalid/corrected', 计入统计=['否'])
        self.assertFalse(build_plan([row], PROJECTS, first['next_registry'], DAY)['events'])

    def test_repeated_value_transition_gets_new_revision_but_replay_does_not(self):
        registry = build_plan([service()], PROJECTS, None, DAY)['next_registry']
        ids = []
        for people in (40, 30, 40):
            plan = build_plan([service(people)], PROJECTS, registry, DAY)
            ids.append(plan['events'][0]['event_id']); registry = plan['next_registry']
        self.assertEqual(len(set(ids)), 3)
        self.assertFalse(build_plan([service(40)], PROJECTS, registry, DAY)['events'])

    def test_audio_unknown_medium_and_identity_reassignment_fail_closed(self):
        for medium in ('有声书', '未确认'):
            row = service(); row['服务对象'] = medium
            with self.subTest(medium=medium), self.assertRaises(ValueError):
                build_plan([row], PROJECTS, None, DAY)
        previous = build_plan([service()], PROJECTS, None, DAY)['next_registry']
        previous['services']['s1']['project_key'] = 'another-book'
        with self.assertRaises(ValueError): build_plan([service()], PROJECTS, previous, DAY)

    def test_unreviewed_source_never_advances_registry_or_reads_base(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run(SimpleNamespace(dry_run=False), {'status': 'source_review_required'}, {}, root=root, day=DAY)
            self.assertEqual(result['status'], 'requires_review')
            self.assertFalse((root / 'runs/marketing-expense-registry.json').exists())

    def test_live_revision_once_and_same_day_replay_zero(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); api=MemoryAPI([service()]); fixture(root, api)
            writer=lambda api, rows, day: api.writes.extend(rows) or len(rows)
            args=SimpleNamespace(dry_run=False)
            runtime={'mode':'owner_only','notifications_enabled':True}; summary={'status':'partial'}
            self.assertTrue(run(args,summary,runtime,root,api,DAY,writer)['baseline'])
            api.services=[service(40,240)]; update_source(root,api)
            self.assertEqual(run(args,summary,runtime,root,api,DAY,writer)['events_created'],1)
            self.assertEqual(run(args,summary,runtime,root,api,DAY,writer)['events_created'],0)
            self.assertEqual(len(api.writes),1)

    def test_unknown_event_write_blocks_retry_without_consuming_registry(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);api=MemoryAPI([service()]);fixture(root,api)
            args=SimpleNamespace(dry_run=False); runtime={'mode':'owner_only','notifications_enabled':True}; summary={'status':'partial'}
            run(args,summary,runtime,root,api,DAY)
            registry=(root/'runs/marketing-expense-registry.json').read_bytes()
            api.services=[service(40,240)];update_source(root,api)
            def uncertain(*_): raise RuntimeError('simulated missing acknowledgment')
            with self.assertRaisesRegex(RuntimeError,'missing acknowledgment'):run(args,summary,runtime,root,api,DAY,uncertain)
            self.assertEqual((root/'runs/marketing-expense-registry.json').read_bytes(),registry)
            with self.assertRaisesRegex(RuntimeError,'outcome unknown'):run(args,summary,runtime,root,api,DAY,uncertain)

    def test_changed_base_without_matching_source_proof_is_not_announced(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);api=MemoryAPI([service()]);fixture(root,api)
            args=SimpleNamespace(dry_run=False); runtime={'mode':'owner_only','notifications_enabled':True}; summary={'status':'partial'}
            run(args,summary,runtime,root,api,DAY)
            before=(root/'runs/marketing-expense-registry.json').read_bytes()
            api.services=[service(40,240)]
            result=run(args,summary,runtime,root,api,DAY)
            self.assertEqual(result['status'],'requires_review');self.assertFalse(result['registry_committed'])
            self.assertEqual((root/'runs/marketing-expense-registry.json').read_bytes(),before)

    def test_source_checksum_change_and_shadow_history_cannot_send(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);api=MemoryAPI([service()]);fixture(root,api)
            args=SimpleNamespace(dry_run=False);runtime={'mode':'shadow','notifications_enabled':False};summary={'status':'completed'}
            run(args,summary,runtime,root,api,DAY)
            api.services=[service(40,240)];plan=update_source(root,api)
            Path(plan['source_file']).write_text('{"changed":true}',encoding='utf-8')
            self.assertEqual(run(args,summary,runtime,root,api,DAY)['status'],'requires_review')
            update_source(root,api)
            result=run(args,summary,runtime,root,api,DAY)
            self.assertEqual(result['candidate_events'],1);self.assertEqual(result['events_created'],0)
            runtime.update(mode='owner_only',notifications_enabled=True)
            self.assertEqual(run(args,summary,runtime,root,api,DAY)['candidate_events'],0)


if __name__ == '__main__': unittest.main()
