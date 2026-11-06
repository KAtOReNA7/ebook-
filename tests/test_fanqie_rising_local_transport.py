import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
from scripts.collect_fanqie_rising import ranking_archive
from scripts.fanqie_rising_daily import exit_observations


class LocalTransportTests(unittest.TestCase):
    def test_ranking_uses_explicit_direct_proxy_handler(self):
        with TemporaryDirectory() as temp, patch('scripts.collect_fanqie_rising.urllib.request.ProxyHandler') as handler, \
                patch('scripts.collect_fanqie_rising.urllib.request.build_opener'):
            ranking_archive(Path(temp)/'new')
            handler.assert_called_once_with({})

    def test_exit_requires_adjacent_complete_observations(self):
        old = {'complete':True,'started_at':'2026-11-05T10:00:00+08:00',
               'books':[{'book_id':'123','rank':7}]}
        current = {'complete':True,'started_at':'2026-11-06T10:00:00+08:00','books':[]}
        result = exit_observations(old,current,{'123':{}})
        self.assertEqual(result[0]['previous_rank'],7)
        self.assertEqual(result[0]['kind'], 'board_exit')
        self.assertNotIn('event_id', result[0])  # Observation alone does not authorize delivery.
        self.assertEqual(exit_observations(old,{**current,'complete':False},{'123':{}}),[])
        self.assertEqual(exit_observations(old,{**current,'started_at':'2026-11-07T10:00:00+08:00'},{'123':{}}),[])


if __name__ == '__main__': unittest.main()
