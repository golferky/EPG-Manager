import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mac"))

import name_episodes as ne  # noqa: E402

EPISODES = [
    {"season": 1, "number": 1, "name": "The Lady in the Bottle",
     "summary": "<p>Astronaut Tony Nelson finds a bottle on a beach and releases a beautiful genie named Jeannie.</p>"},
    {"season": 1, "number": 2, "name": "My Master, the Thief",
     "summary": "<p>Jeannie interferes with an important physical that Tony is to take at the base.</p>"},
    {"season": 2, "number": 9, "name": "The Birds and the Bees",
     "summary": "<p>When Jeannie's nightmare comes true, Tony shrinks to six inches tall.</p>"},
    {"season": 3, "number": 4, "name": "Reno",
     "summary": "<p>Roger and Tony go to Reno after their promotions; Roger sneaks Jeannie along.</p>"},
]


def stamp(date, hm):
    y, m, d = (int(x) for x in date.split("-"))
    return time.mktime((y, m, d, int(hm[:2]), int(hm[2:]), 0, 0, 0, -1))


class MatchingTests(unittest.TestCase):
    def test_a_plot_line_picks_the_right_episode(self):
        episode, score, confident = ne.pick("When Jeannie's nightmare comes true, Tony shrinks to six inches tall.", EPISODES)
        self.assertEqual((episode["season"], episode["number"]), (2, 9))
        self.assertTrue(confident)

    def test_a_vague_plot_line_is_not_confident(self):
        _episode, _score, confident = ne.pick("Jeannie and Tony have a busy day.", EPISODES)
        self.assertFalse(confident)

    def test_air_time_is_read_from_the_file_name(self):
        self.assertEqual(ne.file_start_ts("I Dream of Jeannie - 2026-08-14_1130.mp4"), stamp("2026-08-14", "1130"))
        self.assertIsNone(ne.file_start_ts("I Dream of Jeannie.mp4"))

    def test_target_name_follows_the_recorder_pattern(self):
        self.assertEqual(ne.target_name("I Dream of Jeannie", EPISODES[2]),
                         "I Dream of Jeannie - S02E09 - The Birds and the Bees.mp4")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.airings = [
            {"start_ts": stamp("2026-08-14", "1100"), "channel": "ANTENNA TV",
             "desc": "When Jeannie's nightmare comes true, Tony shrinks to six inches tall."},
            {"start_ts": stamp("2026-08-14", "1130"), "channel": "ANTENNA TV",
             "desc": "Roger and Tony go to Reno after their promotions; Roger sneaks Jeannie"},
            {"start_ts": stamp("2026-08-15", "2100"), "channel": "ANTENNA TV",
             "desc": "Jeannie interferes with an important physical that Tony is to take."},
            {"start_ts": stamp("2026-08-15", "2130"), "channel": "ANTENNA TV",
             "desc": "Jeannie interferes with an important physical that Tony is to take."},
        ]

    def test_confident_files_are_ready_and_odd_ones_are_left_alone(self):
        files = [
            ("I Dream of Jeannie - 2026-08-14_1100.mp4", 600),
            ("I Dream of Jeannie - 2026-08-14_1130.mp4", 590),
            ("I Dream of Jeannie - 2026-08-15_2100.mp4", 595),
            ("I Dream of Jeannie - 2026-08-15_2130.mp4", 598),
            ("I Dream of Jeannie - 2026-09-24_1100.mp4", 300),   # partial and no guide line
            ("I Dream of Jeannie.mp4", 597),
        ]
        rows = {r["name"]: r for r in ne.plan(files, self.airings, EPISODES)}
        self.assertEqual(rows["I Dream of Jeannie - 2026-08-14_1100.mp4"]["status"], "ok")
        self.assertEqual(rows["I Dream of Jeannie - 2026-08-14_1130.mp4"]["status"], "ok")
        self.assertTrue(rows["I Dream of Jeannie - 2026-08-15_2100.mp4"]["status"].startswith("duplicate"))
        self.assertTrue(rows["I Dream of Jeannie - 2026-08-15_2130.mp4"]["status"].startswith("duplicate"))
        self.assertTrue(rows["I Dream of Jeannie - 2026-09-24_1100.mp4"]["status"].startswith("partial"))
        self.assertTrue(rows["I Dream of Jeannie.mp4"]["status"].startswith("no air time"))


class ApplyTests(unittest.TestCase):
    def test_apply_moves_only_confident_files_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            tv = os.path.join(temp, "TV Shows")
            holding = os.path.join(tv, "_Needs Episode Info", "I Dream of Jeannie (1965)")
            os.makedirs(holding)
            good = "I Dream of Jeannie - 2026-08-14_1100.mp4"
            vague = "I Dream of Jeannie - 2026-08-14_1130.mp4"
            for name in (good, vague):
                with open(os.path.join(holding, name), "wb") as handle:
                    handle.write(b"x" * 100)
            airings = [
                {"start_ts": stamp("2026-08-14", "1100"), "channel": "A",
                 "desc": "When Jeannie's nightmare comes true, Tony shrinks to six inches tall."},
                {"start_ts": stamp("2026-08-14", "1130"), "channel": "A", "desc": "Jeannie and Tony have a busy day."},
            ]
            cfg = {"plex_tv_path": tv}
            argv = ["name_episodes.py", "--show", "I Dream of Jeannie", "--apply"]
            with mock.patch("recording_agent.load_config", return_value=cfg), \
                    mock.patch.object(ne, "load_tvmaze", return_value=("1965", EPISODES)), \
                    mock.patch.object(ne, "load_guide", return_value=airings), \
                    mock.patch.object(sys, "argv", argv):
                self.assertEqual(ne.main(), 0)
            moved = os.path.join(tv, "I Dream of Jeannie (1965)", "Season 02",
                                 "I Dream of Jeannie - S02E09 - The Birds and the Bees.mp4")
            self.assertTrue(os.path.exists(moved))
            self.assertFalse(os.path.exists(os.path.join(holding, good)))
            self.assertTrue(os.path.exists(os.path.join(holding, vague)))   # unsure: left in place


class GuideDescriptionsEndpointTests(unittest.TestCase):
    def test_endpoint_returns_only_that_titles_plot_lines(self):
        import server
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            server.ensure_guide_db(guide_db)
            conn = sqlite3.connect(guide_db)
            conn.executemany(
                "INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc) VALUES (?,?,?,?,?,?)",
                [("I Dream of Jeannie", "1", "ANTENNA TV", "20260814150000", "20260814153000", "Plot A"),
                 ("Other Show", "1", "ANTENNA TV", "20260814153000", "20260814160000", "Plot B")])
            conn.commit(); conn.close()
            with mock.patch.object(server, "_guide_db_path", return_value=guide_db):
                data = server.app.test_client().get(
                    "/epg-web/api/guide-descriptions?title=i+dream+of+jeannie").get_json()
                missing = server.app.test_client().get("/epg-web/api/guide-descriptions")
        self.assertEqual([a["desc"] for a in data["airings"]], ["Plot A"])
        self.assertEqual(missing.status_code, 400)


if __name__ == "__main__":
    unittest.main()
