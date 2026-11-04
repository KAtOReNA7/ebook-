import copy
import unittest
from scripts.rules import board_events, evaluate, fanqie_events, recommendation_events, tracking_due


def observation(platform="weread", day="2026-11-04", book_id="a", **values):
    return {"platform": platform, "book_id": book_id, "date": day, "valid": True, **values}


def readers(count, day="2026-11-04", **values):
    return observation("fanqie", day, cumulative_readers=count, readers_exact=True,
                       readers_metric="cumulative_readers", **values)


def board(day="2026-11-04"):
    return {"platform": "weread", "board_id": "weread_rising", "date": day,
            "valid": True, "complete": True,
            "entries": [{"book_id": str(i), "rank": i} for i in range(1, 51)]}


class RecommendationTests(unittest.TestCase):
    def test_boundaries_and_baseline(self):
        for before, now, kind in [(89.9, 90, "recommendation_up"),
                                   (90, 90.1, None), (90, 89.9, "recommendation_down"),
                                   (90, 90, None)]:
            with self.subTest(before=before, now=now):
                result = recommendation_events(observation(day="2026-11-03", recommendation=before),
                                               observation(recommendation=now))
                self.assertEqual([event["kind"] for event in result["events"]], [kind] if kind else [])
        self.assertTrue(recommendation_events(None, observation(recommendation=91))["baseline"])

    def test_missing_and_invalid_no_crossing(self):
        previous = observation(day="2026-11-03", recommendation=89)
        for value in [None, "暂无评分", "NaN", "Infinity", 101, True]:
            with self.subTest(value=value):
                result = recommendation_events(previous, observation(recommendation=value))
                self.assertTrue(result["pending"])
                self.assertFalse(result["events"])

    def test_identity_gap_and_dedup(self):
        previous = observation(day="2026-11-03", recommendation=89)
        current = observation(day="2026-11-06", recommendation=91)
        event = recommendation_events(previous, current)["events"][0]
        self.assertEqual(event["observation_gap_days"], 3)
        self.assertEqual(event, recommendation_events(previous, current)["events"][0])
        self.assertFalse(recommendation_events(previous, current, [event["event_id"]])["events"])
        self.assertTrue(recommendation_events(previous, {**current, "platform": "fanqie"})["pending"])
        self.assertTrue(recommendation_events(previous, {**current, "book_id": "other"})["pending"])


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.previous = board("2026-11-03")
        self.current = board()
        self.current["entries"][0]["book_id"] = "new"

    def test_first_is_baseline(self):
        result = board_events(None, self.current, [{"platform": "weread", "book_id": "new"}])
        self.assertTrue(result["baseline"])
        self.assertFalse(result["events"])

    def test_project_and_verified_motie_union(self):
        self.current["entries"][0].update(copyright_verified=True, copyright_group="磨铁")
        projects = [{"platform": "weread", "book_id": "new"}, {"platform": "weread", "book_id": "1"}]
        events = board_events(self.previous, self.current, projects)["events"]
        self.assertEqual(len(events), 2)
        entry = next(event for event in events if event["kind"] == "board_enter")
        self.assertEqual(entry["reasons"], ["project", "verified_motie"])
        self.assertEqual(entry["current_rank"], 1)
        self.assertFalse(board_events(self.previous, self.current, projects,
                                     [event["event_id"] for event in events])["events"])

    def test_partial_fail_closed(self):
        for mutation in (lambda b: b["entries"].pop(), lambda b: b.update(complete=False),
                         lambda b: b.update(valid=False),
                         lambda b: b["entries"][1].update(book_id="new"),
                         lambda b: b["entries"][1].update(rank=1)):
            snapshot = copy.deepcopy(self.current)
            mutation(snapshot)
            result = board_events(self.previous, snapshot, [{"platform": "weread", "book_id": "1"}])
            self.assertTrue(result["pending"])
            self.assertFalse(result["events"])

    def test_previous_invalid_and_wrong_board(self):
        previous = {**self.previous, "complete": False}
        self.assertTrue(board_events(previous, self.current)["pending"])
        wrong = {**self.current, "board_id": "weread_new"}
        self.assertTrue(board_events(self.previous, wrong)["pending"])

    def test_neither_title_nor_other_platform_enables_alert(self):
        self.current["entries"][0].update(title="磨铁", copyright_group="磨铁", copyright_verified=False)
        projects = [{"platform": "fanqie", "book_id": "new", "title": "磨铁"}]
        self.assertFalse(board_events(self.previous, self.current, projects)["events"])


class FanqieTests(unittest.TestCase):
    def test_strict_threshold_and_baseline(self):
        previous = readers(9999, "2026-11-03")
        self.assertFalse(fanqie_events(previous, readers(10000), "2026-11-03")["events"])
        self.assertEqual(len(fanqie_events(previous, readers(10001), "2026-11-03")["events"]), 1)
        self.assertTrue(fanqie_events(None, readers(21000), "2026-11-03")["baseline"])

    def test_window_boundaries(self):
        for threshold, inside, outside in [(20000, "2026-11-09", "2026-11-10"),
                                          (50000, "2026-11-16", "2026-11-17"),
                                          (100000, "2026-12-02", "2026-12-03")]:
            with self.subTest(threshold=threshold):
                previous = readers(threshold, "2026-11-03")
                valid = fanqie_events(previous, readers(threshold + 1, inside), "2026-11-03")
                invalid = fanqie_events(previous, readers(threshold + 1, outside), "2026-11-03")
                self.assertEqual([e["threshold"] for e in valid["events"]], [threshold])
                self.assertFalse(invalid["events"])

    def test_imprecise_or_wrong_metric_fail_closed(self):
        previous = readers(9000, "2026-11-03")
        for change in [{"readers_exact": False}, {"cumulative_readers": "1.2万"},
                       {"cumulative_readers": 12000.0}, {"readers_metric": "currently_reading"}]:
            with self.subTest(change=change):
                result = fanqie_events(previous, {**readers(12000), **change}, "2026-11-03")
                self.assertTrue(result["pending"])
                self.assertFalse(result["events"])

    def test_all_milestones_and_lifetime_dedup(self):
        result = fanqie_events(readers(9000, "2026-11-03"), readers(110000, "2026-11-08"), "2026-11-03")
        self.assertEqual([e["threshold"] for e in result["events"]], [10000, 20000, 50000, 100000])
        emitted = [event["event_id"] for event in result["events"]]
        recross = fanqie_events(readers(9000, "2026-11-08"), readers(110000, "2026-11-09"), "2026-11-03", emitted)
        self.assertFalse(recross["events"])

    def test_first_already_above_does_not_recross_without_drop(self):
        result = fanqie_events(readers(21000, "2026-11-03"), readers(22000), "2026-11-03")
        self.assertFalse(result["events"])

    def test_initial_overshoot_survives_drop_and_recovery(self):
        initial = fanqie_events(None, readers(21000, "2026-11-03"), "2026-11-03")
        self.assertEqual(initial["reached_thresholds"], [10000, 20000])
        self.assertFalse(initial["events"])
        decline = fanqie_events(readers(21000, "2026-11-03"), readers(9000), "2026-11-03",
                                known_reached=initial["reached_thresholds"])
        recovery = fanqie_events(readers(9000), readers(21000, "2026-11-05"), "2026-11-03",
                                 known_reached=decline["reached_thresholds"])
        self.assertFalse(recovery["events"])
        self.assertEqual(recovery["reached_thresholds"], [10000, 20000])


class TrackingTests(unittest.TestCase):
    def test_inclusive_calendar_window_and_exit_does_not_stop(self):
        enrollment = [{"platform": "weread", "book_id": "a", "start_date": "2026-11-03", "source": "board"}]
        self.assertEqual(len(tracking_due(enrollment, "2026-11-03")["due"]), 1)
        self.assertEqual(len(tracking_due(enrollment, "2026-12-02")["due"]), 1)
        self.assertFalse(tracking_due(enrollment, "2026-12-03")["due"])
        self.assertEqual(len(tracking_due(enrollment, "2026-12-03", [{"platform": "weread", "book_id": "a"}])["due"]), 1)

    def test_union_and_platform_identity(self):
        enrollments = [
            {"platform": "weread", "book_id": "a", "start_date": "2026-11-03", "source": "project"},
            {"platform": "weread", "book_id": "a", "start_date": "2026-11-05", "source": "board"},
            {"platform": "fanqie", "book_id": "a", "start_date": "2026-11-03", "source": "project"}]
        result = tracking_due(enrollments, "2026-11-06")
        self.assertEqual(len(result["due"]), 2)
        weread = next(row for row in result["due"] if row["platform"] == "weread")
        self.assertEqual(weread["minimum_end_date"], "2026-12-04")
        self.assertEqual(weread["reasons"], ["project", "board"])

    def test_dispatcher(self):
        result = evaluate({"rule": "recommendation", "current": observation(recommendation=95)})
        self.assertTrue(result["baseline"])


if __name__ == "__main__":
    unittest.main()
