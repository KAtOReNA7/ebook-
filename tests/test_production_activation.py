import copy
import unittest
from tools.activate_production import workflow_for_production, validate_authorization
from scripts.notification_policy import OWNER_ID


class ProductionActivationTests(unittest.TestCase):
    def test_preserve_message_content_and_only_trigger_two_new_report_kinds(self):
        original={'title':'old','steps':[
            {'id':'trigger_event','type':'AddRecordTrigger','data':{'table_name':'通知事件',
                'condition_list':[{'conditions':[{'field_name':'通知状态','operator':'is','value':['pending']}]}]}},
            {'id':'send_event','title':'old','data':{'receiver':[],'title':['title ref'],
                'content':['content ref'],'send_to_everyone':False}}]}
        prior=copy.deepcopy(original);people=[{'id':OWNER_ID,'name':'Owner'},{'id':'ou_member','name':'Member'}]
        actual=workflow_for_production(original,people)
        self.assertEqual(original,prior)
        send=actual['steps'][1]['data']
        self.assertEqual(send['receiver'],[{'value_type':'user','value':p} for p in people])
        self.assertEqual(send['content'],['content ref']);self.assertFalse(send['send_to_everyone'])
        groups=actual['steps'][0]['data']['condition_list']
        self.assertEqual({g['conditions'][-1]['value'][0]['value'] for g in groups},
                         {'v1:daily_report:ebook','v1:daily_report:weread'})
        self.assertTrue(all(g['conditions'][0]['field_name']=='通知状态' for g in groups))

    def test_new_roster_requires_verified_people_and_same_day_acceptance(self):
        auth={'date':'2026-11-10','explicit_owner_request':True,'resend_today':True,'request':'accepted',
              'people':[{'id':OWNER_ID,'name':'Owner','identity_verified':True}]}
        self.assertEqual(validate_authorization(auth,'2026-11-10'),[{'id':OWNER_ID,'name':'Owner'}])
        for update in ({'date':'2026-11-09'},{'explicit_owner_request':False},{'resend_today':False},
                       {'people':auth['people']*2},{'people':[{'id':OWNER_ID,'name':'Owner'}]}):
            with self.subTest(update=update),self.assertRaises(ValueError):
                validate_authorization({**auth,**update},'2026-11-10')
