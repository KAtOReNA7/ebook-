import unittest

from tools.doctor import valid_ranking_policy


class RankingPolicyTests(unittest.TestCase):
    def test_pause_and_authorized_collection_stay_valid(self):
        self.assertTrue(valid_ranking_policy({'status': 'user_paused', 'enabled': False}))
        self.assertTrue(valid_ranking_policy({'status': 'web_daily', 'enabled': True,
                                             'resumed_by_owner': True, 'notifications_enabled': False}))

    def test_observation_notifications_require_complete_explicit_policy(self):
        config = {'status': 'web_daily', 'enabled': True, 'resumed_by_owner': True,
                  'notifications_enabled': True,
                  'notification_basis': 'consecutive_complete_observations',
                  'notifications_effective_from': '2026-11-06'}
        self.assertTrue(valid_ranking_policy(config))
        for key in ('resumed_by_owner', 'notification_basis', 'notifications_effective_from'):
            incomplete = dict(config)
            del incomplete[key]
            self.assertFalse(valid_ranking_policy(incomplete), key)
        self.assertFalse(valid_ranking_policy({**config, 'notifications_effective_from': 'tomorrow'}))
        self.assertFalse(valid_ranking_policy({**config, 'status': 'user_paused'}))


if __name__ == '__main__':
    unittest.main()
