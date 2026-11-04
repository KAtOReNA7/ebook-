import unittest
from scripts.fanqie_daily import milestone_step


def book(lower,upper):
    return {'book_id':'12345678901','cumulative_readers_metric':{
        'lower_bound':lower,'upper_bound':upper,'raw':str(lower)+'..'+str(upper)}}


class Milestones(unittest.TestCase):
    def test_initial_overshoot_is_baseline_and_never_realerts(self):
        first=milestone_step(book(30000,31000),'2026-11-03','2026-11-03')
        self.assertEqual(first['events'],[])
        second=milestone_step(book(1000,2000),'2026-11-03','2026-11-04',first['state'])
        third=milestone_step(book(30000,31000),'2026-11-03','2026-11-05',second['state'])
        self.assertEqual(third['events'],[])

    def test_rounded_days_keep_last_proven_below(self):
        first=milestone_step(book(9000,9500),'2026-11-03','2026-11-03')
        uncertain=milestone_step(book(9000,11000),'2026-11-03','2026-11-04',first['state'])
        self.assertFalse(uncertain['events'])
        crossed=milestone_step(book(11000,13000),'2026-11-03','2026-11-05',uncertain['state'])
        self.assertEqual([e['threshold'] for e in crossed['events']],[10000])
        self.assertEqual(crossed['events'][0]['previous_date'],'2026-11-03')
        repeat=milestone_step(book(15000,17000),'2026-11-03','2026-11-06',crossed['state'])
        self.assertFalse(repeat['events'])

    def test_strict_and_window_boundaries(self):
        first=milestone_step(book(20000,20000),'2026-11-03','2026-11-03')
        day6=milestone_step(book(21000,23000),'2026-11-03','2026-11-09',first['state'])
        self.assertEqual([e['threshold'] for e in day6['events']],[20000])
        day7=milestone_step(book(21000,23000),'2026-11-03','2026-11-10',first['state'])
        self.assertFalse(day7['events'])

    def test_scope_mismatch_and_replay(self):
        first=milestone_step(book(2000,2000),'2026-11-03','2026-11-03')
        replay=milestone_step(book(30000,30000),'2026-11-03','2026-11-03',first['state'])
        self.assertFalse(replay['events'])
        with self.assertRaises(ValueError):
            milestone_step(book(30000,30000),'2026-11-04','2026-11-05',first['state'])


if __name__=='__main__':
    unittest.main()
