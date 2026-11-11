import copy,json,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from scripts.operational_validation import compare_rows,data_contract,workflow_issues,minute


def workflow_fixture():
    groups=[{'conjunction':'and','conditions':[
        {'field_name':'通知状态','operator':'is','value':[{'value':{'name':'待发送'}}]},
        {'field_name':'规则ID','operator':'is','value':[{'value':'v1:daily_report:'+kind}]}]} for kind in ('ebook','weread')]
    return {'status':'enabled','steps':[
        {'id':'trigger_event','type':'AddRecordTrigger','next':'mark_sending','data':{'condition_list':groups}},
        {'id':'mark_sending','type':'SetRecordAction','next':'send_event'},
        {'id':'send_event','type':'LarkMessageAction','next':'mark_sent',
         'data':{'receiver':[{'value_type':'user','value':{'id':'owner'}}],'send_to_everyone':False,
                 'title':[{'value_type':'ref','value':'$.trigger_event.title_id'}],
                 'content':[{'value_type':'ref','value':'$.trigger_event.body_id'}],'content_type':'plain_text'}},
        {'id':'mark_sent','type':'SetRecordAction'}]}


class OperationalValidationTests(unittest.TestCase):
    def test_workflow_roster_order_trigger_and_previous_failures(self):
        runtime={'recipient_open_ids':['owner']};wf=workflow_fixture()
        self.assertEqual(workflow_issues(wf,runtime,[]),[])
        mutations=[lambda w:w.update(status='disabled'),
                   lambda w:w['steps'][2]['data'].update(send_to_everyone=True),
                   lambda w:w['steps'][2]['data']['receiver'][0]['value'].update(id='other'),
                   lambda w:w['steps'][0].update(type='UpdateRecordTrigger'),
                   lambda w:w['steps'][0]['data']['condition_list'][0].update(conjunction='or'),
                   lambda w:w['steps'][1].update(next='mark_sent')]
        for mutate in mutations:
            bad=copy.deepcopy(wf);mutate(bad);self.assertTrue(workflow_issues(bad,runtime,[]))
        for status in ('发送失败','结果未知','发送中','待发送'):
            self.assertTrue(workflow_issues(wf,runtime,[{'通知状态':[status]}]))
        fields={'通知标题':{'id':'title_id'},'通知正文':{'id':'body_id'}}
        self.assertEqual(workflow_issues(wf,runtime,[],fields),[])
        wf['steps'][2]['data']['content'][0]['value']='wrong_field'
        self.assertTrue(workflow_issues(wf,runtime,[],fields))

    def test_equal_counts_cannot_hide_wrong_ids_or_metrics(self):
        expected=[{'key':'a','metric':2},{'key':'b','metric':3}]
        compare_rows(expected,expected,'key','test')
        for actual in ([{'key':'a','metric':3},{'key':'b','metric':2}],
                       [{'key':'a','metric':2},{'key':'c','metric':3}],expected[:1]):
            with self.assertRaises(ValueError):compare_rows(expected,actual,'key','test')

    def test_timezone_is_shanghai_not_host_wall_clock(self):
        self.assertEqual(minute('2026-11-10T18:00:00-05:00'),'2026-11-11 07:00')

    def test_source_contract_chooses_latest_and_rejects_tampered_daily(self):
        day='2026-11-11'
        with TemporaryDirectory() as d:
            root=Path(d);directory=root/'runs'/day;directory.mkdir(parents=True)
            def write(name,value):
                path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
            book={'book_id':'1','reviewer_count':2,'word_count':100,'recommendation_percent':90,
                  'today_reading_count':3,'detail_source':{'observed_at':day+'T10:00:00+08:00'}}
            snapshot={'collection_date':day,'finished_at':day+'T10:01:00+08:00','books':[book],'charts':[]}
            write('weread.json',snapshot)
            latest=copy.deepcopy(book);latest.update(today_reading_count=4);latest['detail_source']['observed_at']=day+'T10:02:00+08:00'
            write('recent/metrics.json',{**snapshot,'books':[latest]})
            write('publisher-recent-state.json',{'phase':'finished','summary':{'sync':{'source':str(directory/'recent/metrics.json')}}})
            write('fanqie/snapshot.json',{**snapshot,'books':[]})
            write('tencent-daily-import-plan.json',{'safe_to_apply':True,'import_day':day,'services':[]})
            projects=[{'作品唯一键':'weread:1','record_id':'p1'}]
            row={'日数据唯一键':day+':weread:1','作品唯一键':'weread:1','关联项目':[{'id':'p1'}],
                 '平台':['微信读书'],'数据日期':day+' 00:00','采集时间':day+' 10:02',
                 '点评人数':2,'字数':100,'推荐值':90,'今日阅读人数':4}
            with patch('scripts.fanqie_rising_daily.verified_snapshot',return_value={'books':[]}):
                self.assertEqual(data_contract(root,day,projects,[row],[],[])['status'],'passed')
                for field,value in [('今日阅读人数',3),('关联项目',[{'id':'wrong'}]),('点评人数',None)]:
                    with self.assertRaises(ValueError):data_contract(root,day,projects,[{**row,field:value}],[],[])
