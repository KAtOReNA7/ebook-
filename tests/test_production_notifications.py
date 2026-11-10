import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import daily_run
from scripts.notification_policy import OWNER_ID, notification_recipients, enforce_recipients
from scripts.shared_runtime import capture, restore


class ProductionNotifications(unittest.TestCase):
    def policy(self):
        return {'mode':'live','deployment_stage':'production','production_approved':True,
                'recipient_open_ids':[OWNER_ID,'ou_verified_member'],
                'production_authorization':{'explicit_owner_request':True,
                    'accepted_on':'2026-11-10','request':'Owner accepted production'}}

    def write(self, root, value):
        daily_run.atomic_json(root/'config/runtime.json',value)

    def test_recipient_resolution_after_shared_restore_in_same_process(self):
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            a,b=Path(a),Path(b)
            self.write(a,self.policy())
            self.write(b,{'production_approved':False})
            with patch.object(daily_run,'ROOT',b):
                self.assertEqual(daily_run.notification_recipients(),[OWNER_ID])
                restore(b,capture(a,'2026-11-10'))
                self.assertEqual(daily_run.notification_recipients(),self.policy()['recipient_open_ids'])

    def test_exact_production_roster_and_stale_owner_only_rejected(self):
        with TemporaryDirectory() as d, patch('scripts.notification_policy.OWNER_CONFIGURED',True):
            root=Path(d); self.write(root,self.policy())
            good=[{'id':i} for i in self.policy()['recipient_open_ids']]
            enforce_recipients({'收件人':good},root)
            for ids in (good[:1],good+[{'id':'ou_unapproved'}],good+good,[]):
                with self.assertRaises(ValueError): enforce_recipients({'收件人':ids},root)

    def test_missing_approval_and_invalid_roster_fail_closed(self):
        cases=[{'production_authorization':{}},{'recipient_open_ids':[]},
               {'recipient_open_ids':['ou_other']},{'recipient_open_ids':[OWNER_ID,OWNER_ID]},
               {'mode':'owner_only'},{'deployment_stage':'debug'}]
        with TemporaryDirectory() as d:
            root=Path(d)
            for change in cases:
                self.write(root,{**self.policy(),**change})
                with self.subTest(change=change),self.assertRaises(ValueError): notification_recipients(root)

    def test_debug_cannot_expand_from_roster_alone(self):
        with TemporaryDirectory() as d:
            root=Path(d); self.write(root,{**self.policy(),'production_approved':False})
            self.assertEqual(notification_recipients(root),[OWNER_ID])

    def test_activation_policy_and_guard_are_portable(self):
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            a,b=Path(a),Path(b)
            daily_run.atomic_json(a/'config/production-activation.json',{'completed':True})
            daily_run.atomic_json(a/'runs/2026-11-10/production-activation.apply-state.json',{'phase':'finished'})
            restore(b,capture(a,'2026-11-10'))
            self.assertTrue(json.loads((b/'config/production-activation.json').read_text())['completed'])
            self.assertEqual(json.loads((b/'runs/2026-11-10/production-activation.apply-state.json').read_text())['phase'],'finished')
