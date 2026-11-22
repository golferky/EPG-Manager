import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))

import split_247 as sp  # noqa: E402

LOG = """
[blackdetect @ 0x1] black_start:438.2 black_end:441.4 black_duration:3.2
[blackdetect @ 0x1] black_start:1214.4 black_end:1218.8 black_duration:4.4
[blackdetect @ 0x1] black_start:1237.1 black_end:1240.2 black_duration:3.1
[blackdetect @ 0x1] black_start:2725.9 black_end:2730.8 black_duration:4.9
[blackdetect @ 0x1] black_start:4284.3 black_end:4288.9 black_duration:4.6
[blackdetect @ 0x1] black_start:5813.0 black_end:5817.5 black_duration:4.5
"""


class SplitTests(unittest.TestCase):
    def test_black_moments_are_read_from_the_log(self):
        blacks = sp.parse_blacks(LOG)
        self.assertEqual(len(blacks), 6)
        self.assertEqual(blacks[1], (1214.4, 1218.8))

    def test_each_long_black_starts_an_episode_and_the_partial_ends_are_dropped(self):
        episodes, skipped = sp.episodes_from_blacks(sp.parse_blacks(LOG), 6808)
        self.assertEqual([(round(a), round(b)) for a, b, _ in episodes], [(1214, 2726), (2726, 4284), (4284, 5813)])
        self.assertEqual(skipped, [])
        self.assertTrue(all(24 * 60 < length < 27 * 60 for _a, _b, length in episodes))

    def test_a_missed_marker_or_a_gap_is_reported_not_cut(self):
        blacks = [(100.0, 105.0), (1600.0, 1605.0), (5600.0, 5605.0), (5700.0, 5705.0)]
        episodes, skipped = sp.episodes_from_blacks(blacks, 6000)
        self.assertEqual([(round(a), round(b)) for a, b, _ in episodes], [(100, 1600)])
        self.assertEqual(len(skipped), 2)
        self.assertIn("too long", skipped[0][2])
        self.assertIn("too short", skipped[1][2])

    def test_a_short_black_is_not_a_marker_and_no_marker_means_no_episodes(self):
        self.assertEqual(sp.episodes_from_blacks([(10.0, 12.0), (900.0, 903.0)], 2000), ([], []))

    def test_the_file_name_carries_the_air_time(self):
        began = time.mktime(time.strptime("2026-11-20 07:00", "%Y-%m-%d %H:%M"))
        self.assertEqual(sp.episode_filename("The Andy Griffith Show", began, 1214),
                         "The Andy Griffith Show - 2026-11-20_0720.mp4")
        self.assertEqual(sp.episode_filename('Bad:Name?', began, 0), "BadName - 2026-11-20_0700.mp4")

    def test_a_name_already_taken_gets_a_number_and_nothing_is_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "Show - 2026-11-20_0700.mp4")
            self.assertEqual(sp.unique_path(path), path)
            open(path, "w").close()
            self.assertEqual(sp.unique_path(path), os.path.join(temp, "Show - 2026-11-20_0700 (2).mp4"))
            open(sp.unique_path(path), "w").close()
            self.assertEqual(sp.unique_path(path), os.path.join(temp, "Show - 2026-11-20_0700 (3).mp4"))


if __name__ == "__main__":
    unittest.main()


class JoinedCaptureLengthTests(unittest.TestCase):
    def test_the_length_comes_from_the_last_progress_line(self):
        log = "frame= 100 time=00:00:04.00 bitrate=1\nframe= 999 time=02:24:10.52 bitrate=2\nvideo:1kB"
        self.assertAlmostEqual(sp.parse_total_seconds(log), 2 * 3600 + 24 * 60 + 10.52, places=2)
        self.assertEqual(sp.parse_total_seconds("no progress here"), 0.0)

    def test_the_start_is_the_creation_time_when_the_system_keeps_one(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            path = handle.name
        try:
            began = sp.recording_began(path, 600)
            stat = os.stat(path)
            expected = getattr(stat, "st_birthtime", 0) or stat.st_mtime - 600
            self.assertAlmostEqual(began, expected, places=2)
        finally:
            os.remove(path)


class CutCheckTests(unittest.TestCase):
    def test_a_missing_or_tiny_cut_is_not_good(self):
        self.assertFalse(sp.cut_is_good("/no/such/file.mp4", 1200))
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"x" * 2048)
            path = handle.name
        try:
            self.assertFalse(sp.cut_is_good(path, 1200))
        finally:
            os.remove(path)

    def test_a_cut_of_about_the_right_length_is_good(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"x" * 2000000)
            path = handle.name
        try:
            original = sp.probe_duration
            sp.probe_duration = lambda _p: 1195.0
            try:
                self.assertTrue(sp.cut_is_good(path, 1200))
                sp.probe_duration = lambda _p: 100.0
                self.assertFalse(sp.cut_is_good(path, 1200))
            finally:
                sp.probe_duration = original
        finally:
            os.remove(path)


class TrimTests(unittest.TestCase):
    def test_by_default_a_cut_starts_one_second_into_the_black_and_ends_at_the_next_marker(self):
        self.assertEqual(sp.trim_episodes([(100.0, 1400.0, 1300.0)]), [(101.0, 1400.0)])

    def test_the_lead_in_is_skipped_and_the_closing_credits_are_kept(self):
        cuts = sp.trim_episodes([(878.44, 2169.22, 1290.78)], skip_start=36.0, keep_after=15.5)
        self.assertAlmostEqual(cuts[0][0], 914.44, places=2)
        self.assertAlmostEqual(cuts[0][1], 2184.72, places=2)
