from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts import merge_project_aliases as merge


def fixture():
    projects, services, identities = [], [], {}
    for i, target in enumerate(merge.TARGETS):
        bid = target.split(':')[1]
        count = 2 if i == 2 else 1
        for j in range(count):
            name = 'book-' + str(i) + '-' + str(j)
            rid = merge.PREFERRED[target] if i == 2 and j == 0 else 'pending-' + name
            key = 'pending:weread:ebook:' + name
            sid = 'service-' + name
            projects.append({'record_id': rid, '书名': name, '作品唯一键': key,
                             '监测范围': ['营销项目'], '平台': ['微信读书'], '作品形式': '电子书',
                             '首次进入项目总表': '2026-11-03T00:00:00+08:00',
                             '追踪截止日': '2026-12-02T00:00:00+08:00',
                             '营销服务明细': [{'id': sid}], '证据与备注': 'original',
                             '每日记录': [], '上榜记录': [], '通知记录': []})
            services.append({'record_id': sid, '服务明细唯一键': 'unique-' + name,
                             '作品唯一键': key, '关联项目': [{'id': rid}], '原始行总金额': 40 + j,
                             '已确认分摊金额': 40 + j, '邀评人数': 10 + j, '原始书名': name})
            identities[('weread', 'ebook', name)] = {
                'verified': True, 'book_id': bid, 'source_title': name,
                'title': 'official-' + str(i), 'author': 'author',
                'detail_url': 'https://weread.qq.com/web/reader/' + bid}
        if i < 2:
            projects.append({'record_id': 'canonical-' + str(i), '书名': 'official-' + str(i),
                             '作品唯一键': target, '平台作品ID': bid,
                             '监测范围': ['仅榜单样本'], '营销服务明细': [],
                             '首次进入项目总表': '2026-11-01T00:00:00+08:00' if i == 0 else None,
                             '追踪截止日': '2026-12-09T00:00:00+08:00',
                             '每日记录': [{'id': 'historical-' + str(i)}], '上榜记录': [], '通知记录': []})
    return projects, services, identities


def applied(plan, projects, services):
    projects, services = deepcopy(projects), deepcopy(services)
    for row in services:
        row.update(plan['service_updates'].get(row['record_id'], {}))
    for row in projects:
        row.update(plan['project_updates'].get(row['record_id'], {}))
        row['营销服务明细'] = [{'id': s['record_id']} for s in services
                              if merge.links(s['关联项目']) == [row['record_id']]]
    return projects, services


class AliasMergeTests(unittest.TestCase):
    def test_preserves_existing_canonical_earliest_start_and_longest_deadline(self):
        p, s, ids = fixture()
        plan = merge.build_plan(p, s, ids)
        field = plan['project_updates']['canonical-0']
        self.assertEqual(field['首次进入项目总表'], '2026-11-01T00:00:00+08:00')
        self.assertEqual(field['追踪截止日'], '2026-12-09T00:00:00+08:00')
        self.assertEqual(plan['groups'][2]['canonical_record_id'], 'reczz28V47quenUw')
        for update in plan['service_updates'].values():
            self.assertEqual(set(update), {'作品唯一键', '关联项目'})
        aliases = {a['record_id'] for g in plan['groups'] for a in g['aliases']}
        for rid in aliases:
            self.assertNotIn('作品唯一键', plan['project_updates'][rid])
            self.assertEqual(plan['project_updates'][rid]['监测范围'], ['别名留档'])
        merge.readback(plan, *applied(plan, p, s))

    def test_rejects_unverified_identity_duplicate_key_and_crosslinked_service(self):
        for mode in ('unverified', 'duplicate', 'crosslink'):
            p, s, ids = fixture()
            if mode == 'unverified':
                next(iter(ids.values()))['verified'] = False
            elif mode == 'duplicate':
                p[-1]['作品唯一键'] = p[0]['作品唯一键']
            else:
                s[0]['关联项目'] = [{'id': p[-1]['record_id']}]
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                merge.build_plan(p, s, ids)

    def test_readback_detects_net_zero_amount_swap_and_wrong_links(self):
        p, s, ids = fixture()
        plan = merge.build_plan(p, s, ids)
        after_p, after_s = applied(plan, p, s)
        after_s[0]['原始行总金额'] += 1
        after_s[1]['原始行总金额'] -= 1
        with self.assertRaisesRegex(RuntimeError, 'conservation'):
            merge.readback(plan, after_p, after_s)
        after_p, after_s = applied(plan, p, s)
        after_s[0]['关联项目'] = [{'id': 'wrong'}]
        with self.assertRaisesRegex(RuntimeError, 'link readback'):
            merge.readback(plan, after_p, after_s)
        after_p, after_s = applied(plan, p, s)
        after_p.append({'record_id': 'unexpected', '作品唯一键': 'unexpected'})
        with self.assertRaisesRegex(RuntimeError, 'Project count'):
            merge.readback(plan, after_p, after_s)

    def test_stale_plan_and_interruption_never_retry(self):
        p, s, ids = fixture()
        approved = {'plan': merge.build_plan(p, s, ids)}
        with TemporaryDirectory() as tmp:
            class FakeBase:
                receipt_dir = Path(tmp) / 'receipts' / 'day' / 'attempt'
                manifest = {'tables': {'项目总表': {'field_readback': {'fields': [
                    {'name': '监测范围', 'options': [{'name': '别名留档'}]}]}}}}
                calls = []
                def read(self, table):
                    return deepcopy(p if table == '项目总表' else s)
                def call(self, command, table, payload):
                    self.calls.append((table, payload))
                    raise TimeoutError('unknown external result')
            api = FakeBase()
            s[0]['邀评人数'] += 1
            with self.assertRaisesRegex(RuntimeError, 'stale'):
                merge.apply_plan(api, approved, ids)
            self.assertEqual(api.calls, [])
            s[0]['邀评人数'] -= 1
            with self.assertRaises(TimeoutError):
                merge.apply_plan(api, approved, ids)
            self.assertEqual(len(api.calls), 1)
            state = json.loads((api.receipt_dir / 'link-state.json').read_text(encoding='utf-8'))
            self.assertNotEqual(state['phase'], 'finished')
            with self.assertRaisesRegex(RuntimeError, 'Unknown/incomplete'):
                merge.assert_no_unknown(Path(tmp))

    def test_rejects_changed_source_service_count_even_when_totals_same(self):
        p, s, ids = fixture()
        plan = merge.build_plan(p, s, ids)
        p2, s2 = applied(plan, p, s)
        s2.append({'record_id': 'new', '服务明细唯一键': 'new', '原始行总金额': 0,
                   '已确认分摊金额': 0, '邀评人数': 0})
        with self.assertRaisesRegex(RuntimeError, 'conservation'):
            merge.readback(plan, p2, s2)


if __name__ == '__main__':
    unittest.main()
