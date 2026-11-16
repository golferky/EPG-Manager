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


class AgreementTests(unittest.TestCase):
    def _row(self, status, season, number):
        return {"name": "f.mp4", "status": status, "episode": {"season": season, "number": number}}

    def test_two_sources_must_name_the_same_episode(self):
        self.assertEqual(ne.agree([self._row("ok", 2, 9)], [self._row("ok", 2, 9)])[0]["status"], "ok")
        self.assertEqual(ne.agree([self._row("ok", 2, 9)], [self._row("ok", 3, 1)])[0]["status"], "unsure")
        self.assertEqual(ne.agree([self._row("ok", 2, 9)], [self._row("unsure", 2, 9)])[0]["status"], "unsure")


class AcceptTests(unittest.TestCase):
    def test_accept_text_is_parsed_and_bad_text_is_refused(self):
        self.assertEqual(ne.parse_accept("2026-08-14_1130=S02E15, 2026-08-15_2100=s1e3"),
                         {"2026-08-14_1130": (2, 15), "2026-08-15_2100": (1, 3)})
        with self.assertRaises(ValueError):
            ne.parse_accept("2026-08-14=S02E15")

    def test_accepting_marks_the_file_ok_but_never_a_partial_or_a_clash(self):
        def row(name, status, ep=None):
            return {"name": name, "size": 1, "status": status, "episode": ep, "score": 0.0, "plot": ""}
        rows = [row("Show - 2026-08-14_1130.mp4", "unsure"),
                row("Show - 2026-08-15_2100.mp4", "ok", EPISODES[2]),      # already going to S02E09
                row("Show - 2026-08-17_1100.mp4", "unsure"),
                row("Show - 2026-09-24_1100.mp4", "partial episode (much smaller than the rest): not for Plex")]
        notes = ne.apply_accepted(rows, {"2026-08-14_1130": (1, 2), "2026-08-17_1100": (2, 9),
                                         "2026-09-24_1100": (1, 1), "2026-10-01_1100": (1, 1),
                                         "2026-08-15_2100": (9, 9)}, EPISODES)
        status = {r["name"]: r["status"] for r in rows}
        self.assertEqual(status["Show - 2026-08-14_1130.mp4"], "ok")
        self.assertTrue(status["Show - 2026-08-17_1100.mp4"].startswith("duplicate"))   # same episode as 08-15_2100
        self.assertTrue(status["Show - 2026-08-15_2100.mp4"].startswith("duplicate"))
        self.assertTrue(status["Show - 2026-09-24_1100.mp4"].startswith("partial"))
        self.assertEqual(len(notes), 3)    # the partial, the missing file, the episode that does not exist


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
                    mock.patch.object(ne, "load_tmdb", return_value=("1965", EPISODES)), \
                    mock.patch.object(ne, "load_guide", return_value=airings), \
                    mock.patch.object(sys, "argv", argv):
                self.assertEqual(ne.main(), 0)
            moved = os.path.join(tv, "I Dream of Jeannie (1965)", "Season 02",
                                 "I Dream of Jeannie - S02E09 - The Birds and the Bees.mp4")
            self.assertTrue(os.path.exists(moved))
            self.assertFalse(os.path.exists(os.path.join(holding, good)))
            self.assertTrue(os.path.exists(os.path.join(holding, vague)))   # unsure: left in place


class HoldingFolderLocationTests(unittest.TestCase):
    def test_the_matcher_reads_the_holding_folder_beside_tv_shows(self):
        with tempfile.TemporaryDirectory() as temp:
            tv = os.path.join(temp, "TV Shows")
            holding = os.path.join(temp, "TV Needing Episode Info", "I Dream of Jeannie")
            os.makedirs(tv)
            os.makedirs(holding)
            name = "I Dream of Jeannie - 2026-08-14_1100.mp4"
            with open(os.path.join(holding, name), "wb") as handle:
                handle.write(b"x" * 100)
            airings = [{"start_ts": stamp("2026-08-14", "1100"), "channel": "A",
                        "desc": "When Jeannie's nightmare comes true, Tony shrinks to six inches tall."}]
            argv = ["name_episodes.py", "--show", "I Dream of Jeannie", "--apply"]
            with mock.patch("recording_agent.load_config", return_value={"plex_tv_path": tv}), \
                    mock.patch.object(ne, "load_tmdb", return_value=("1965", EPISODES)), \
                    mock.patch.object(ne, "load_guide", return_value=airings), \
                    mock.patch.object(sys, "argv", argv):
                self.assertEqual(ne.main(), 0)
            self.assertFalse(os.path.exists(os.path.join(holding, name)))
            self.assertTrue(os.path.isdir(os.path.join(tv, "I Dream of Jeannie (1965)", "Season 02")))


class TmdbEpisodesEndpointTests(unittest.TestCase):
    def test_endpoint_returns_episodes_without_exposing_the_key(self):
        import json as _json
        import server

        class Reply:
            def __init__(self, data):
                self.data = data
            def read(self):
                return _json.dumps(self.data).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(url, timeout=0):
            if "/search/tv" in url:
                return Reply({"results": [{"id": 7, "name": "Test Show"}]})
            if "/season/1" in url:
                return Reply({"episodes": [{"episode_number": 1, "name": "Pilot", "overview": "A plot."}]})
            return Reply({"first_air_date": "1965-09-18", "seasons": [{"season_number": 0}, {"season_number": 1}]})

        server._tmdb_episode_cache.clear()
        with mock.patch.object(server, "load_config", return_value={"tmdb_key": "SECRETKEY"}), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            data = server.app.test_client().get("/epg-web/api/tmdb-episodes?title=Test+Show").get_json()
        self.assertEqual(data["premiered"], "1965")
        self.assertEqual(data["episodes"], [{"season": 1, "number": 1, "name": "Pilot", "summary": "A plot."}])
        self.assertNotIn("SECRETKEY", _json.dumps(data))

    def test_failure_does_not_leak_the_key(self):
        import json as _json
        import server

        def boom(url, timeout=0):
            raise OSError("failed for " + url)

        server._tmdb_episode_cache.clear()
        with mock.patch.object(server, "load_config", return_value={"tmdb_key": "SECRETKEY"}), \
                mock.patch("urllib.request.urlopen", boom):
            reply = server.app.test_client().get("/epg-web/api/tmdb-episodes?title=Other+Show")
        self.assertEqual(reply.status_code, 502)
        self.assertNotIn("SECRETKEY", _json.dumps(reply.get_json()))


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
