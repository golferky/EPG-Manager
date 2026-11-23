import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mac"))

import filler  # noqa: E402
import recording_agent  # noqa: E402


def make_cfg(root):
    return {"local_recordings": str(Path(root) / "rec"), "ffmpeg": "ffmpeg", "epg_url": "http://panel.invalid:826",
            "epg_user": "u", "epg_pass": "p", "plex_tv_path": str(Path(root) / "Plex" / "TV Shows"),
            "server_url": "http://dash.invalid", "plex_mount_marker": ""}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeProcess:
    """An FFmpeg that writes its segment and ends after ``run_for`` seconds of fake time (or when stopped)."""
    def __init__(self, clock, target, run_for, rc=0, size=2048):
        self.clock, self.end, self.rc = clock, clock.t + run_for, rc
        self.stopped = False
        Path(target).write_bytes(b"v" * size)

    def poll(self):
        return self.rc if (self.stopped or self.clock.t >= self.end) else None

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.stopped = True

    def wait(self, timeout=None):
        return self.rc


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.clock = Clock()
        self.info = {"id": 3, "channel": "RAYMOND", "show": "Everybody Loves Raymond", "stream_id": "555",
                     "start_ts": 1000.0, "until_ts": 1000.0 + 3600}

    def _popen(self, durations):
        durations = list(durations)

        def popen(command, **kwargs):
            target = command[-1]
            return FakeProcess(self.clock, target, durations.pop(0) if durations else 10 ** 9)
        return popen

    def test_the_command_has_no_reconnect_options_and_no_stdin(self):
        command = filler.build_capture_command(self.cfg, "555", 600, "/x/y.ts")
        self.assertIn("-nostdin", command)
        self.assertIn("-rw_timeout", command)
        self.assertFalse([c for c in command if "reconnect" in str(c)])
        self.assertEqual(command[-1], "/x/y.ts")

    def test_it_records_until_the_end_of_the_opening(self):
        api = mock.Mock()
        api.filler_status.return_value = {}
        path, seconds, drops = filler.capture(self.cfg, api, self.info, popen=self._popen([]), now=self.clock.now,
                                              sleep=self.clock.sleep, log=lambda *a: None)
        self.assertTrue(Path(path).exists())
        self.assertEqual(drops, 0)
        self.assertAlmostEqual(seconds, 3600, delta=10)

    def test_it_stops_at_once_when_the_dashboard_says_stop(self):
        api = mock.Mock()
        api.filler_status.side_effect = [{"stop": False}, {"stop": True}]
        path, seconds, drops = filler.capture(self.cfg, api, self.info, popen=self._popen([]), now=self.clock.now,
                                              sleep=self.clock.sleep, log=lambda *a: None)
        self.assertLess(seconds, 60)
        self.assertTrue(Path(path).exists())

    def test_a_shrinking_opening_ends_the_capture_early(self):
        api = mock.Mock()
        api.filler_status.return_value = {"until_ts": 1000.0 + 120}
        _path, seconds, _drops = filler.capture(self.cfg, api, self.info, popen=self._popen([]), now=self.clock.now,
                                                sleep=self.clock.sleep, log=lambda *a: None)
        self.assertLess(seconds, 200)

    def test_a_dropped_stream_starts_a_new_segment_and_the_segments_are_joined(self):
        api = mock.Mock()
        api.filler_status.return_value = {}
        path, _seconds, drops = filler.capture(self.cfg, api, self.info, popen=self._popen([300]), now=self.clock.now,
                                               sleep=self.clock.sleep, log=lambda *a: None)
        self.assertEqual(drops, 1)
        self.assertEqual(Path(path).stat().st_size, 4096)     # two segments of 2048 bytes
        self.assertFalse([p for p in Path(path).parent.glob("*seg*")])

    def test_nothing_captured_gives_no_path(self):
        api = mock.Mock()
        api.filler_status.return_value = {}

        def empty(command, **kwargs):
            return FakeProcess(self.clock, command[-1], 100, size=0)
        path, _s, _d = filler.capture(self.cfg, api, self.info, popen=empty, now=self.clock.now, sleep=self.clock.sleep,
                                      log=lambda *a: None)
        self.assertIsNone(path)


class AfterwardsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)

    def test_repair_keeps_the_original_if_the_copy_fails(self):
        original = Path(self.tmp) / "a.ts"
        original.write_bytes(b"x" * 10)
        failed = mock.Mock(returncode=1)
        self.assertEqual(filler.repair(self.cfg, original, run=lambda *a, **k: failed), original)

    def test_the_holding_folder_is_beside_the_tv_library_and_named_for_the_show(self):
        folder = filler.holding_folder(self.cfg, "Everybody Loves Raymond")
        self.assertEqual(folder.name, "Everybody Loves Raymond")
        self.assertEqual(folder.parent.name, "TV Needing Episode Info")

    def test_old_working_captures_are_removed_but_episodes_and_new_files_stay(self):
        folder = filler.captures_dir(self.cfg)
        old, new = folder / "old.ts", folder / "new.clean.ts"
        episode_dir = folder / "episodes" / "Show"
        episode_dir.mkdir(parents=True)
        episode = episode_dir / "Show - 2026-11-22_0808.mp4"
        for path in (old, new, episode):
            path.write_bytes(b"x")
        long_ago = time.time() - 30 * 86400
        os.utime(old, (long_ago, long_ago))
        os.utime(episode, (long_ago, long_ago))
        self.assertEqual(filler.prune_old_captures(self.cfg, 0), 0)
        self.assertTrue(old.exists())
        self.assertEqual(filler.prune_old_captures(self.cfg, 7), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists() and episode.exists())

    def test_confident_episodes_are_filed_in_plex_and_the_rest_are_held(self):
        import identify_episodes as ident
        import name_episodes as ne
        episodes = [
            {"season": 2, "number": 5, "name": "The Sculpture", "summary": "Marie unveils a giant marble sculpture of Robert in the living room and everyone pretends to admire it."},
            {"season": 3, "number": 9, "name": "Golf Weekend", "summary": "Ray sneaks away for a golf tournament while Frank hides the car keys in the garage."},
            {"season": 1, "number": 1, "name": "Pilot", "summary": "Ray forgets the anniversary dinner and Debra sulks while his parents argue about lasagna."},
        ]
        speech = {"a": ("so marie brought over the sculpture and it is huge marble robert looks like a giant in the living room "
                        "everyone says it is beautiful and admire it but nobody wants it in the house ") * 6,
                  "b": "okay well see you later bye " * 3}
        folder = Path(self.tmp) / "eps"
        folder.mkdir()
        files = []
        for key in ("a", "b"):
            path = folder / f"Everybody Loves Raymond - 2026-11-22_080{'8' if key == 'a' else '9'}.mp4"
            path.write_bytes(b"x" * 100)
            files.append(path)
        order = iter(["a", "b"])
        Path(self.cfg["plex_tv_path"]).mkdir(parents=True)          # Plex is mounted
        info = {"show": "Everybody Loves Raymond"}
        with mock.patch.object(ne, "load_tmdb", return_value=("1996", episodes)), \
             mock.patch.object(ne, "load_omdb", return_value=("1996", [])), \
             mock.patch.object(ident, "transcribe", side_effect=lambda *a, **k: (speech[next(order)], "model")):
            filed, held, notes = filler.identify_and_file(self.cfg, files, info, log=lambda *a: None)
        self.assertEqual((filed, held), (1, 1))
        plex = Path(self.cfg["plex_tv_path"])
        found = list(plex.rglob("*.mp4"))
        self.assertEqual(len(found), 1)
        self.assertIn("S02E05", found[0].name)
        self.assertEqual(len(list(filler.holding_folder(self.cfg, "Everybody Loves Raymond").glob("*.mp4"))), 1)

    def test_without_the_speech_package_everything_is_held_and_nothing_is_lost(self):
        import identify_episodes as ident
        import name_episodes as ne
        folder = Path(self.tmp) / "eps"
        folder.mkdir()
        path = folder / "Show - 2026-11-22_0808.mp4"
        path.write_bytes(b"x" * 100)
        with mock.patch.object(ne, "load_tmdb", return_value=("1996", [])), \
             mock.patch.object(ne, "load_omdb", return_value=("1996", [])), \
             mock.patch.object(ident, "transcribe", side_effect=SystemExit("install faster-whisper")):
            filed, held, notes = filler.identify_and_file(self.cfg, [path], {"show": "Show"}, log=lambda *a: None)
        self.assertEqual((filed, held), (0, 1))
        self.assertIn("faster-whisper", notes[0])
        self.assertTrue((filler.holding_folder(self.cfg, "Show") / path.name).exists())


class RunCaptureTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(tempfile.mkdtemp())
        self.info = {"id": 4, "show": "S", "channel": "C", "stream_id": "1", "start_ts": 1.0, "until_ts": 2.0}

    def test_a_whole_run_is_reported(self):
        api = mock.Mock()
        path = Path(self.cfg["local_recordings"]) / "x.ts"
        with mock.patch.object(filler, "capture", return_value=(path, 3000, 1)), \
             mock.patch.object(filler, "repair", return_value=path), \
             mock.patch.object(filler, "cut_episodes", return_value=["e1", "e2"]), \
             mock.patch.object(filler, "identify_and_file", return_value=(1, 1, ["note"])):
            report = filler.run_capture(self.cfg, api, self.info, log=lambda *a: None)
        self.assertEqual((report["status"], report["episodes"], report["filed"], report["held"], report["drops"]),
                         ("done", 2, 1, 1, 1))
        api.filler_finish.assert_called_once()

    def test_a_short_capture_is_not_cut(self):
        api = mock.Mock()
        with mock.patch.object(filler, "capture", return_value=(Path("/x.ts"), 600, 0)), \
             mock.patch.object(filler, "cut_episodes") as cut:
            report = filler.run_capture(self.cfg, api, self.info, log=lambda *a: None)
        self.assertEqual(report["status"], "too_short")
        cut.assert_not_called()

    def test_an_error_is_reported_not_raised(self):
        api = mock.Mock()
        with mock.patch.object(filler, "capture", side_effect=RuntimeError("boom")):
            report = filler.run_capture(self.cfg, api, self.info, log=lambda *a: None)
        self.assertEqual(report["status"], "failed")
        self.assertIn("boom", report["note"])
        api.filler_finish.assert_called_once()


class AgentLoopTests(unittest.TestCase):
    def test_no_opening_starts_nothing_and_an_opening_starts_a_worker(self):
        api = mock.Mock()
        api.filler_next.return_value = None
        self.assertIsNone(recording_agent.maybe_start_filler(api, {}))
        api.filler_next.return_value = {"id": 1, "channel": "C", "until_ts": time.time() + 60}
        started = []

        class Thread:
            def __init__(self, target, args, daemon, name):
                self.target, self.args = target, args

            def start(self):
                started.append(self.args)
        worker = recording_agent.maybe_start_filler(api, {"x": 1}, thread_factory=Thread)
        self.assertIsNotNone(worker)
        self.assertEqual(started[0][2]["id"], 1)

    def test_a_dashboard_without_the_feature_is_not_an_error(self):
        api = mock.Mock()
        api.filler_next.side_effect = RuntimeError("HTTP 404")
        self.assertIsNone(recording_agent.maybe_start_filler(api, {}))


if __name__ == "__main__":
    unittest.main()
