import unittest
from unittest.mock import Mock

from tools.weread_login import request_qr, poll, exchange


class LoginTests(unittest.TestCase):
    def test_qr_requires_valid_ticket_and_server_uuid(self):
        fetch = Mock(side_effect=[{'signature': 'fixture', 'timeStamp': 123},
                                  {'errcode': 0, 'uuid': 'fixture-uuid'}])
        self.assertEqual(request_qr(fetch), 'fixture-uuid')
        self.assertTrue(fetch.call_args.args[0].startswith('https://open.weixin.qq.com/connect/sdk/qrconnect?'))
        for body in ({}, {'signature': '', 'timeStamp': 1}):
            with self.assertRaises(ValueError):
                request_qr(Mock(return_value=body))

    def test_poll_never_exchanges_without_user_confirmation(self):
        for body in ({'wx_errcode': 403}, {'wx_errcode': 402}, {'wx_errcode': 405}, {'wx_errcode': 999}):
            with self.assertRaises(ValueError):
                poll('fixture', fetch=Mock(return_value=body))
        fetch = Mock(side_effect=[{'wx_errcode': 404}, {'wx_errcode': 405, 'wx_code': 'confirmed'}])
        self.assertEqual(poll('fixture', fetch=fetch, sleep=Mock()), 'confirmed')

    def test_poll_deadline_is_bounded(self):
        with self.assertRaises(TimeoutError):
            poll('fixture', seconds=30, clock=Mock(side_effect=[0, 31]))

    def test_exchange_only_retains_required_fields(self):
        fetch = Mock(return_value={'vid': '123', 'accessToken': 'fixture-access',
                                   'refreshToken': 'fixture-refresh', 'unrelated': 'omit'})
        result = exchange('fixture-code', fetch)
        self.assertEqual(set(result), {'vid', 'accessToken', 'refreshToken', 'deviceId'})
        self.assertEqual(fetch.call_args.args[1]['deviceId'], result['deviceId'])
        for body in ({'errCode': 1}, {'vid': '123', 'accessToken': 'a'}):
            with self.assertRaises(ValueError):
                exchange('fixture-code', Mock(return_value=body))
