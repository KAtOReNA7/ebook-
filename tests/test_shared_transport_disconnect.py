import io
import unittest
from http.client import RemoteDisconnected
from unittest.mock import patch, Mock
from scripts import shared_runtime


class SharedTransportDisconnectTests(unittest.TestCase):
    def call(self, effects, payload=None):
        opener=Mock()
        opener.open.side_effect=effects
        with patch.object(shared_runtime, '_github_token', 'synthetic-test-token'), \
             patch.object(shared_runtime, 'transport_env', return_value={}), \
             patch.object(shared_runtime.urllib.request, 'build_opener', return_value=opener):
            result=shared_runtime.gh('test/repo','ref/heads/test',payload)
        return result,opener.open.call_count

    def test_read_retries_disconnect_then_returns_response(self):
        result,count=self.call([RemoteDisconnected('closed'),io.BytesIO(b'{"ok": true}')])
        self.assertEqual(result,{'ok':True})
        self.assertEqual(count,2)

    def test_write_disconnect_never_retries(self):
        opener=Mock()
        opener.open.side_effect=RemoteDisconnected('closed')
        with patch.object(shared_runtime,'_github_token','synthetic-test-token'), \
             patch.object(shared_runtime,'transport_env',return_value={}), \
             patch.object(shared_runtime.urllib.request,'build_opener',return_value=opener):
            with self.assertRaisesRegex(RuntimeError,'RemoteDisconnected'):
                shared_runtime.gh('test/repo','refs/heads/test',{'sha':'test'})
        self.assertEqual(opener.open.call_count,1)

    def test_read_disconnect_is_bounded(self):
        opener=Mock()
        opener.open.side_effect=RemoteDisconnected('closed')
        with patch.object(shared_runtime,'_github_token','synthetic-test-token'), \
             patch.object(shared_runtime,'transport_env',return_value={}), \
             patch.object(shared_runtime.urllib.request,'build_opener',return_value=opener):
            with self.assertRaisesRegex(RuntimeError,'Coordination API failed'):
                shared_runtime.gh('test/repo','ref/heads/test')
        self.assertEqual(opener.open.call_count,3)
