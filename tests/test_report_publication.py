import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock,patch
from scripts.daily_reports import flush,event_row
from scripts.report_publication import source_issues,readback_issues,evidence_issues,BOARDS
from scripts.initialize_manual_projects import unknown_guard_paths
from tests.test_operational_validation import workflow_fixture


def ready_pipeline(day='2026-11-11'):
    modules={name:{'status':'completed'} for name in ('project_initialization','project_notifications','marketing_expense_notifications')}
    modules.update({
        'tencent':{'status':'partial','import_readback':'passed','import':{'reconciliation':'passed'},'source_changes':[]},
        'weread':{'status':'partial','coverage':{'requested_books':10,'successful_books':10,'missing_metric_fields':{'1':['recommendation_percent']}}},
        'fanqie':{'status':'partial','readback':'passed','projects':2,'observations':2},
        'fanqie_rising':{'status':'completed','complete':True,'count':100,'readback':'passed'},
        'publisher_recent':{'status':'completed','source':{'complete':True},'readback':'passed','sampled_books':2,'active_targets':2,'metric_errors':0},
        'notification_comparison':{'status':'completed','modules':{'weread':{'status':'handled_by_source'},'fanqie_rising':{'status':'compared'}}},
        'report_validation':{'status':'completed','date':day},
    })
    return {'date':day,'modules':modules}


def complete_evidence():
    return {'copyright_ranking':[{'board':b,'counts':{'other':50},'unknown':[]} for b in
            ('rising','newbook','novel_female_rising','newbook_novel_female')],
            'fanqie_publication_rising':{'complete':True,'ranking_count':100,'unknown':[]}}


class ReportPublicationTests(unittest.TestCase):
    def run_flush(self,pipeline,evidence=None,records=None,hold=False,guard=False,workflow=None):
        with TemporaryDirectory() as d:
            root=Path(d);(root/'config').mkdir()
            (root/'config/runtime.json').write_text(json.dumps({'report_publication_hold':{'active':hold},'native_workflow':'flow','recipient_open_ids':['owner']}))
            if guard:
                p=root/'runs'/pipeline['date'];p.mkdir(parents=True)
                (p/'daily-state.json').write_text('{"phase":"requires_review"}')
            api=Mock();api.read.side_effect=[records or [],[],[]];api.call.return_value={'data':workflow if workflow is not None else workflow_fixture()};writer=Mock()
            api._fields.return_value={'通知标题':{'id':'title_id'},'通知正文':{'id':'body_id'}}
            with patch('scripts.daily_run.shanghai_day',return_value=pipeline['date']), \
                 patch('scripts.notification_policy.notification_recipients',return_value=['owner']), \
                 patch('scripts.daily_reports.summary_body',return_value=('verified report',evidence if evidence is not None else complete_evidence())):
                result=flush(SimpleNamespace(dry_run=False),pipeline,{'notifications_enabled':True},root,api,writer)
            return result,writer.call_count

    def test_each_failed_or_missing_module_blocks_all_messages(self):
        for name in ready_pipeline()['modules']:
            for status in ('failed','blocked','requires_review','source_review_required','waiting_evidence',None):
                with self.subTest(module=name,status=status):
                    p=ready_pipeline()
                    if status is None:del p['modules'][name]
                    else:p['modules'][name]['status']=status
                    result,calls=self.run_flush(p)
                    self.assertEqual(result['status'],'requires_review');self.assertEqual(calls,0)

    def test_actual_incident_shape_zero_writes(self):
        p=ready_pipeline();p['modules']['weread']={'status':'failed','error':'RemoteDisconnected'}
        p['modules']['fanqie_rising']={'status':'requires_review'}
        p['modules']['publisher_recent']={'status':'requires_review'}
        p['modules']['notification_comparison']={'status':'requires_review'}
        result,calls=self.run_flush(p)
        self.assertEqual(calls,0);self.assertEqual(result['status'],'requires_review')

    def test_incomplete_copyrights_or_boards_block_both_reports(self):
        for scope in ('copyright_ranking','fanqie_publication_rising'):
            ev=complete_evidence()
            ev[scope]=[] if scope=='copyright_ranking' else {'complete':False}
            self.assertEqual(self.run_flush(ready_pipeline(),ev)[1],0)
        ev=complete_evidence();ev['copyright_ranking'][0]['unknown']=['1']
        self.assertEqual(self.run_flush(ready_pipeline(),ev)[1],0)

    def test_nested_waiting_comparison_blocks(self):
        p=ready_pipeline();p['modules']['notification_comparison']['modules']['weread']['status']='waiting_evidence'
        self.assertEqual(self.run_flush(p)[1],0)

    def test_missing_observation_counts_cannot_approve_publication(self):
        for module,fields in [('fanqie',('projects','observations')),
                              ('publisher_recent',('sampled_books','active_targets'))]:
            p=ready_pipeline()
            for field in fields:p['modules'][module].pop(field)
            self.assertEqual(self.run_flush(p)[1],0)

    def test_transport_hold_precedes_old_resend_authorization(self):
        from scripts.notification_policy import enforce_recipients
        with TemporaryDirectory() as d:
            root=Path(d);(root/'config').mkdir()
            (root/'config/runtime.json').write_text('{"report_publication_hold":{"active":true}}')
            with patch('scripts.notification_policy.OWNER_CONFIGURED',True), \
                 patch('scripts.notification_policy.authorized_resend',return_value=True) as resend:
                with self.assertRaisesRegex(ValueError,'paused'):
                    enforce_recipients({'事件唯一键':'daily-report:v1:resend:old'},root)
                resend.assert_not_called()

    def test_current_unknown_source_guard_blocks(self):
        self.assertEqual(self.run_flush(ready_pipeline(),guard=True)[1],0)

    def test_roster_change_after_full_readback_still_blocks_writer(self):
        workflow=workflow_fixture();workflow['steps'][2]['data']['receiver'][0]['value']['id']='unauthorized'
        result,calls=self.run_flush(ready_pipeline(),workflow=workflow)
        self.assertEqual(calls,0);self.assertEqual(result['status'],'requires_review')

    def test_owner_hold_blocks_even_with_all_checks_complete(self):
        result,calls=self.run_flush(ready_pipeline(),hold=True)
        self.assertEqual(result['status'],'paused');self.assertEqual(calls,0)

    def test_complete_zero_changes_sends_two_reports(self):
        result,calls=self.run_flush(ready_pipeline())
        self.assertEqual(result['messages_created'],2);self.assertEqual(calls,1)

    def test_existing_reports_not_replayed_after_recovery(self):
        p=ready_pipeline();rows=[event_row(p['date'],k,'saved',{},['owner']) for k in ('ebook','weread')]
        result,calls=self.run_flush(p,records=rows)
        self.assertEqual(result['messages_created'],0);self.assertEqual(calls,0)

    def test_full_base_readback_needs_every_board_and_reconciliation(self):
        valid={'readback':'passed','all_keys_unique':True,'service_links_valid':True,'fee_reconciliation':'passed','complete_board_counts':BOARDS,
               'source_contract':{'status':'passed'},'native_workflow_verification':{'status':'passed'}}
        self.assertEqual(readback_issues(valid),[])
        for key in valid:
            bad=copy.deepcopy(valid);bad.pop(key)
            self.assertTrue(readback_issues(bad))

    def test_all_three_source_unknown_guards_recognized(self):
        with TemporaryDirectory() as d:
            root=Path(d);p=root/'runs/2026-11-11';p.mkdir(parents=True)
            for name in ('daily-state.json','tencent-state.json','fanqie-state.json'):
                (p/name).write_text('{"phase":"requires_review"}')
            self.assertEqual(len(unknown_guard_paths(root)),3)

    def test_final_readback_runs_only_after_all_sources_and_rejects_stale_result(self):
        from scripts import daily_run
        from datetime import datetime,timezone
        with TemporaryDirectory() as d:
            root=Path(d);p=root/'runs/2026-11-11';p.mkdir(parents=True)
            evidence={'date':'2026-11-11','verified_at':'2026-11-11T10:00:00+08:00','readback':'passed','all_keys_unique':True,
                      'service_links_valid':True,'fee_reconciliation':'passed','complete_board_counts':BOARDS}
            (p/'acceptance-summary.json').write_text(json.dumps(evidence))
            with patch.object(daily_run,'ROOT',root),patch.object(daily_run,'call_script') as call, \
                 patch.object(daily_run,'now',return_value=datetime(2026,11,11,3,tzinfo=timezone.utc)):
                result=daily_run.run_report_validation(SimpleNamespace(dry_run=False),ready_pipeline())
                self.assertEqual(result['status'],'requires_review');call.assert_called_once()
                call.reset_mock();pipeline=ready_pipeline();pipeline['modules']['weread']['status']='failed'
                result=daily_run.run_report_validation(SimpleNamespace(dry_run=False),pipeline)
                self.assertEqual(result['status'],'requires_review');call.assert_not_called()
