import unittest

from scripts.link_project_identities import guard_duplicate_targets


class IdentityCollisionTests(unittest.TestCase):
    def test_two_source_aliases_require_merge_without_blocking_unrelated_book(self):
        # Real failure mode: punctuation variants become one official book ID.
        projects = {'alias1': {'作品唯一键': 'weread:3300193302'},
                    'alias2': {'作品唯一键': 'weread:3300193302'},
                    'other': {'作品唯一键': 'weread:123'}}
        services = {'paid30': {'作品唯一键': 'weread:3300193302'},
                    'paid10': {'作品唯一键': 'weread:3300193302'},
                    'other_service': {'作品唯一键': 'weread:123'}}
        ready, linked, pending = guard_duplicate_targets(projects, services)
        self.assertEqual(set(ready), {'other'})
        self.assertEqual(set(linked), {'other_service'})
        self.assertEqual(set(pending[0]['project_record_ids']), {'alias1', 'alias2'})
        self.assertEqual(pending[0]['reason'], 'multiple_pending_aliases_same_platform_id_merge_required')
        self.assertEqual(len(projects), 3)
        self.assertEqual(len(services), 3)


if __name__ == '__main__':
    unittest.main()
