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

class GlitchAndCreditsTests(unittest.TestCase):
    # The black screens of the Nov 23 Raymond capture from 2074 s on (marker = a black of 4 s or more).
    REAL = [(2074.33, 2077.69), (2116.13, 2117.21), (2117.25, 2122.21), (2122.25, 2123.21), (2123.25, 2124.17),
            (2127.73, 2129.73), (2141.21, 2143.53), (2212.29, 2212.65), (2247.41, 2250.73), (2498.33, 2499.97),
            (2503.09, 2504.41), (2526.49, 2530.49), (2530.57, 2532.57), (2912.49, 2915.81), (3460.45, 3463.73),
            (3491.97, 3492.29), (3498.13, 3505.25), (3509.25, 3511.25), (3523.29, 3526.11)]

    def test_a_glitch_inside_an_episode_is_joined_not_cut_as_a_break(self):
        episodes, skipped = sp.episodes_from_blacks(self.REAL, 4500, 4.0, 18 * 60, 26 * 60)
        self.assertEqual([(round(a), round(b)) for a, b, _ in episodes], [(2117, 3498)])
        self.assertEqual(skipped, [])

    def test_the_episode_runs_from_the_end_of_its_break_to_the_end_of_its_credits(self):
        episodes, _ = sp.episodes_from_blacks(self.REAL, 4500, 4.0, 18 * 60, 26 * 60)
        (start, end), = sp.trim_episodes(episodes, 36.0, 28.0, self.REAL, 22.0)
        self.assertAlmostEqual(start, 2143.0, places=0)
        self.assertAlmostEqual(end, 3525.8, places=0)

    def test_the_end_never_runs_past_keep_after_into_a_logo_or_bumper(self):
        blacks = [(1000.0, 1012.0), (1016.0, 1017.0), (1035.0, 1038.0), (1100.0, 1101.0)]   # a long chain
        (start, end), = sp.trim_episodes([(100.0, 1000.0, 900.0)], 36.0, 28.0, blacks, 22.0)
        self.assertAlmostEqual(end, 1028.0, places=1)

    def test_a_short_piece_before_a_full_episode_is_not_glued_to_it(self):
        starts = [(0.0, 5.0), (180.0, 185.0), (1500.0, 1505.0)]            # a 3 minute left-over, then an episode
        episodes, skipped = sp.episodes_from_blacks(starts, 3000, 4.0, 18 * 60, 22 * 60)
        self.assertEqual([(round(a), round(b)) for a, b, _ in episodes], [(180, 1500)])
        self.assertEqual(len(skipped), 1)

    def test_a_smaller_start_gap_keeps_an_opening_that_sits_after_the_last_black(self):
        # The break before Raymond S01E22: blacks, 11 s of opening, then one more black.
        blacks = [(1406.06, 1414.18), (1418.18, 1420.18), (1431.14, 1433.87)]
        episode = [(1406.06, 2680.0, 1273.9)]
        self.assertAlmostEqual(sp.trim_episodes(episode, 36.0, 0.0, blacks, 22.0)[0][0], 1433.4, places=1)
        self.assertAlmostEqual(sp.trim_episodes(episode, 36.0, 0.0, blacks, 22.0, 10.0)[0][0], 1419.7, places=1)

    def test_the_break_before_s01e19_starts_at_its_opening_too(self):
        episodes, _ = sp.episodes_from_blacks(GlitchAndCreditsTests.REAL, 4500, 4.0, 18 * 60, 26 * 60)
        (start, _end), = sp.trim_episodes(episodes, 36.0, 28.0, GlitchAndCreditsTests.REAL, 22.0, 10.0)
        self.assertAlmostEqual(start, 2129.2, places=1)


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


class ChainTests(unittest.TestCase):
    # The two break layouts seen on the Raymond channel.
    LONG_BREAK = [(878.4, 890.6), (894.6, 895.6), (913.4, 916.1), (1161.0, 1164.0)]     # credits, logo, bumper, short black
    SHORT_BREAK = [(2498.4, 2500.0), (2503.2, 2504.5), (2526.6, 2530.6), (2530.6, 2532.6), (3100.0, 3103.0)]

    def test_the_chain_ends_where_the_close_together_blacks_stop(self):
        self.assertAlmostEqual(sp.chain_end(self.LONG_BREAK, 878.4, 22.0), 916.1, places=1)
        self.assertAlmostEqual(sp.chain_end(self.SHORT_BREAK, 2526.6, 22.0), 2532.6, places=1)

    def test_an_episode_starts_at_the_end_of_its_break_whatever_the_layout(self):
        long_cut = sp.trim_episodes([(878.4, 2169.2, 1290.8)], skip_start=36.0, keep_after=15.5,
                                    blacks=self.LONG_BREAK, chain_gap=22.0)
        short_cut = sp.trim_episodes([(2526.6, 3500.0, 973.4)], skip_start=36.0, keep_after=15.5,
                                     blacks=self.SHORT_BREAK, chain_gap=22.0)
        self.assertAlmostEqual(long_cut[0][0], 915.6, places=1)         # not 36 s after the marker by luck
        self.assertAlmostEqual(short_cut[0][0], 2532.1, places=1)       # about 5.5 s after the marker, not 36 s
        self.assertAlmostEqual(short_cut[0][1], 3515.5, places=1)

    def test_without_a_chain_gap_or_blacks_the_fixed_skip_is_used(self):
        self.assertEqual(sp.trim_episodes([(100.0, 1400.0, 1300.0)], skip_start=36.0)[0][0], 136.0)
        self.assertEqual(sp.trim_episodes([(100.0, 1400.0, 1300.0)], skip_start=36.0, chain_gap=22.0)[0][0], 136.0)