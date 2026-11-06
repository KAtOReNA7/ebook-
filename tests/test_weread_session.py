import unittest
from scripts.weread_session import refreshed


class SessionTests(unittest.TestCase):
    def test_renew_keeps_identity_and_device(self):
        old={'vid':123,'deviceId':'test-device','refreshToken':'old-refresh','accessToken':'old-access'}
        result=refreshed(old,{'vid':'123','accessToken':'new-access'})
        self.assertEqual(result['deviceId'],'test-device')
        self.assertEqual(result['refreshToken'],'old-refresh')
        self.assertEqual(old['accessToken'],'old-access')

    def test_account_switch_and_invalid_result_rejected(self):
        old={'vid':123,'refreshToken':'test-refresh'}
        for result in ({'vid':456,'accessToken':'token'}, {}, {'accessToken':'x','refreshToken':None}):
            with self.assertRaises(ValueError): refreshed(old,result)
