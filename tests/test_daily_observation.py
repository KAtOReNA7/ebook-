import unittest
from scripts.daily_observation import select_daily_observations


class DailyObservationTests(unittest.TestCase):
    def test_older_replay_does_not_overwrite_newer_observation(self):
        old={'日数据唯一键':'2026-11-03:weread:1','采集时间':'2026-11-03T14:32:00.000+08:00','点评人数':18}
        incoming={**old,'采集时间':'2026-11-03 11:16','点评人数':15}
        self.assertEqual(select_daily_observations([incoming],[old]),[])

    def test_newer_unavailable_value_clears_old_value(self):
        old={'日数据唯一键':'2026-11-03:weread:1','采集时间':'2026-11-03T11:16:00.000+08:00','推荐值':90}
        incoming={**old,'采集时间':'2026-11-03 14:32','推荐值':None,'原始证据定位':'new-source'}
        self.assertEqual(select_daily_observations([incoming],[old]),[incoming])

    def test_different_days_are_not_merged_and_new_nulls_omitted(self):
        old={'日数据唯一键':'2026-11-02:weread:1','采集时间':'2026-11-02 10:00','推荐值':90}
        incoming={'日数据唯一键':'2026-11-03:weread:1','采集时间':'2026-11-03 10:00','推荐值':None}
        self.assertEqual(select_daily_observations([incoming],[old]),[{'日数据唯一键':incoming['日数据唯一键'],'采集时间':incoming['采集时间']}])
