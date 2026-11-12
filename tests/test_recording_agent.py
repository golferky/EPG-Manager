import json
import sys
import time
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import recording_agent


class QualityDecisionTests(unittest.TestCase):
    def test_recording_tail_defaults_to_45_seconds(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "agent.json"
            config.write_text('{"server_url":"http://example.test","agent_token":"x",'
                              '"epg_url":"http://example.test","epg_user":"u",'
                              '"epg_pass":"p"}', encoding="utf-8")
            self.assertEqual(recording_agent.load_config(config)["recording_tail_seconds"], 45)

    def test_higher_resolution_records(self):
        record, reason = recording_agent.quality_decision(
            {"width": 1280, "height": 720, "fps": 60, "total_bitrate": 6_000_000},
            {"width": 1920, "height": 1080, "fps": 30, "total_bitrate": 5_000_000},
        )
        self.assertTrue(record)
        self.assertIn("720p", reason)
        self.assertIn("1080p", reason)

    def test_lower_resolution_skips(self):
        record, reason = recording_agent.quality_decision(
            {"width": 1920, "height": 1080, "fps": 60, "total_bitrate": 5_000_000},
            {"width": 1280, "height": 720, "fps": 30, "total_bitrate": 6_000_000},
        )
        self.assertFalse(record)
        self.assertIn("Plex is 1080p", reason)

    def test_equal_resolution_requires_material_bitrate_gain(self):
        base = {"width": 1920, "height": 1080, "fps": 30, "total_bitrate": 5_000_000}
        record, _reason = recording_agent.quality_decision(
            base, {**base, "total_bitrate": 5_500_000}
        )
        self.assertFalse(record)
        record, _reason = recording_agent.quality_decision(
            base, {**base, "total_bitrate": 6_500_000}
        )
        self.assertTrue(record)

    def test_incomplete_movie_is_eligible_for_replacement(self):
        # A provider cutoff left only 61 minutes of a scheduled 160-minute movie.
        self.assertTrue(recording_agent.incomplete_for_scheduled_window(
            {"duration": 61 * 60}, 0, 160 * 60
        ))
        self.assertTrue(recording_agent.incomplete_for_scheduled_window(
            {"duration": 126 * 60 + 44}, 0, 160 * 60
        ))
        self.assertFalse(recording_agent.incomplete_for_scheduled_window(
            {"duration": 155 * 60}, 0, 160 * 60
        ))

    def test_a_fresh_recording_missing_a_few_minutes_is_flagged(self):
        missing = recording_agent.recording_missing_seconds
        # Star Trek: 128 of about 137 minutes - passed the old 80% rule.
        self.assertGreater(missing({"duration": 128 * 60 + 38}, 0, 137 * 60), 8 * 60)
        # The Last Face: 103 of 132 minutes.
        self.assertGreater(missing({"duration": 103 * 60 + 19}, 0, 132 * 60), 28 * 60)

    def test_a_complete_recording_is_not_flagged(self):
        missing = recording_agent.recording_missing_seconds
        self.assertEqual(missing({"duration": 137 * 60 + 40}, 0, 137 * 60), 0)   # runs a little long
        self.assertEqual(missing({"duration": 136 * 60}, 0, 137 * 60), 0)        # a minute short
        self.assertEqual(missing({"duration": 133 * 60}, 0, 137 * 60), 0)        # 4 min < allowance

    def test_short_programmes_and_unknown_lengths_are_not_judged(self):
        missing = recording_agent.recording_missing_seconds
        self.assertEqual(missing({"duration": 5 * 60}, 0, 10 * 60), 0)    # under 15 min slots
        self.assertEqual(missing({"duration": 0}, 0, 120 * 60), 0)
        self.assertEqual(missing({}, 0, 120 * 60), 0)

    def test_library_copies_still_use_the_looser_rule(self):
        # A 111-minute rip against a 137-minute slot must not look incomplete.
        self.assertFalse(recording_agent.incomplete_for_scheduled_window(
            {"duration": 111 * 60}, 0, 137 * 60))

    def test_atomic_transfer_verifies_and_renames(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"recording-data")
            plex = root / "plex"
            plex.mkdir()
            destination = recording_agent.verified_transfer(
                source, plex, "Test Movie (2026)"
            )
            self.assertEqual(destination.read_bytes(), b"recording-data")
            self.assertFalse(destination.with_name(destination.name + ".partial").exists())

    def test_incomplete_recordings_use_a_separate_qnap_folder(self):
        self.assertEqual(
            recording_agent.incomplete_recordings_root({"plex_path": "/Volumes/Plex/Movies"}),
            Path("/Volumes/Plex/Incomplete Recordings"),
        )
        self.assertEqual(
            recording_agent.incomplete_recordings_root({
                "plex_path": "/Volumes/Plex/Movies",
                "incomplete_recordings_path": "/Volumes/QNAP/Partial TV",
            }),
            Path("/Volumes/QNAP/Partial TV"),
        )

    def test_transfer_uses_resolved_year_when_title_has_none(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"recording-data")
            plex = root / "plex"
            plex.mkdir()
            destination = recording_agent.verified_transfer(
                source, plex, "F/X", year="1986"
            )
            self.assertEqual(destination.parent.name, "FX (1986)")
            self.assertEqual(destination.name, "FX.mp4")

    def test_episode_transfer_uses_plex_tv_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"episode-data")
            destination = recording_agent.verified_episode_transfer(
                source, root / "TV Shows", "Dutton Ranch", 1, 2,
                "Earn Another Day",
            )
            self.assertEqual(
                destination.relative_to(root).as_posix(),
                "TV Shows/Dutton Ranch/Season 01/"
                "Dutton Ranch - S01E02 - Earn Another Day.mp4",
            )
            self.assertEqual(destination.read_bytes(), b"episode-data")

    def test_episode_metadata_requires_season_and_episode(self):
        self.assertEqual(
            recording_agent.episode_metadata({
                "season_num": 3, "episode_num": 9, "episode_title": "Test",
            }),
            {"season": 3, "episode": 9, "title": "Test"},
        )
        self.assertIsNone(recording_agent.episode_metadata({"season_num": 3}))

    def test_unidentified_series_transfer_uses_tv_holding_area(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"episode-data")
            destination = recording_agent.verified_unidentified_episode_transfer(
                source, root / "TV Shows", "I Dream of Jeannie", 1_786_719_600,
            )
            self.assertIn("TV Shows/_Needs Episode Info/I Dream of Jeannie/",
                          destination.as_posix())
            self.assertEqual(destination.read_bytes(), b"episode-data")

    def test_title_normalization_ignores_punctuation_and_year(self):
        self.assertEqual(
            recording_agent.normalized_title("Crazy, Stupid, Love. (2011)"),
            recording_agent.normalized_title("Crazy Stupid Love"),
        )

    @mock.patch("recording_agent.os.path.ismount", return_value=False)
    def test_volumes_directory_must_be_real_mount(self, _ismount):
        with mock.patch.object(Path, "is_dir", return_value=True):
            self.assertFalse(
                recording_agent.plex_mount_available("/Volumes/Plex/Movies")
            )

    @mock.patch("recording_agent.time.sleep", return_value=None)
    @mock.patch("recording_agent.subprocess.Popen")
    def test_ffmpeg_continues_through_heartbeat_outage(self, popen, _sleep):
        process = mock.Mock()
        process.poll.side_effect = [None, 0]
        process.returncode = 0
        popen.return_value = process
        api = mock.Mock()
        api.heartbeat.side_effect = RuntimeError("server restarting")
        with tempfile.TemporaryDirectory() as temp:
            result = recording_agent.run_process(
                api, {"id": "job1"}, "recording", ["ffmpeg"],
                {"heartbeat_seconds": 0}, Path(temp) / "ffmpeg.log",
            )
        self.assertEqual(result, 0)
        process.terminate.assert_not_called()

    @mock.patch("recording_agent.process_job")
    @mock.patch("recording_agent.AgentAPI")
    def test_agent_claims_multiple_jobs_up_to_worker_limit(self, agent_class, process_job):
        api = agent_class.return_value
        api.health.return_value = {"recording_backend": "agent"}
        api.claim.side_effect = [
            {"id": "one", "title": "First"},
            {"id": "two", "title": "Second"},
            None,
        ]
        cfg = {
            "claim_ahead_seconds": 300,
            "max_concurrent_recordings": 2,
            "poll_seconds": 0,
        }
        recording_agent.run_agent(cfg, once=True)
        self.assertEqual(process_job.call_count, 2)
        self.assertEqual(api.claim.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class ResumeRecordingTests(unittest.TestCase):
    """record_with_resume restarts FFmpeg when the provider cuts the stream early."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ts = Path(self.tmp.name) / "show.ts"
        self.log = Path(self.tmp.name) / "show.log"
        self.now = [1000.0]
        self.cfg = {"ffmpeg": "ffmpeg"}
        self.job = {"id": "j1", "title": "Show"}

    def _run(self, plan, end_time=1000.0 + 600):
        """plan: list of (bytes_written, seconds_elapsed, rc) per FFmpeg run."""
        calls = []

        def fake_run_process(api, job, status, cmd, cfg, log_path, file_path="", watchdog=None):
            data, elapsed, rc = plan[len(calls)]
            seg = Path(cmd[-1])
            seg.write_bytes(data)
            self.now[0] += elapsed
            calls.append(seg.name)
            return rc

        def fake_sleep(api, job, status, seconds, cfg, **fields):
            self.now[0] += seconds
            return True

        with mock.patch.object(recording_agent, "run_process", fake_run_process), \
             mock.patch.object(recording_agent, "heartbeat_sleep", fake_sleep), \
             mock.patch.object(recording_agent.time, "time", lambda: self.now[0]):
            rc = recording_agent.record_with_resume(
                None, self.job, self.cfg, "http://x/y.ts", self.ts, self.log, end_time)
        return rc, calls

    def test_resumes_after_an_early_stop_and_joins_the_segments(self):
        rc, calls = self._run([(b"AAAA", 100, 0), (b"BBBB", 500, 0)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.ts.read_bytes(), b"AAAABBBB")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(p.name for p in self.ts.parent.glob("show*.ts")), ["show.ts"])

    def test_wrong_programme_result_is_returned_and_pieces_are_removed(self):
        def fake_run_process(api, job, status, cmd, cfg, log_path, file_path="", watchdog=None):
            Path(cmd[-1]).write_bytes(b"XXXX")
            self.now[0] += 100
            if Path(cmd[-1]).name != "show.ts":
                return recording_agent.WrongProgramme("provider shows another movie")
            return 0

        def fake_sleep(api, job, status, seconds, cfg, **fields):
            self.now[0] += seconds
            return True

        with mock.patch.object(recording_agent, "run_process", fake_run_process), \
             mock.patch.object(recording_agent, "heartbeat_sleep", fake_sleep), \
             mock.patch.object(recording_agent.time, "time", lambda: self.now[0]):
            rc = recording_agent.record_with_resume(
                None, self.job, self.cfg, "http://x/y.ts", self.ts, self.log, 1000.0 + 600)
        self.assertIsInstance(rc, recording_agent.WrongProgramme)
        self.assertEqual(sorted(p.name for p in self.ts.parent.glob("show*.ts")), ["show.ts"])

    def test_a_recording_that_reaches_its_end_is_not_restarted(self):
        rc, calls = self._run([(b"AAAA", 600, 0)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ts.read_bytes(), b"AAAA")

    def test_gives_up_after_repeated_empty_segments(self):
        plan = [(b"", 1, 1)] * 20
        rc, calls = self._run(plan)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(calls), recording_agent.RESUME_MAX_EMPTY_SEGMENTS)

    def test_data_already_captured_is_kept_when_later_attempts_fail(self):
        plan = [(b"AAAA", 50, 0)] + [(b"", 1, 1)] * 10
        rc, calls = self._run(plan)
        self.assertEqual(rc, 0)
        self.assertEqual(self.ts.read_bytes(), b"AAAA")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class RealFfmpegSegmentTests(unittest.TestCase):
    """Run the real command for a later segment, the case a fake FFmpeg hid."""

    def test_later_segment_name_is_accepted_by_ffmpeg(self):
        import functools
        import http.server
        import subprocess
        import threading

        with tempfile.TemporaryDirectory() as temp:
            sample = Path(temp) / "sample.ts"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=2:s=160x120:r=10",
                 "-c:v", "libx264", "-t", "2", "-f", "mpegts", str(sample)], check=True)
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=temp)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            segment = recording_agent.segment_path(Path(temp) / "show.ts", 4)
            self.assertEqual(segment.name, "show.part4.ts")
            cmd = recording_agent.build_record_command(
                {"ffmpeg": "ffmpeg"}, f"http://127.0.0.1:{server.server_port}/sample.ts", 2, segment)
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            self.assertTrue(segment.exists() and segment.stat().st_size > 0, done.stderr[-400:])


class WrongProgrammeTests(unittest.TestCase):
    def test_plainly_different_titles_are_flagged(self):
        self.assertTrue(recording_agent.titles_clearly_differ("Quantum of Solace", "The Terminator"))
        self.assertTrue(recording_agent.titles_clearly_differ("MobLand", "V for Vendetta"))

    def test_different_wordings_of_one_title_are_not_flagged(self):
        same = [("Quantum of Solace", "007: Quantum of Solace"),
                ("The Untouchables (1987)", "Untouchables, The"),
                ("Quantum of Solace", "QUANTUM OF SOLACE (2008)"),
                ("Indiana Jones and the Temple of Doom", "Indiana Jones: Temple of Doom")]
        for a, b in same:
            self.assertFalse(recording_agent.titles_clearly_differ(a, b), (a, b))

    def test_blank_titles_are_never_flagged(self):
        self.assertFalse(recording_agent.titles_clearly_differ("Quantum of Solace", ""))

    def _dog(self, titles, now, enabled=True):
        job = {"title": "Quantum of Solace", "start_ts": 1000.0, "stop_ts": 1000.0 + 7200,
               "stream_id": "9", "stream_provider": "eaglecast"}
        seq = iter(titles)
        clock = [now]
        dog = recording_agent.ProgrammeWatchdog(
            {"verify_stream_programme": enabled}, job, now=lambda: clock[0],
            fetch=lambda cfg, provider, stream_id, now: ({"title": next(seq)} if True else None))
        return dog, clock

    def test_stops_after_two_matching_wrong_readings(self):
        dog, clock = self._dog(["The Terminator", "The Terminator"], now=1000.0 + 100)
        self.assertIsNone(dog())                      # first disagreement: wait and look again
        clock[0] += 31
        problem = dog()
        self.assertIsInstance(problem, recording_agent.WrongProgramme)
        self.assertIn("The Terminator", problem)

    def test_a_single_odd_reading_does_not_stop_the_recording(self):
        dog, clock = self._dog(["The Terminator", "Quantum of Solace"], now=1000.0 + 100)
        self.assertIsNone(dog())
        clock[0] += 31
        self.assertIsNone(dog())
        clock[0] += 31
        self.assertIsNone(dog())                      # confirmed once: nothing more to check

    def test_too_early_or_too_late_or_disabled_does_nothing(self):
        for now, enabled in ((1000.0 + 10, True), (1000.0 + 7100, True), (1000.0 + 100, False)):
            dog, _ = self._dog(["The Terminator"] * 3, now=now, enabled=enabled)
            self.assertIsNone(dog())

    def test_unknown_provider_guide_never_stops_a_recording(self):
        job = {"title": "Quantum of Solace", "start_ts": 1000.0, "stop_ts": 8200.0, "stream_id": "9"}
        clock = [1100.0]
        dog = recording_agent.ProgrammeWatchdog({}, job, now=lambda: clock[0],
                                                fetch=lambda *a, **k: None)
        for _ in range(8):
            self.assertIsNone(dog())
            clock[0] += 31

    def test_now_playing_decodes_the_provider_listing(self):
        import base64, io, json as _json
        listing = {"epg_listings": [
            {"title": base64.b64encode(b"Old Show").decode(), "start_timestamp": "100", "stop_timestamp": "500"},
            {"title": base64.b64encode(b"The Terminator").decode(), "start_timestamp": "500", "stop_timestamp": "9000"}]}
        cfg = {"epg_url": "http://p.test", "epg_user": "u", "epg_pass": "p"}
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = _json.dumps(listing).encode()
        with mock.patch.object(recording_agent.urllib.request, "urlopen", return_value=resp):
            playing = recording_agent.fetch_now_playing(cfg, "primestreams", "9", now=700)
        self.assertEqual(playing["title"], "The Terminator")

    def test_run_process_stops_ffmpeg_when_the_watchdog_complains(self):
        class Api:
            def heartbeat(self, *a, **k):
                return {}
        with tempfile.TemporaryDirectory() as temp:
            result = recording_agent.run_process(
                Api(), {"id": "j"}, "recording", ["sleep", "30"], {"heartbeat_seconds": 0.05},
                str(Path(temp) / "log"), watchdog=lambda: recording_agent.WrongProgramme("wrong"))
        self.assertIsInstance(result, recording_agent.WrongProgramme)


class RepeatClaimTests(unittest.TestCase):
    class FakeThread:
        started = []

        def __init__(self, target=None, args=(), daemon=None, name=None, alive=True):
            self.name, self.alive = name, alive

        def start(self):
            RepeatClaimTests.FakeThread.started.append(self.name)

        def is_alive(self):
            return self.alive

    def setUp(self):
        RepeatClaimTests.FakeThread.started = []
        self.job = {"id": "2ba0fd44", "title": "Star Trek: First Contact"}

    def test_a_new_job_is_started(self):
        active = {}
        self.assertTrue(recording_agent.start_job_once(None, self.job, {}, active, self.FakeThread))
        self.assertEqual(self.FakeThread.started, ["epg-record-2ba0fd44"])
        self.assertIn("2ba0fd44", active)

    def test_a_repeat_claim_of_a_running_job_is_ignored(self):
        active = {}
        recording_agent.start_job_once(None, self.job, {}, active, self.FakeThread)
        original = active["2ba0fd44"]
        self.assertFalse(recording_agent.start_job_once(None, self.job, {}, active, self.FakeThread))
        self.assertEqual(self.FakeThread.started, ["epg-record-2ba0fd44"])   # no second worker
        self.assertIs(active["2ba0fd44"], original)

    def test_a_job_whose_worker_has_finished_can_be_started_again(self):
        active = {"2ba0fd44": self.FakeThread(name="old", alive=False)}
        self.assertTrue(recording_agent.start_job_once(None, self.job, {}, active, self.FakeThread))
        self.assertEqual(self.FakeThread.started, ["epg-record-2ba0fd44"])


class AgentVersionTests(unittest.TestCase):
    def test_version_is_v_date_letter(self):
        import datetime as dt
        import re as _re
        self.assertRegex(recording_agent.AGENT_VERSION, r"^v\d{8}[a-z]$")
        dt.datetime.strptime(recording_agent.AGENT_VERSION[1:9], "%Y%m%d")

    def test_the_connect_line_shows_the_version(self):
        import contextlib
        import io

        class FakeApi:
            def __init__(self, cfg):
                pass

            def health(self):
                return {"recording_backend": "agent"}

            def claim(self, ahead):
                return None

        out = io.StringIO()
        with mock.patch.object(recording_agent, "AgentAPI", FakeApi), contextlib.redirect_stdout(out):
            recording_agent.run_agent({"max_concurrent_recordings": 2, "claim_ahead_seconds": 300,
                                       "poll_seconds": 0}, once=True)
        self.assertIn(f"version {recording_agent.AGENT_VERSION}", out.getvalue())
        self.assertIn("[agent] connected;", out.getvalue())


class PatientHeartbeatTests(unittest.TestCase):
    def _api(self, outcomes):
        """An AgentAPI whose heartbeat returns or raises each outcome in turn."""
        cfg = {"server_url": "http://x.test", "agent_token": "t", "agent_id": "a",
               "epg_url": "", "epg_user": "", "epg_pass": ""}
        api = recording_agent.AgentAPI(dict(cfg, lease_seconds=90))
        results = iter(outcomes)
        calls = []

        def heartbeat(job_id, status, **fields):
            calls.append(status)
            value = next(results)
            if isinstance(value, Exception):
                raise value
            return value
        api.heartbeat = heartbeat
        return api, calls

    def test_rides_out_a_server_outage_then_succeeds(self):
        import urllib.error
        api, calls = self._api([urllib.error.URLError("Connection refused"),
                                urllib.error.URLError("Connection refused"), {"ok": True}])
        with mock.patch.object(recording_agent.time, "sleep"):
            self.assertEqual(api.patient_heartbeat("j1", "done", pause=0), {"ok": True})
        self.assertEqual(len(calls), 3)

    def test_server_errors_are_retried_but_client_errors_are_not(self):
        api, calls = self._api([RuntimeError("EPG API HTTP 500: busy"), {"ok": True}])
        with mock.patch.object(recording_agent.time, "sleep"):
            api.patient_heartbeat("j1", "done", pause=0)
        self.assertEqual(len(calls), 2)
        api, calls = self._api([RuntimeError("EPG API HTTP 404: Recording job not found"), {"ok": True}])
        with mock.patch.object(recording_agent.time, "sleep"):
            with self.assertRaises(RuntimeError):
                api.patient_heartbeat("j1", "done", pause=0)
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_the_wait_limit(self):
        import urllib.error
        api, calls = self._api([urllib.error.URLError("down")] * 50)
        clock = [1000.0]
        with mock.patch.object(recording_agent.time, "time", lambda: clock[0]), \
             mock.patch.object(recording_agent.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            with self.assertRaises(urllib.error.URLError):
                api.patient_heartbeat("j1", "done", wait_seconds=100, pause=20)
        self.assertLessEqual(len(calls), 7)


class AgentVersionPayloadTests(unittest.TestCase):
    def test_claim_and_heartbeat_send_the_recorders_version(self):
        api = recording_agent.AgentAPI({"server_url": "http://x.test", "agent_token": "t", "agent_id": "a",
                                        "epg_url": "", "epg_user": "", "epg_pass": "", "lease_seconds": 90})
        sent = []
        api._request = lambda method, path, payload=None, timeout=30: sent.append(payload) or {}
        api.claim(300)
        api.heartbeat("j1", "recording")
        self.assertEqual([p["agent_version"] for p in sent],
                         [recording_agent.AGENT_VERSION] * 2)


class ShortRecordingFlowTests(unittest.TestCase):
    """A recording a few minutes short goes to Incomplete Recordings, not Plex."""

    def _run(self, recorded_minutes):
        now = time.time()
        job = {"id": "j1", "title": "Short Movie", "stream_id": "1", "start_ts": now - 3600,
               "stop_ts": now - 3600 + 137 * 60, "stream_provider": "primestreams",
               "stream_extension": "ts"}
        with tempfile.TemporaryDirectory() as temp:
            cfg = {"local_recordings": temp, "plex_path": temp + "/Movies", "plex_tv_path": temp + "/TV",
                   "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "recording_tail_seconds": 45,
                   "epg_url": "http://x.test", "epg_user": "u", "epg_pass": "p", "heartbeat_seconds": 1,
                   "transfer_wait_timeout": 5, "transfer_retry_seconds": 1}
            api = mock.Mock()
            probe = {"duration": recorded_minutes * 60, "height": 480}
            with mock.patch.object(recording_agent, "find_plex_candidates", return_value=[]), \
                 mock.patch.object(recording_agent, "probe_media", return_value=probe), \
                 mock.patch.object(recording_agent, "heartbeat_sleep", return_value=True), \
                 mock.patch.object(recording_agent, "record_with_resume", return_value=0), \
                 mock.patch.object(recording_agent, "run_process", return_value=0), \
                 mock.patch.object(recording_agent, "plex_mount_available", return_value=True), \
                 mock.patch.object(recording_agent, "archive_terminal_result") as archive, \
                 mock.patch.object(recording_agent, "discard_failed_media"), \
                 mock.patch.object(recording_agent, "preserve_incomplete_recording",
                                   return_value=Path(temp) / "x - INCOMPLETE.mp4") as preserve, \
                 mock.patch.object(recording_agent, "verified_transfer",
                                   return_value=Path(temp) / "x.mp4"):
                recording_agent.process_job(api, job, cfg)
            return archive, preserve

    def test_a_recording_nine_minutes_short_is_saved_as_incomplete(self):
        archive, preserve = self._run(128)
        preserve.assert_called_once()
        kwargs = archive.call_args.kwargs
        self.assertEqual(archive.call_args.args[2], "failed")
        self.assertIn("min missing", kwargs["message"])

    def test_a_complete_recording_is_not_saved_as_incomplete(self):
        _archive, preserve = self._run(138)
        preserve.assert_not_called()


class SameTitleDifferentYearTests(unittest.TestCase):
    """Two films with one title (a remake) must not be treated as the same copy."""

    def _library(self, temp, *folders):
        root = Path(temp)
        for name in folders:
            (root / name).mkdir()
            (root / name / "movie.mp4").write_bytes(b"x")
        return root

    def test_a_copy_from_another_year_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp, "Employee of the Month (2022)")
            self.assertEqual(recording_agent.find_plex_candidates(root, "Employee of the Month", "2004"), [])

    def test_the_same_year_is_a_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp, "Employee of the Month (2022)")
            found = recording_agent.find_plex_candidates(root, "Employee of the Month", "2022")
            self.assertEqual([p.name for p in found], ["movie.mp4"])

    def test_only_the_matching_year_is_picked_when_both_exist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp, "Employee of the Month (2004)", "Employee of the Month (2022)")
            found = recording_agent.find_plex_candidates(root, "Employee of the Month", "2004")
            self.assertEqual([p.parent.name for p in found], ["Employee of the Month (2004)"])

    def test_a_folder_with_no_year_is_not_assumed_to_be_the_same_film(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp, "Employee of the Month")
            self.assertEqual(recording_agent.find_plex_candidates(root, "Employee of the Month", "2004"), [])

    def test_no_year_keeps_the_old_behaviour(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp, "Employee of the Month (2022)")
            self.assertEqual(len(recording_agent.find_plex_candidates(root, "Employee of the Month")), 1)

    def test_a_year_in_the_title_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp, "Employee of the Month (2022)")
            self.assertEqual(recording_agent.find_plex_candidates(root, "Employee of the Month (2004)"), [])

    def test_the_new_copy_goes_to_its_own_year_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp, "Employee of the Month (2022)")
            source = Path(temp) / "rec.mp4"
            source.write_bytes(b"new-copy")
            dest = recording_agent.verified_transfer(source, root, "Employee of the Month", year="2004")
            self.assertEqual(dest.parent.name, "Employee of the Month (2004)")
            self.assertEqual((root / "Employee of the Month (2022)" / "movie.mp4").read_bytes(), b"x")


class NeedsYearTests(unittest.TestCase):
    def test_a_film_without_a_year_goes_to_the_holding_folder_not_the_library(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = {"plex_path": temp + "/Movies"}
            (Path(temp) / "Movies").mkdir()
            source = Path(temp) / "rec.mp4"
            source.write_bytes(b"film")
            start = time.mktime((2026, 11, 11, 20, 0, 0, 0, 0, -1))
            dest = recording_agent.verified_unyeared_movie_transfer(source, cfg, "Employee of the Month", start)
            self.assertEqual(dest.parent.name, "Movies Needing Year")
            self.assertEqual(dest.parent.parent, Path(temp))
            self.assertEqual(dest.name, "Employee of the Month - 2026-11-11_2000.mp4")
            self.assertEqual(dest.read_bytes(), b"film")
            self.assertEqual(list((Path(temp) / "Movies").iterdir()), [])

    def test_the_holding_folder_can_be_configured(self):
        cfg = {"plex_path": "/Volumes/Plex/Movies", "needs_year_path": "/Volumes/Plex/Waiting"}
        self.assertEqual(recording_agent.needs_year_root(cfg), Path("/Volumes/Plex/Waiting"))


class PlexMovieYearsToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plex_movie_years", Path(__file__).resolve().parent.parent / "mac" / "plex_movie_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def test_it_finds_only_folders_without_a_year(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("Employee of the Month", "Heat (1995)", ".hidden", "_wrong_movies", "Alien (1979) "):
                (Path(temp) / name).mkdir()
            (Path(temp) / "stray.mp4").write_bytes(b"x")
            self.assertEqual(self.tool.yearless_folders(temp), ["Employee of the Month"])

    def test_rename_adds_the_year_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "Employee of the Month").mkdir()
            (Path(temp) / "Employee of the Month" / "m.mp4").write_bytes(b"x")
            self.assertEqual(self.tool.rename_with_year(temp, "Employee of the Month", "2022"),
                             "Employee of the Month (2022)")
            self.assertTrue((Path(temp) / "Employee of the Month (2022)" / "m.mp4").exists())
            (Path(temp) / "Employee of the Month").mkdir()
            with self.assertRaises(FileExistsError):
                self.tool.rename_with_year(temp, "Employee of the Month", "2022")
            with self.assertRaises(ValueError):
                self.tool.rename_with_year(temp, "Employee of the Month", "22")


class SeriesYearFolderTests(unittest.TestCase):
    def _tv(self, temp, *folders):
        root = Path(temp) / "TV Shows"
        for name in folders:
            season = root / name / "Season 01"
            season.mkdir(parents=True)
            (season / "Show - S01E01.mp4").write_bytes(b"x")
        if not folders:
            root.mkdir()
        return root

    def test_a_folder_with_or_without_a_year_is_the_same_show(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._tv(temp, "Bewitched (1964)")
            self.assertEqual(recording_agent.resolve_show_dir(root, "Bewitched").name, "Bewitched (1964)")
            self.assertEqual(recording_agent.resolve_show_dir(root, "Bewitched", "1964").name, "Bewitched (1964)")
            self.assertEqual(len(recording_agent.find_tv_episode_candidates(root, "Bewitched", 1, 1)), 1)

    def test_a_folder_without_a_year_is_still_used_while_the_library_is_renamed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._tv(temp, "I Dream of Jeannie")
            self.assertEqual(recording_agent.resolve_show_dir(root, "I Dream of Jeannie", "1965").name, "I Dream of Jeannie")
            self.assertEqual(len(recording_agent.find_tv_episode_candidates(root, "I Dream of Jeannie", 1, 1, "1965")), 1)

    def test_a_reboot_is_not_mistaken_for_the_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._tv(temp, "Hawaii Five-0 (1968)")
            self.assertEqual(recording_agent.find_tv_episode_candidates(root, "Hawaii Five-0", 1, 1, "2010"), [])
            self.assertEqual(recording_agent.resolve_show_dir(root, "Hawaii Five-0", "2010").name, "Hawaii Five-0 (2010)")

    def test_both_versions_exist_and_the_right_one_is_picked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._tv(temp, "Hawaii Five-0 (1968)", "Hawaii Five-0 (2010)")
            self.assertEqual(recording_agent.resolve_show_dir(root, "Hawaii Five-0", "2010").name, "Hawaii Five-0 (2010)")
            self.assertEqual(recording_agent.resolve_show_dir(root, "Hawaii Five-0", "1968").name, "Hawaii Five-0 (1968)")

    def test_a_new_show_gets_its_year_in_the_folder_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._tv(temp)
            source = Path(temp) / "rec.mp4"
            source.write_bytes(b"ep")
            dest = recording_agent.verified_episode_transfer(source, root, "Perry Mason", 1, 2, "Chapter Two", year="2020")
            self.assertEqual(dest.parent.parent.name, "Perry Mason (2020)")
            self.assertEqual(dest.parent.name, "Season 01")
            self.assertEqual(dest.name, "Perry Mason - S01E02 - Chapter Two.mp4")

    def test_a_new_show_without_a_year_keeps_the_plain_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._tv(temp)
            source = Path(temp) / "rec.mp4"
            source.write_bytes(b"ep")
            dest = recording_agent.verified_episode_transfer(source, root, "Perry Mason", 1, 2)
            self.assertEqual(dest.parent.parent.name, "Perry Mason")

    def test_an_existing_show_folder_is_reused_for_a_new_episode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._tv(temp, "Bewitched (1964)")
            source = Path(temp) / "rec.mp4"
            source.write_bytes(b"ep")
            dest = recording_agent.verified_episode_transfer(source, root, "Bewitched", 1, 2, year="1964")
            self.assertEqual(dest.parent.parent.name, "Bewitched (1964)")
            self.assertFalse((root / "Bewitched").exists())

    def test_unidentified_episodes_keep_the_year_in_the_holding_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._tv(temp)
            source = Path(temp) / "rec.mp4"
            source.write_bytes(b"ep")
            dest = recording_agent.verified_unidentified_episode_transfer(source, root, "Perry Mason", 0, year="2020")
            self.assertEqual(dest.parent.name, "Perry Mason (2020)")
            self.assertEqual(dest.parent.parent.name, "_Needs Episode Info")


class PlexTvYearsToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        cls.mac = Path(__file__).resolve().parent.parent / "mac"
        spec = importlib.util.spec_from_file_location("plex_movie_years_tv", cls.mac / "plex_movie_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def _config(self, temp, movies, tv):
        import json
        path = Path(temp) / "agent.json"
        path.write_text(json.dumps({"plex_path": str(movies), "plex_tv_path": str(tv), "server_url": ""}))
        return str(path)

    def test_the_tv_folder_is_read_from_the_config_with_tv(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self._config(temp, "/m", "/t")
            self.assertEqual(self.tool.load_defaults(cfg, tv=True)[0], "/t")
            self.assertEqual(self.tool.load_defaults(cfg, tv=False)[0], "/m")

    def test_holding_folders_and_dated_shows_are_left_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("I Dream of Jeannie", "Bewitched (1964)", "_Needs Episode Info", ".hidden"):
                (Path(temp) / name).mkdir()
            self.assertEqual(self.tool.yearless_folders(temp), ["I Dream of Jeannie"])

    def test_it_reports_what_the_recorder_is_copying(self):
        import io, json
        body = io.BytesIO(json.dumps({"finishing": [{"title": "Bewitched"}], "recording": []}).encode())
        with mock.patch.object(self.tool.urllib.request, "urlopen", return_value=body):
            self.assertEqual(self.tool.recorder_copying("http://x"), ["Bewitched"])
        with mock.patch.object(self.tool.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(self.tool.recorder_copying("http://x"), [])
        self.assertEqual(self.tool.recorder_copying(""), [])

    def _run(self, temp, answers, *flags, copying=()):
        tv = Path(temp) / "TV Shows"
        movies = Path(temp) / "Movies"
        movies.mkdir(exist_ok=True)
        cfg = self._config(temp, movies, tv)
        with mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch.object(self.tool, "recorder_copying", return_value=list(copying)), \
             mock.patch("builtins.print") as printed:
            rc = self.tool.main(["--tv", "--config", cfg, *flags])
        return rc, " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)

    def test_a_show_is_renamed_with_the_year_typed(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "TV Shows" / "I Dream of Jeannie" / "Season 01").mkdir(parents=True)
            rc, out = self._run(temp, ["set 1 1965", "y"])
            self.assertEqual(rc, 0)
            self.assertTrue((Path(temp) / "TV Shows" / "I Dream of Jeannie (1965)" / "Season 01").is_dir())
            self.assertFalse((Path(temp) / "TV Shows" / "I Dream of Jeannie").exists())
            self.assertIn("TV Shows library", out)

    def test_a_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "TV Shows" / "Bewitched").mkdir(parents=True)
            rc, _out = self._run(temp, ["set 1 1964", "y"], "--dry-run")
            self.assertEqual(rc, 0)
            self.assertTrue((Path(temp) / "TV Shows" / "Bewitched").is_dir())

    def test_skip_and_quit_leave_folders_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("Bewitched", "Cheers"):
                (Path(temp) / "TV Shows" / name).mkdir(parents=True)
            rc, _out = self._run(temp, ["set 1 1964", "q"])
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(p.name for p in (Path(temp) / "TV Shows").iterdir()), ["Bewitched", "Cheers"])

    def test_it_refuses_while_the_recorder_is_copying(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "TV Shows" / "Bewitched").mkdir(parents=True)
            rc, out = self._run(temp, ["set 1 1964", "y"], copying=["Bewitched"])
            self.assertEqual(rc, 1)
            self.assertIn("copying", out)
            self.assertTrue((Path(temp) / "TV Shows" / "Bewitched").is_dir())

    def test_an_existing_folder_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("Bewitched", "Bewitched (1964)"):
                (Path(temp) / "TV Shows" / name).mkdir(parents=True)
            (Path(temp) / "TV Shows" / "Bewitched" / "a.mp4").write_bytes(b"a")
            _rc, out = self._run(temp, ["set 1 1964", "y"])
            self.assertTrue((Path(temp) / "TV Shows" / "Bewitched" / "a.mp4").exists())
            self.assertIn("Not renamed", out)

    def test_the_tv_script_runs_the_tool_in_tv_mode(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "TV Shows" / "Bewitched").mkdir(parents=True)
            (Path(temp) / "Movies").mkdir()
            cfg = self._config(temp, Path(temp) / "Movies", Path(temp) / "TV Shows")
            result = subprocess.run([sys.executable, str(self.mac / "plex_tv_years.py"), "--config", cfg, "--dry-run"],
                                    input="q\n", capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("1 TV show folder(s) need attention", result.stdout)
            self.assertIn("Nothing was renamed", result.stdout)
            self.assertTrue((Path(temp) / "TV Shows" / "Bewitched").is_dir())


class SuggestYearKindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plex_movie_years_kind", Path(__file__).resolve().parent.parent / "mac" / "plex_movie_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def _suggest(self, kind, year_text="2014–2026"):
        import io, json
        seen = []

        def fake_urlopen(url, timeout=0):
            seen.append(url)
            return io.BytesIO(json.dumps({"year": year_text}).encode())
        with mock.patch.object(self.tool.urllib.request, "urlopen", side_effect=fake_urlopen):
            year = self.tool.suggest_year("http://qnap:5001", "Outlander", kind)
        return year, seen[0]

    def test_a_tv_lookup_asks_for_a_series_and_takes_the_first_year(self):
        year, url = self._suggest("series")
        self.assertEqual(year, "2014")
        self.assertIn("content_type=series", url)
        self.assertIn("title=Outlander", url)

    def test_a_movie_lookup_asks_for_a_movie(self):
        _year, url = self._suggest("movie", "2008")
        self.assertIn("content_type=movie", url)

    def test_no_kind_keeps_the_plain_lookup(self):
        _year, url = self._suggest("")
        self.assertNotIn("content_type", url)

    def test_main_passes_the_kind_for_tv_and_movies(self):
        for flags, kind in (([], "movie"), (["--tv"], "series")):
            with tempfile.TemporaryDirectory() as temp:
                library = Path(temp) / "lib"
                (library / "Show").mkdir(parents=True)
                with mock.patch.object(self.tool, "suggest_year", return_value="") as suggest, \
                     mock.patch.object(self.tool, "recorder_copying", return_value=[]), \
                     mock.patch("builtins.input", side_effect=["q"]), mock.patch("builtins.print"):
                    self.tool.main(["--movies", str(library), "--server", "http://x", *flags])
                self.assertEqual(suggest.call_args.args[2], kind)


class YearRecapTests(unittest.TestCase):
    """One recap and one question, instead of a question for every folder."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plex_movie_years_recap", Path(__file__).resolve().parent.parent / "mac" / "plex_movie_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    SUGGESTIONS = {"Heat": "1995", "Employee of the Month": "2004", "Odd Film": "", "Alien": "1979"}

    def _library(self, temp):
        root = Path(temp) / "Movies"
        for name in self.SUGGESTIONS:
            (root / name).mkdir(parents=True)
        (root / "Dated (2001)").mkdir()
        return root

    def _run(self, root, answers, *flags):
        def suggest(server, name, kind=""):
            return self.SUGGESTIONS.get(name, "")
        out = []
        with mock.patch.object(self.tool, "suggest_year", side_effect=suggest), \
             mock.patch.object(self.tool, "recorder_copying", return_value=[]), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(str(x) for x in a))):
            rc = self.tool.main(["--movies", str(root), "--server", "http://x", *flags])
        return rc, "\n".join(out)

    def _names(self, root):
        return sorted(p.name for p in root.iterdir())

    def test_the_recap_lists_every_folder_once_before_asking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out = self._run(root, ["q"])
            self.assertIn("Will be renamed (3):", out)
            self.assertIn("Alien (1979)", out)
            self.assertIn("Employee of the Month (2004)", out)
            self.assertIn("No year found", out)
            self.assertIn("Odd Film", out)
            self.assertNotIn("Dated", out)                       # already has a year
            self.assertEqual(self._names(root), ["Alien", "Dated (2001)", "Employee of the Month", "Heat", "Odd Film"])

    def test_one_y_renames_every_suggestion_and_leaves_the_rest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            rc, out = self._run(root, ["y"])
            self.assertEqual(rc, 0)
            self.assertEqual(self._names(root), ["Alien (1979)", "Dated (2001)", "Employee of the Month (2004)",
                                                 "Heat (1995)", "Odd Film"])
            self.assertIn("Renamed or fixed 3, moved 0, left alone 1", out)
            self.assertIn("Left alone: Odd Film", out)

    def test_skip_leaves_numbers_out(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            # order is alphabetical: 1 Alien, 2 Employee of the Month, 3 Heat, 4 Odd Film
            self._run(root, ["skip 2", "y"])
            self.assertIn("Employee of the Month", self._names(root))
            self.assertIn("Alien (1979)", self._names(root))

    def test_set_gives_a_year_to_a_folder_with_no_suggestion_and_can_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["set 4 1999", "set 3 2020", "y"])
            names = self._names(root)
            self.assertIn("Odd Film (1999)", names)
            self.assertIn("Heat (2020)", names)

    def test_a_dry_run_renames_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out = self._run(root, ["y"], "--dry-run")
            self.assertIn("would rename", out)
            self.assertEqual(self._names(root), ["Alien", "Dated (2001)", "Employee of the Month", "Heat", "Odd Film"])

    def test_yes_renames_without_asking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            rc, _out = self._run(root, [], "--yes")                # no answers available: input would fail
            self.assertEqual(rc, 0)
            self.assertIn("Heat (1995)", self._names(root))

    def test_a_bad_command_is_explained_and_asked_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out = self._run(root, ["skip 99", "set 4 20x4", "banana", "", "q"])
            self.assertIn('"99" is not a number', out)
            self.assertIn("four-digit year", out)
            self.assertIn('Unknown command "banana"', out)
            self.assertEqual(self._names(root), ["Alien", "Dated (2001)", "Employee of the Month", "Heat", "Odd Film"])

    def test_an_existing_target_is_reported_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            (root / "Heat (1995)").mkdir()
            (root / "Heat" / "a.mp4").write_bytes(b"a")
            _rc, out = self._run(root, ["y"])
            self.assertTrue((root / "Heat" / "a.mp4").exists())
            self.assertIn("Not renamed: Heat", out)
            self.assertIn("could not do 1", out)

    def test_end_of_input_quits_without_renaming(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            out = []
            with mock.patch.object(self.tool, "suggest_year", return_value="1999"), \
                 mock.patch.object(self.tool, "recorder_copying", return_value=[]), \
                 mock.patch("builtins.input", side_effect=EOFError()), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                rc = self.tool.main(["--movies", str(root), "--server", "http://x"])
            self.assertEqual(rc, 0)
            self.assertIn("Nothing was renamed", "\n".join(out))
            self.assertEqual(self._names(root), ["Alien", "Dated (2001)", "Employee of the Month", "Heat", "Odd Film"])

    def test_parse_command_actions(self):
        plan = self.tool.build_plan(["A", "B"], {"A": "2000", "B": ""})
        self.assertEqual(self.tool.parse_command("y", plan)[0], "apply")
        self.assertEqual(self.tool.parse_command("Q", plan)[0], "quit")
        self.assertEqual(self.tool.parse_command("", plan)[0], "continue")
        self.assertEqual(self.tool.parse_command("list", plan)[0], "continue")


class JunkNameTests(unittest.TestCase):
    """Folders named like leftovers are moved out of Plex for a person to look at."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plex_movie_years_junk", Path(__file__).resolve().parent.parent / "mac" / "plex_movie_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    JUNK = "Bottle Shock 20260205 181000-Copy(1)"

    def test_the_junk_pattern(self):
        junk = self.tool.JUNK_NAME
        for name in (self.JUNK, "Movie - Copy", "Movie-Copy(2)", "Movie Copy (3)", "Show 20260205_181000",
                     "Film 20260205 181000 (2008)"):
            self.assertTrue(junk.search(name), name)
        for name in ("Heat (1995)", "The Copy", "Copy Cat", "Blade Runner 2049", "Alien (1979)", "Copycat (1995)"):
            self.assertFalse(junk.search(name), name)

    def test_junk_folders_are_found_even_when_they_have_a_year(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in (self.JUNK, "Film 20260205 181000 (2008)", "Heat (1995)", "_wrong_movies", ".x"):
                (Path(temp) / name).mkdir()
            (Path(temp) / "Movie-Copy.mp4").write_bytes(b"x")          # a loose file is not a folder
            self.assertEqual(self.tool.junk_folders(temp), [self.JUNK, "Film 20260205 181000 (2008)"])

    def test_the_attention_folder_sits_next_to_the_library(self):
        self.assertEqual(self.tool.attention_root("/Volumes/Plex/Movies"), "/Volumes/Plex/Movies Needing Attention")
        self.assertEqual(self.tool.attention_root("/Volumes/Plex/TV Shows", tv=True),
                         "/Volumes/Plex/TV Needing Attention")
        self.assertEqual(self.tool.attention_root("/Volumes/Plex/Movies", configured="/x/Look"), "/x/Look")

    def _library(self, temp):
        root = Path(temp) / "Movies"
        (root / self.JUNK).mkdir(parents=True)
        (root / self.JUNK / f"{self.JUNK}.mp4").write_bytes(b"film-data")
        (root / "Heat").mkdir()
        return root

    def _run(self, root, answers, *flags, suggest=None):
        out = []
        looked_up = []

        def fake_suggest(server, name, kind=""):
            looked_up.append(name)
            return "1995" if name == "Heat" else ""
        with mock.patch.object(self.tool, "suggest_year", side_effect=suggest or fake_suggest), \
             mock.patch.object(self.tool, "recorder_copying", return_value=[]), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            rc = self.tool.main(["--movies", str(root), "--server", "http://x", *flags])
        return rc, "\n".join(out), looked_up

    def test_the_recap_shows_the_move_and_junk_is_not_looked_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out, looked_up = self._run(root, ["q"])
            self.assertIn("Will be moved out of Plex", out)
            self.assertIn("Movies Needing Attention", out)
            self.assertIn(self.JUNK, out)
            self.assertEqual(sorted(looked_up), ["Bottle Shock", "Heat"])      # the CLEANED title, not the junk name

    def test_y_moves_the_junk_folder_out_with_its_contents_and_renames_the_rest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            rc, out, _ = self._run(root, ["y"])
            self.assertEqual(rc, 0)
            moved = Path(temp) / "Movies Needing Attention" / self.JUNK
            self.assertEqual((moved / f"{self.JUNK}.mp4").read_bytes(), b"film-data")   # nothing lost
            self.assertFalse((root / self.JUNK).exists())
            self.assertTrue((root / "Heat (1995)").is_dir())
            self.assertIn("moved 1", out)
            self.assertIn("Nothing was deleted", out)

    def test_skip_leaves_a_junk_folder_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            # alphabetical: 1 Bottle Shock..., 2 Heat
            self._run(root, ["skip 1", "y"])
            self.assertTrue((root / self.JUNK).is_dir())
            self.assertFalse((Path(temp) / "Movies Needing Attention").exists())

    def test_set_is_refused_for_a_junk_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out, _ = self._run(root, ["set 1 2008", "q"])
            self.assertIn("junk name", out)
            self.assertIn("fix 1 2008", out)
            self.assertTrue((root / self.JUNK).is_dir())

    def test_a_dry_run_moves_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out, _ = self._run(root, ["y"], "--dry-run")
            self.assertIn("would move", out)
            self.assertTrue((root / self.JUNK).is_dir())
            self.assertFalse((Path(temp) / "Movies Needing Attention").exists())

    def test_a_name_already_in_the_attention_folder_is_reported_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            existing = Path(temp) / "Movies Needing Attention" / self.JUNK
            existing.mkdir(parents=True)
            (existing / "old.mp4").write_bytes(b"old")
            _rc, out, _ = self._run(root, ["y"])
            self.assertTrue((root / self.JUNK).is_dir())
            self.assertTrue((existing / "old.mp4").exists())
            self.assertIn("Not renamed", out)

    def test_tv_mode_moves_to_tv_needing_attention(self):
        with tempfile.TemporaryDirectory() as temp:
            tv = Path(temp) / "TV Shows"
            (tv / "Show 20260205 181000").mkdir(parents=True)
            out = []
            with mock.patch.object(self.tool, "suggest_year", return_value=""), \
                 mock.patch.object(self.tool, "recorder_copying", return_value=[]), \
                 mock.patch("builtins.input", side_effect=["y"]), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                self.tool.main(["--tv", "--movies", str(tv), "--server", "http://x"])
            self.assertTrue((Path(temp) / "TV Needing Attention" / "Show 20260205 181000").is_dir())

    def test_the_attention_folder_can_be_set_in_the_config(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            cfg = Path(temp) / "agent.json"
            cfg.write_text(json.dumps({"needs_attention_path": "/x/Look"}))
            self.assertEqual(self.tool.load_attention_path(str(cfg)), "/x/Look")
            self.assertEqual(self.tool.load_attention_path(str(Path(temp) / "missing.json")), "")


class CleanedTitleTests(unittest.TestCase):
    """A junk-named folder gets a cleaned name and year to choose from."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plex_movie_years_clean", Path(__file__).resolve().parent.parent / "mac" / "plex_movie_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    JUNK = "Bottle Shock 20260205 181000-Copy(1)"

    def test_the_title_is_cleaned(self):
        clean = self.tool.clean_junk_title
        self.assertEqual(clean(self.JUNK), ("Bottle Shock", ""))
        self.assertEqual(clean("Film 20260205 181000 (2008)"), ("Film", "2008"))
        self.assertEqual(clean("Movie - Copy"), ("Movie", ""))
        self.assertEqual(clean("Movie-Copy(2)"), ("Movie", ""))
        self.assertEqual(clean("The Hobbit: An Unexpected Journey 20260101_200000 - Copy (2)"),
                         ("The Hobbit: An Unexpected Journey", ""))

    def test_fix_in_place_renames_the_folder_and_the_files_with_its_name(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / self.JUNK
            folder.mkdir()
            for name in (f"{self.JUNK}.mp4", f"{self.JUNK}.en.srt", "poster.jpg"):
                (folder / name).write_bytes(b"x")
            new = self.tool.fix_in_place(temp, self.JUNK, "Bottle Shock", "2008")
            self.assertEqual(new, "Bottle Shock (2008)")
            self.assertEqual(sorted(p.name for p in (Path(temp) / new).iterdir()),
                             ["Bottle Shock.en.srt", "Bottle Shock.mp4", "poster.jpg"])
            self.assertFalse(folder.exists())

    def test_fix_in_place_never_overwrites_and_checks_its_input(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / self.JUNK).mkdir()
            (Path(temp) / "Bottle Shock (2008)").mkdir()
            with self.assertRaises(FileExistsError):
                self.tool.fix_in_place(temp, self.JUNK, "Bottle Shock", "2008")
            self.assertTrue((Path(temp) / self.JUNK).is_dir())
            with self.assertRaises(ValueError):
                self.tool.fix_in_place(temp, self.JUNK, "", "2008")
            with self.assertRaises(ValueError):
                self.tool.fix_in_place(temp, self.JUNK, "Bottle Shock", "08")

    def _run(self, root, answers, *flags, years=None):
        years = {"Bottle Shock": "2008", "Heat": "1995"} if years is None else years
        out = []
        with mock.patch.object(self.tool, "suggest_year", side_effect=lambda s, n, k="": years.get(n, "")), \
             mock.patch.object(self.tool, "recorder_copying", return_value=[]), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            rc = self.tool.main(["--movies", str(root), "--server", "http://x", *flags])
        return rc, "\n".join(out)

    def _library(self, temp):
        root = Path(temp) / "Movies"
        (root / self.JUNK).mkdir(parents=True)
        (root / self.JUNK / f"{self.JUNK}.mp4").write_bytes(b"film")
        (root / "Heat").mkdir()
        return root

    def test_the_recap_shows_the_cleaned_name_for_a_junk_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out = self._run(root, ["q"])
            self.assertIn('cleaned: "Bottle Shock (2008)"', out)
            self.assertIn("fix 1", out)

    def test_fix_renames_in_place_instead_of_moving(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out = self._run(root, ["fix 1", "y"])
            self.assertTrue((root / "Bottle Shock (2008)" / "Bottle Shock.mp4").is_file())
            self.assertFalse((Path(temp) / "Movies Needing Attention").exists())
            self.assertIn("Fixed to", out)

    def test_list_shows_the_fixed_in_place_section(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out = self._run(root, ["fix 1", "list", "q"])
            self.assertIn("Will be fixed in place", out)
            self.assertIn("->  Bottle Shock (2008)", out)

    def test_fix_with_a_given_year_overrides_the_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["fix 1 2009", "y"])
            self.assertTrue((root / "Bottle Shock (2009)").is_dir())

    def test_fix_without_a_year_asks_for_one(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out = self._run(root, ["fix 1", "q"], years={})
            self.assertIn("No year found", out)
            self.assertTrue((root / self.JUNK).is_dir())

    def test_fix_all_fixes_every_junk_folder_with_a_year(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            other = "Alien 20260101 090000 - Copy"
            (root / other).mkdir()
            self._run(root, ["fix all", "y"], years={"Bottle Shock": "2008", "Alien": "1979", "Heat": "1995"})
            names = sorted(p.name for p in root.iterdir())
            self.assertIn("Bottle Shock (2008)", names)
            self.assertIn("Alien (1979)", names)
            self.assertFalse((Path(temp) / "Movies Needing Attention").exists())

    def test_a_clashing_cleaned_name_is_flagged_and_not_fixed_by_fix_all(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            (root / "Bottle Shock (2008)").mkdir()
            _rc, out = self._run(root, ["fix all", "fix 2", "y"])
            self.assertIn("likely a duplicate", out)
            self.assertIn("No junk-named folder has a cleaned name", out)
            # the junk copy was moved out, the proper folder untouched
            self.assertTrue((Path(temp) / "Movies Needing Attention" / self.JUNK).is_dir())
            self.assertTrue((root / "Bottle Shock (2008)").is_dir())

    def test_move_puts_a_fixed_one_back_to_move_out(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["fix 1", "move 1", "y"])
            self.assertTrue((Path(temp) / "Movies Needing Attention" / self.JUNK).is_dir())

    def test_the_year_in_the_junk_name_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Movies"
            (root / "Film 20260205 181000 (2008)").mkdir(parents=True)
            self._run(root, ["fix 1", "y"], years={})
            self.assertTrue((root / "Film (2008)").is_dir())

    def test_fix_on_a_normal_folder_points_to_set(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out = self._run(root, ["fix 2", "q"])
            self.assertIn("use set 2 YEAR", out)


class RenameOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plex_movie_years_only", Path(__file__).resolve().parent.parent / "mac" / "plex_movie_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    JUNK = "Bottle Shock 20260205 181000-Copy(1)"
    JUNK2 = "Alien 20260101 090000 - Copy"

    def _library(self, temp):
        root = Path(temp) / "Movies"
        for name in (self.JUNK, self.JUNK2, "Heat"):
            (root / name).mkdir(parents=True)
        return root

    def _run(self, root, answers):
        out = []
        years = {"Bottle Shock": "2008", "Alien": "1979", "Heat": "1995"}
        with mock.patch.object(self.tool, "suggest_year", side_effect=lambda s, n, k="": years.get(n, "")), \
             mock.patch.object(self.tool, "recorder_copying", return_value=[]), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.tool.main(["--movies", str(root), "--server", "http://x"])
        return "\n".join(out)

    def _names(self, root):
        return sorted(p.name for p in root.iterdir())

    def test_rename_only_leaves_every_junk_folder_alone_and_still_renames_the_rest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            out = self._run(root, ["rename only", "y"])
            self.assertEqual(self._names(root), sorted([self.JUNK, self.JUNK2, "Heat (1995)"]))
            self.assertFalse((Path(temp) / "Movies Needing Attention").exists())
            self.assertIn("2 junk-named folder(s) will be left alone", out)

    def test_it_also_undoes_a_fix_chosen_earlier(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["fix all", "rename only", "y"])
            self.assertIn(self.JUNK, self._names(root))
            self.assertNotIn("Bottle Shock (2008)", self._names(root))

    def test_the_one_word_spellings_work_too(self):
        plan = self.tool.build_plan(["A 20260101 090000-Copy(1)"], {}, junk=["A 20260101 090000-Copy(1)"])
        for text in ("renameonly", "rename-only", "Rename Only"):
            for row in plan:
                row["skip"] = False
            self.tool.parse_command(text, plan)
            self.assertTrue(plan[0]["skip"], text)

    def test_with_no_junk_folders_it_says_so(self):
        plan = self.tool.build_plan(["Heat"], {"Heat": "1995"})
        action, message = self.tool.parse_command("rename only", plan)
        self.assertEqual(action, "continue")
        self.assertIn("no junk-named folders", message)

    def test_plain_rename_with_something_else_is_not_mistaken_for_it(self):
        plan = self.tool.build_plan(["A 20260101 090000-Copy(1)"], {}, junk=["A 20260101 090000-Copy(1)"])
        action, message = self.tool.parse_command("rename", plan)
        self.assertIn("Unknown command", message)
        self.assertFalse(plan[0]["skip"])

    def test_the_menu_mentions_it_only_when_there_are_junk_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self.assertIn("rename only", self._run(root, ["q"]))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Movies"
            (root / "Heat").mkdir(parents=True)
            self.assertNotIn("rename only", self._run(root, ["q"]))


class FoldedTitleAgentTests(unittest.TestCase):
    def test_a_ligature_title_matches_its_plex_folder(self):
        self.assertEqual(recording_agent.normalized_title("Æon Flux"), recording_agent.normalized_title("Aeon Flux (2005)"))
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "Aeon Flux (2005)").mkdir()
            (Path(temp) / "Aeon Flux (2005)" / "movie.mp4").write_bytes(b"x")
            found = recording_agent.find_plex_candidates(temp, "Æon Flux", "2005")
            self.assertEqual(len(found), 1)


class WantedFromEmptyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        mac = Path(__file__).resolve().parent.parent / "mac"
        sys.path.insert(0, str(mac))
        spec = importlib.util.spec_from_file_location("wanted_from_empty_under_test", mac / "wanted_from_empty.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def _library(self, temp):
        root = Path(temp) / "Movies"
        for name, files in (("Minority Report (2002)", []), ("Quantum of Solace (2008)", ["q.convert.ffmpeg.log"]),
                            ("Sahara (2005)", ["Sahara.mp4.partial"]), ("Heat (1995)", ["Heat.mp4"]),
                            ("Nested (2001)", ["sub/n.mkv"]), ("Nobody s Fool (1994)", []),
                            ("No Year Film", []), ("_wrong_movies", []), (".hidden", [])):
            folder = root / name
            folder.mkdir(parents=True)
            for rel in files:
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (folder / rel).write_bytes(b"x")
        return root

    def test_it_finds_folders_with_no_video_and_reads_title_and_year(self):
        with tempfile.TemporaryDirectory() as temp:
            found = {name: (title, year) for name, title, year in self.tool.empty_film_folders(self._library(temp))}
            self.assertEqual(sorted(found), ["Minority Report (2002)", "No Year Film", "Nobody s Fool (1994)",
                                             "Quantum of Solace (2008)", "Sahara (2005)"])
            self.assertEqual(found["Minority Report (2002)"], ("Minority Report", "2002"))
            self.assertEqual(found["No Year Film"], ("No Year Film", ""))

    def test_the_wanted_list_is_read_and_a_failure_is_detected(self):
        data = {"recommendations": [{"title": "Heat", "year": "1995", "type": "movie"},
                                    {"title": "Bewitched", "year": "1964", "type": "series"}]}
        with mock.patch.object(self.tool, "get_json", return_value=data):
            self.assertEqual(self.tool.already_wanted("http://x"), {("heat", "1995")})
        with mock.patch.object(self.tool, "get_json", return_value={}):
            self.assertIsNone(self.tool.already_wanted("http://x"))

    def test_the_database_title_is_used_only_for_the_same_film(self):
        same = {"title": "Nobody's Fool", "year": "1994", "imdb_id": "tt0110684"}
        with mock.patch.object(self.tool, "get_json", return_value=same):
            self.assertEqual(self.tool.canonical("http://x", "Nobody s Fool", "1994"), ("Nobody's Fool", "tt0110684"))
        other_year = {"title": "Nobody's Fool", "year": "2018", "imdb_id": "tt1"}
        with mock.patch.object(self.tool, "get_json", return_value=other_year):
            self.assertEqual(self.tool.canonical("http://x", "Nobody s Fool", "1994"), ("Nobody s Fool", ""))
        other_title = {"title": "Something Else", "year": "1994", "imdb_id": "tt2"}
        with mock.patch.object(self.tool, "get_json", return_value=other_title):
            self.assertEqual(self.tool.canonical("http://x", "Nobody s Fool", "1994"), ("Nobody s Fool", ""))
        with mock.patch.object(self.tool, "get_json", return_value={}):
            self.assertEqual(self.tool.canonical("http://x", "Nobody s Fool", "1994"), ("Nobody s Fool", ""))

    def _run(self, root, answers, *flags, wanted=None, add_result=""):
        wanted = {("sahara", "2005")} if wanted is None else wanted
        out, added = [], []
        info = {"Nobody s Fool": {"title": "Nobody's Fool", "year": "1994", "imdb_id": "tt0110684"}}

        def fake_get(server, path, params=None, timeout=20):
            return info.get((params or {}).get("title"), {})

        def fake_add(server, row):
            added.append((row["title"], row["year"], row["imdb_id"]))
            return add_result
        with mock.patch.object(self.tool, "get_json", side_effect=fake_get), \
             mock.patch.object(self.tool, "already_wanted", return_value=wanted), \
             mock.patch.object(self.tool, "add_to_wanted", side_effect=fake_add), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            rc = self.tool.main(["--movies", str(root), "--server", "http://x", *flags])
        return rc, "\n".join(out), added

    def test_the_recap_sorts_folders_into_add_already_and_no_year(self):
        with tempfile.TemporaryDirectory() as temp:
            _rc, out, added = self._run(self._library(temp), ["q"])
            self.assertIn("Will be added to Wanted (3):", out)
            self.assertIn("Nobody's Fool (1994)", out)              # the database spelling
            self.assertIn("Already on the Wanted list (1):", out)
            self.assertIn("Sahara (2005)", out)
            self.assertIn("no year in the folder name", out)
            self.assertEqual(added, [])

    def test_y_adds_each_film_with_its_year_and_id(self):
        with tempfile.TemporaryDirectory() as temp:
            rc, out, added = self._run(self._library(temp), ["y"])
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(added), [("Minority Report", "2002", ""), ("Nobody's Fool", "1994", "tt0110684"),
                                             ("Quantum of Solace", "2008", "")])
            self.assertIn("Nothing was removed", out)

    def test_skip_leaves_a_film_out(self):
        with tempfile.TemporaryDirectory() as temp:
            _rc, _out, added = self._run(self._library(temp), ["skip 1", "y"])
            titles = [a[0] for a in added]
            self.assertNotIn("Minority Report", titles)          # number 1 is Minority Report
            self.assertEqual(len(titles), 2)

    def test_a_dry_run_and_quit_add_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out, added = self._run(root, ["y"], "--dry-run")
            self.assertEqual(added, [])
            self.assertIn("would add", out)
            _rc, _out, added = self._run(root, ["q"])
            self.assertEqual(added, [])

    def test_yes_adds_without_asking(self):
        with tempfile.TemporaryDirectory() as temp:
            _rc, _out, added = self._run(self._library(temp), [], "--yes")
            self.assertEqual(len(added), 3)

    def test_a_refused_add_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            _rc, out, _added = self._run(self._library(temp), ["y"], add_result="A movie needs a year.")
            self.assertIn("could not add 3", out)
            self.assertIn("A movie needs a year.", out)

    def test_nothing_is_added_if_the_wanted_list_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as temp:
            out, added = [], []
            with mock.patch.object(self.tool, "already_wanted", return_value=None), \
                 mock.patch.object(self.tool, "add_to_wanted", side_effect=lambda *a: added.append(a) or ""), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                rc = self.tool.main(["--movies", str(self._library(temp)), "--server", "http://x"])
            self.assertEqual(rc, 1)
            self.assertEqual(added, [])
            self.assertIn("Could not read the Wanted list", "\n".join(out))

    def test_add_to_wanted_posts_the_film_and_reads_the_answer(self):
        import io
        sent = []

        def fake_urlopen(request, timeout=0):
            sent.append((request.full_url, json.loads(request.data), request.get_method()))
            return io.BytesIO(json.dumps({"ok": True}).encode())
        row = {"title": "Heat", "year": "1995", "imdb_id": "tt1"}
        with mock.patch.object(self.tool.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(self.tool.add_to_wanted("http://q:5001", row), "")
        url, body, method = sent[0]
        self.assertEqual((url, method), ("http://q:5001/epg-web/api/wanted", "POST"))
        self.assertEqual(body, {"action": "add", "title": "Heat", "year": "1995", "imdb_id": "tt1", "type": "movie"})
        with mock.patch.object(self.tool.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertIn("down", self.tool.add_to_wanted("http://q:5001", row))


class PrivateMoviesRecorderTests(unittest.TestCase):
    def test_the_private_folder_defaults_to_next_to_the_movies_folder(self):
        self.assertEqual(recording_agent.private_movies_root({"plex_path": "/Volumes/Plex/Movies"}),
                         Path("/Volumes/Plex/Movies Private"))
        self.assertEqual(recording_agent.private_movies_root({"plex_path": "/x/Movies", "private_movies_path": "/y/Hidden"}),
                         Path("/y/Hidden"))

    def test_a_private_film_counts_as_a_film_we_have(self):
        with tempfile.TemporaryDirectory() as temp:
            movies, private = Path(temp) / "Movies", Path(temp) / "Movies Private"
            (movies / "Heat (1995)").mkdir(parents=True)
            (movies / "Heat (1995)" / "Heat.mp4").write_bytes(b"a")
            (private / "Minority Report (2002)").mkdir(parents=True)
            (private / "Minority Report (2002)" / "m.mp4").write_bytes(b"b")
            cfg = {"plex_path": str(movies)}
            found = recording_agent.find_movie_candidates(cfg, movies, "Minority Report", "2002")
            self.assertEqual([p.name for p in found], ["m.mp4"])
            self.assertEqual([p.name for p in recording_agent.find_movie_candidates(cfg, movies, "Heat", "1995")],
                             ["Heat.mp4"])

    def test_a_missing_private_folder_is_fine(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = Path(temp) / "Movies"
            movies.mkdir()
            self.assertEqual(recording_agent.find_movie_candidates({"plex_path": str(movies)}, movies, "Heat", "1995"), [])

    def test_the_same_title_in_another_year_is_still_a_different_film(self):
        with tempfile.TemporaryDirectory() as temp:
            movies, private = Path(temp) / "Movies", Path(temp) / "Movies Private"
            movies.mkdir()
            (private / "Employee of the Month (2022)").mkdir(parents=True)
            (private / "Employee of the Month (2022)" / "e.mp4").write_bytes(b"x")
            cfg = {"plex_path": str(movies)}
            self.assertEqual(recording_agent.find_movie_candidates(cfg, movies, "Employee of the Month", "2004"), [])


class PrivateMoviesToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        mac = Path(__file__).resolve().parent.parent / "mac"
        sys.path.insert(0, str(mac))
        spec = importlib.util.spec_from_file_location("private_movies_under_test", mac / "private_movies.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def _library(self, temp):
        movies = Path(temp) / "Movies"
        for name in ("Heat (1995)", "Minority Report (2002)", "Amélie (2001)", "Empty Folder (2000)"):
            (movies / name).mkdir(parents=True)
            if name != "Empty Folder (2000)":
                (movies / name / "film.mp4").write_bytes(name.encode())
        (movies / "_wrong_movies").mkdir()
        return movies

    def _run(self, movies, answers, *flags, copying=()):
        out = []
        with mock.patch.object(self.tool.base, "recorder_copying", return_value=list(copying)), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            rc = self.tool.main(["--movies", str(movies), "--server", "http://x", *flags])
        return rc, "\n".join(out)

    def test_the_private_folder_default_and_override(self):
        self.assertEqual(self.tool.private_root("/Volumes/Plex/Movies"), "/Volumes/Plex/Movies Private")
        self.assertEqual(self.tool.private_root("/Volumes/Plex/Movies", "/x/Hidden"), "/x/Hidden")

    def test_only_folders_with_a_film_in_them_can_be_chosen(self):
        with tempfile.TemporaryDirectory() as temp:
            names = self.tool.library_films(str(self._library(temp)))
            self.assertEqual(names, ["Amélie (2001)", "Heat (1995)", "Minority Report (2002)"])

    def test_find_ignores_accents_and_capitals(self):
        names = ["Amélie (2001)", "Heat (1995)"]
        self.assertEqual(self.tool.matching(names, "amelie"), ["Amélie (2001)"])
        self.assertEqual(self.tool.matching(names, "HEAT"), ["Heat (1995)"])
        self.assertEqual(self.tool.matching(names, ""), [])

    def test_find_add_go_moves_the_folder_with_its_film(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            rc, out = self._run(movies, ["find minority", "add 1", "go"])
            self.assertEqual(rc, 0)
            private = Path(temp) / "Movies Private" / "Minority Report (2002)"
            self.assertEqual((private / "film.mp4").read_bytes(), b"Minority Report (2002)")
            self.assertFalse((movies / "Minority Report (2002)").exists())
            self.assertTrue((movies / "Heat (1995)").is_dir())          # the others are untouched
            self.assertIn("Nothing was deleted", out)
            self.assertIn("do not share it", out)

    def test_quit_and_dry_run_move_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            self._run(movies, ["find heat", "add 1", "q"])
            self._run(movies, ["find heat", "add 1", "go"], "--dry-run")
            self.assertTrue((movies / "Heat (1995)").is_dir())
            self.assertFalse((Path(temp) / "Movies Private").exists())

    def test_drop_and_chosen_and_bad_input(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            _rc, out = self._run(movies, ["go", "add 99", "chosen", "add 1 2", "drop 1", "chosen", "banana", "q"])
            self.assertIn("Nothing chosen yet: use find and add first.", out)          # go before choosing
            self.assertIn("is not a number", out)
            self.assertIn("Type list, find TEXT", out)
            self.assertIn("1 chosen.", out)                      # two added, one dropped
            self.assertFalse((Path(temp) / "Movies Private").exists())

    def test_it_refuses_while_the_recorder_is_copying(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            rc, out = self._run(movies, ["find heat", "add 1", "go"], copying=["Heat"])
            self.assertEqual(rc, 1)
            self.assertIn("copying", out)
            self.assertTrue((movies / "Heat (1995)").is_dir())

    def test_a_folder_already_in_the_private_folder_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            existing = Path(temp) / "Movies Private" / "Heat (1995)"
            existing.mkdir(parents=True)
            (existing / "old.mp4").write_bytes(b"old")
            _rc, out = self._run(movies, ["find heat", "add 1", "go"])
            self.assertTrue((movies / "Heat (1995)" / "film.mp4").exists())
            self.assertTrue((existing / "old.mp4").exists())
            self.assertIn("Not moved", out)

    def test_restore_puts_a_private_film_back(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            self._run(movies, ["find heat", "add 1", "go"])
            _rc, out = self._run(movies, ["list", "add 1", "go"], "--restore")
            self.assertTrue((movies / "Heat (1995)" / "film.mp4").exists())
            self.assertFalse((Path(temp) / "Movies Private" / "Heat (1995)").exists())
            self.assertIn("Scan the Movies library", out)

    def test_show_lists_the_private_films(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            self._run(movies, ["find heat", "add 1", "go"])
            _rc, out = self._run(movies, [], "--show")
            self.assertIn("1 private movie(s)", out)
            self.assertIn("Heat (1995)", out)


class PrivateRecordingFlowTests(unittest.TestCase):
    """A private film is filed in the private folder, never in the Plex library."""

    def _run(self, private, existing_year=None):
        now = time.time()
        job = {"id": "p1", "title": "Private Film", "stream_id": "1", "start_ts": now - 3600,
               "stop_ts": now - 3600 + 100 * 60, "stream_provider": "primestreams", "stream_extension": "ts",
               "year": "2004", "private": private}
        with tempfile.TemporaryDirectory() as temp:
            movies, private_dir = Path(temp) / "Movies", Path(temp) / "Movies Private"
            movies.mkdir()
            cfg = {"local_recordings": temp, "plex_path": str(movies), "plex_tv_path": temp + "/TV",
                   "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "recording_tail_seconds": 45,
                   "epg_url": "http://x.test", "epg_user": "u", "epg_pass": "p", "heartbeat_seconds": 1,
                   "transfer_wait_timeout": 5, "transfer_retry_seconds": 1}
            api = mock.Mock()
            probe = {"duration": 100 * 60 + 40, "height": 480}
            seen = {}

            def fake_transfer(source, root, title, existing_path=None, progress_callback=None, year=""):
                seen["root"], seen["year"] = str(root), year
                return Path(root) / f"{title} ({year})" / f"{title}.mp4"
            with mock.patch.object(recording_agent, "probe_media", return_value=probe), \
                 mock.patch.object(recording_agent, "heartbeat_sleep", return_value=True), \
                 mock.patch.object(recording_agent, "record_with_resume", return_value=0), \
                 mock.patch.object(recording_agent, "run_process", return_value=0), \
                 mock.patch.object(recording_agent, "plex_mount_available", return_value=True), \
                 mock.patch.object(recording_agent, "archive_terminal_result"), \
                 mock.patch.object(recording_agent, "discard_failed_media"), \
                 mock.patch.object(recording_agent, "verified_transfer", side_effect=fake_transfer):
                recording_agent.process_job(api, job, cfg)
            return seen, str(movies), str(private_dir), private_dir.is_dir()

    def test_a_private_film_goes_to_the_private_folder(self):
        seen, movies, private_dir, created = self._run(True)
        self.assertEqual(seen["root"], private_dir)
        self.assertNotEqual(seen["root"], movies)
        self.assertEqual(seen["year"], "2004")

    def test_a_normal_film_goes_to_plex(self):
        seen, movies, _private_dir, created = self._run(False)
        self.assertEqual(seen["root"], movies)

    def test_the_private_folder_is_created_when_the_share_is_mounted(self):
        _seen, _movies, _private_dir, created = self._run(True)
        self.assertTrue(created)

    def test_a_private_flag_is_ignored_for_tv(self):
        job = {"id": "t1", "title": "Show", "season_num": 1, "episode_num": 2, "private": True}
        episode = recording_agent.episode_metadata(job)
        self.assertIsNotNone(episode)
        # (the choice is made in process_job: private applies only when there is no episode and no series)
        private_film = bool(job.get("private")) and not (episode or False)
        self.assertFalse(private_film)


class CopyMarkerIsNotAYearTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plex_movie_years_copy", Path(__file__).resolve().parent.parent / "mac" / "plex_movie_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def test_a_film_named_copy_with_a_year_is_not_junk(self):
        for name in ("Carbon Copy (1981)", "Copy Cat (1995)", "Perfect Copy (2003)", "Movie-Copy (1999)"):
            self.assertFalse(self.tool.JUNK_NAME.search(name), name)

    def test_real_copy_markers_still_match(self):
        for name in ("Movie Copy (1)", "Movie Copy (12)", "Movie-Copy(2)", "Movie - Copy", "Movie-Copy(123)"):
            self.assertTrue(self.tool.JUNK_NAME.search(name), name)

    def test_a_film_with_copy_and_a_year_is_not_listed_or_moved(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "Carbon Copy (1981)").mkdir()
            (Path(temp) / "Carbon Copy (1981)" / "c.mp4").write_bytes(b"x")
            self.assertEqual(self.tool.junk_folders(temp), [])
            self.assertEqual(self.tool.yearless_folders(temp), [])


class MoveNoYearTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plex_movie_years_noyear", Path(__file__).resolve().parent.parent / "mac" / "plex_movie_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    JUNK = "Caddyshack 20260124 073500-Copy(1)"

    def _library(self, temp):
        root = Path(temp) / "Movies"
        for name in ("Reefer Madness", "Hellboy Blood and Iron", "Mad Max 3 Beyond Thunderdome", self.JUNK):
            (root / name).mkdir(parents=True)
            (root / name / "film.mp4").write_bytes(name.encode())
        return root

    def _run(self, root, answers):
        years = {"Reefer Madness": "1936"}
        out = []
        with mock.patch.object(self.tool, "suggest_year", side_effect=lambda s, n, k="": years.get(n, "")), \
             mock.patch.object(self.tool, "recorder_copying", return_value=[]), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.tool.main(["--movies", str(root), "--server", "http://x"])
        return "\n".join(out)

    def _names(self, path):
        return sorted(p.name for p in Path(path).iterdir()) if Path(path).exists() else []

    def test_move_noyear_sends_every_folder_with_no_year_out_of_plex(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            out = self._run(root, ["move noyear", "y"])
            attention = Path(temp) / "Movies Needing Attention"
            self.assertEqual(self._names(attention), sorted(["Hellboy Blood and Iron", "Mad Max 3 Beyond Thunderdome", self.JUNK]))
            self.assertEqual((attention / "Hellboy Blood and Iron" / "film.mp4").read_bytes(), b"Hellboy Blood and Iron")
            self.assertIn("Reefer Madness (1936)", self._names(root))      # the one with a year is renamed in place
            self.assertIn("Nothing was deleted", out)

    def test_move_by_number_moves_just_those(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            # alphabetical: 1 Caddyshack..., 2 Hellboy, 3 Mad Max, 4 Reefer Madness
            self._run(root, ["move 2", "y"])
            attention = Path(temp) / "Movies Needing Attention"
            self.assertIn("Hellboy Blood and Iron", self._names(attention))
            self.assertIn("Mad Max 3 Beyond Thunderdome", self._names(root))

    def test_a_given_year_keeps_a_folder_in_plex_with_move_noyear(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["set 3 1985", "move noyear", "y"])
            self.assertIn("Mad Max 3 Beyond Thunderdome (1985)", self._names(root))
            self.assertNotIn("Mad Max 3 Beyond Thunderdome", self._names(Path(temp) / "Movies Needing Attention"))

    def test_skip_after_move_noyear_leaves_a_folder_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["move noyear", "skip 2", "y"])
            self.assertIn("Hellboy Blood and Iron", self._names(root))

    def test_the_recap_says_why_they_are_moved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            out = self._run(root, ["move noyear", "list", "q"])
            self.assertIn("no year yet: moved out until you know it", out)
            self.assertIn("folder(s) with no year will be moved out of Plex", out)

    def test_move_with_bad_input_is_explained(self):
        plan = self.tool.build_plan(["A"], {})
        self.assertIn("is not a number", self.tool.parse_command("move 9", plan)[1])
        self.assertIn("move needs numbers", self.tool.parse_command("move", plan)[1])
        self.assertIn("no folders with no year", self.tool.parse_command("move noyear", self.tool.build_plan(["A"], {"A": "2000"}))[1])

    def test_rename_only_still_leaves_moved_folders_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["move noyear", "rename only", "y"])
            self.assertFalse((Path(temp) / "Movies Needing Attention").exists())


class AdultSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        mac = Path(__file__).resolve().parent.parent / "mac"
        sys.path.insert(0, str(mac))
        spec = importlib.util.spec_from_file_location("private_movies_adult", mac / "private_movies.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def test_scoring(self):
        sig = self.tool.adult_signals
        self.assertEqual(sig({"found": True, "adult": True})[0], 3)
        self.assertEqual(sig({"found": True, "keywords": ["softcore"]})[0], 2)
        self.assertEqual(sig({"found": True, "keywords": ["softcore", "erotica"]})[0], 3)       # two strong ones
        self.assertEqual(sig({"found": True, "keywords": ["eroticism"]})[0], 1)                 # weak
        self.assertEqual(sig({"found": True, "certification": "NC-17"})[0], 1)                  # a rating alone is not enough
        self.assertEqual(sig({"found": True, "certification": "X"})[1], ["rated X"])
        self.assertEqual(sig({"found": True, "certification": "Unrated"})[0], 0)
        self.assertEqual(sig({"found": True, "certification": "R", "keywords": ["nudity", "violence"]})[0], 0)
        self.assertEqual(sig({"found": False}), (0, []))
        self.assertEqual(sig({}), (0, []))
        self.assertEqual(sig({"found": True, "adult": True, "certification": "NC-17", "keywords": ["softcore"]})[0], 6)

    def test_keywords_are_matched_whole_and_plot_details_are_ignored(self):
        sig = self.tool.adult_signals
        self.assertEqual(sig({"found": True, "keywords": ["homoeroticism"]})[0], 0)             # not "eroticism"
        self.assertEqual(sig({"found": True, "keywords": ["pornography", "pornographic video"]})[0], 0)
        self.assertEqual(sig({"found": True, "keywords": ["Erotic Thriller"]})[0], 2)           # capitals do not matter
        self.assertEqual(sig({"found": True, "keywords": ["sexploitation"], "certification": "NC-17"})[0], 3)

    def test_the_tiers(self):
        self.assertEqual(self.tool.tier(2), "maybe")
        self.assertEqual(self.tool.tier(3), "likely")
        self.assertEqual(self.tool.tier(8), "likely")

    def _library(self, temp):
        root = Path(temp) / "Movies"
        for name in ("Heat (1995)", "Adult One (1998)", "Raunchy Two (2003)", "Unknown Film (1990)", "Unrated Only (2001)",
                     "Violent Cut (1980)"):
            (root / name).mkdir(parents=True)
            (root / name / "film.mp4").write_bytes(name.encode())
        return root

    SIGNALS = {
        "Heat": {"found": True, "certification": "R", "keywords": ["heist"], "adult": False},
        "Adult One": {"found": True, "adult": True, "certification": "NC-17", "keywords": ["erotic thriller"]},
        "Raunchy Two": {"found": True, "certification": "R", "keywords": ["erotic thriller"]},
        "Violent Cut": {"found": True, "certification": "NC-17", "keywords": ["dystopia"]},
        "Unrated Only": {"found": True, "certification": "NR", "keywords": []},
    }

    def _run(self, root, answers, cache_path, signals=None, calls=None):
        signals = self.SIGNALS if signals is None else signals
        out = []

        def fake_get(server, path, params=None, timeout=20):
            if calls is not None:
                calls.append(params["title"])
            return signals.get(params["title"], {"found": False})
        with mock.patch.object(self.tool.scan, "get_json", side_effect=fake_get), \
             mock.patch.object(self.tool.base, "recorder_copying", return_value=[]), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.tool.main(["--movies", str(root), "--server", "http://x", "--cache", str(cache_path)])
        return "\n".join(out)

    def test_adult_lists_likely_films_with_reasons_strongest_first(self):
        with tempfile.TemporaryDirectory() as temp:
            out = self._run(self._library(temp), ["adult", "q"], Path(temp) / "cache.json")
            self.assertIn("2 of 6 may be adult: 1 likely, 1 maybe (1 not found in the database", out)
            self.assertIn("1. Adult One (1998)   likely: adult flag; keywords: erotic thriller; rated NC-17", out)
            self.assertIn("2. Raunchy Two (2003)   maybe: keywords: erotic thriller", out)
            self.assertNotIn("Heat (1995)   ", out)
            self.assertNotIn("Violent Cut (1980)   ", out)            # an NC-17 rating alone is not listed
            self.assertNotIn("Unrated Only (2001)   ", out)

    def test_add_all_and_go_move_the_suggestions_to_the_private_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["adult", "add all", "go"], Path(temp) / "cache.json")
            private = Path(temp) / "Movies Private"
            self.assertEqual(sorted(p.name for p in private.iterdir()), ["Adult One (1998)", "Raunchy Two (2003)"])
            self.assertTrue((root / "Heat (1995)").is_dir())
            self.assertEqual((private / "Adult One (1998)" / "film.mp4").read_bytes(), b"Adult One (1998)")

    def test_add_by_number_picks_from_the_suggestions_on_screen(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["adult", "add 2", "go"], Path(temp) / "cache.json")
            self.assertEqual(sorted(p.name for p in (Path(temp) / "Movies Private").iterdir()), ["Raunchy Two (2003)"])

    def test_the_second_scan_only_asks_about_new_films(self):
        with tempfile.TemporaryDirectory() as temp:
            root, cache = self._library(temp), Path(temp) / "cache.json"
            calls = []
            self._run(root, ["adult", "q"], cache, calls=calls)
            first = len(calls)
            self.assertGreaterEqual(first, 6)
            calls.clear()
            self._run(root, ["adult", "q"], cache, calls=calls)
            self.assertEqual(calls, ["Unknown Film"])          # not found last time, so asked again; the rest are remembered

    def test_when_nothing_can_be_looked_up_it_says_why(self):
        with tempfile.TemporaryDirectory() as temp:
            out = self._run(self._library(temp), ["adult", "q"], Path(temp) / "cache.json", signals={})
            self.assertIn("None of them could be looked up", out)

    def test_adult_is_refused_in_restore_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            private = Path(temp) / "Movies Private" / "Old (2000)"
            private.mkdir(parents=True)
            (private / "o.mp4").write_bytes(b"x")
            out = []
            with mock.patch.object(self.tool.base, "recorder_copying", return_value=[]), \
                 mock.patch("builtins.input", side_effect=["adult", "q"]), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                self.tool.main(["--movies", str(root), "--server", "http://x", "--restore"])
            self.assertIn("adult works on the Movies library", "\n".join(out))

    def test_a_cache_file_is_written_and_read_back(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cache.json"
            self.tool.save_cache(str(path), {"A (2000)": {"found": True}})
            self.assertEqual(self.tool.load_cache(str(path)), {"A (2000)": {"found": True}})
            self.assertEqual(self.tool.load_cache(str(Path(temp) / "missing.json")), {})
            self.assertEqual(self.tool.default_cache_path("~/EPG-Manager/recording_agent.json").endswith(
                "EPG-Manager/film_signals_cache.json"), True)


class NotFoundListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        mac = Path(__file__).resolve().parent.parent / "mac"
        sys.path.insert(0, str(mac))
        spec = importlib.util.spec_from_file_location("private_movies_notfound", mac / "private_movies.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def _run(self, root, answers, cache_path):
        out = []

        def fake_get(server, path, params=None, timeout=20):
            return {"found": True, "certification": "R", "keywords": []} if params["title"] == "Heat" else {"found": False}
        with mock.patch.object(self.tool.scan, "get_json", side_effect=fake_get), \
             mock.patch.object(self.tool.base, "recorder_copying", return_value=[]), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.tool.main(["--movies", str(root), "--server", "http://x", "--cache", str(cache_path)])
        return "\n".join(out)

    def _library(self, temp):
        root = Path(temp) / "Movies"
        for name in ("Heat (1995)", "Obscure Two (1975)", "Obscure One (1972)"):
            (root / name).mkdir(parents=True)
            (root / name / "film.mp4").write_bytes(b"x")
        return root

    def test_notfound_lists_the_unknown_films_and_add_works_on_that_list(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            out = self._run(root, ["adult", "notfound", "add 2", "go"], Path(temp) / "c.json")
            self.assertIn("2 not found in the database", out)
            self.assertIn("1. Obscure One (1972)", out)
            self.assertIn("2. Obscure Two (1975)", out)
            self.assertEqual(sorted(p.name for p in (Path(temp) / "Movies Private").iterdir()), ["Obscure Two (1975)"])

    def test_notfound_before_a_scan_says_to_run_adult_first(self):
        with tempfile.TemporaryDirectory() as temp:
            out = self._run(self._library(temp), ["notfound", "q"], Path(temp) / "c.json")
            self.assertIn("run adult first", out)


class CheckYearsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        mac = Path(__file__).resolve().parent.parent / "mac"
        sys.path.insert(0, str(mac))
        spec = importlib.util.spec_from_file_location("check_years_under_test", mac / "check_years.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def test_judge(self):
        judge = lambda *a, **k: self.tool.judge(*a, this_year=2026, **k)
        one = [{"year": "2005", "runtime": 0}]
        self.assertEqual(judge("2005", one, 0)[1], "ok")                       # the year is a real film's year
        self.assertEqual(judge("2026", one, 0)[:2], ("2005", "the only film with this title"))
        self.assertEqual(judge("2026", [], 0), ("", "unknown", ""))
        two = [{"year": "1981", "runtime": 97}, {"year": "2011", "runtime": 110}]
        self.assertEqual(judge("2026", two, 96)[0], "1981")                    # the file's length picks the film
        self.assertEqual(judge("2026", two, 111)[0], "2011")
        self.assertEqual(judge("2011", two, 98)[1], "ok")                      # the year matches one of them
        guess, reason, options = judge("2026", two, 104)                       # between the two: not guessed
        self.assertEqual((guess, reason), ("", "choose"))
        self.assertIn("1981 (97 min), 2011 (110 min)", options)
        self.assertIn("your file is 104 min", options)
        self.assertEqual(judge("2026", two, 0)[1], "choose")                   # no file length: not guessed

    def test_a_year_one_off_is_fine(self):
        judge = lambda *a, **k: self.tool.judge(*a, this_year=2026, **k)
        for folder, film in (("2006", "2007"), ("2002", "2001"), ("2005", "2006")):
            self.assertEqual(judge(folder, [{"year": film, "runtime": 120}], 125)[1], "ok", folder)
        self.assertEqual(judge("2016", [{"year": "2014", "runtime": 100}], 100)[0], "2014")     # two off is not

    def test_a_big_change_is_not_renamed_unless_confirmed(self):
        judge = lambda *a, **k: self.tool.judge(*a, this_year=2026, **k)
        guess, reason, options = judge("1961", [{"year": "1983", "runtime": 100}], 106)         # Breathless
        self.assertEqual((guess, reason), ("", "big"))
        self.assertIn("22-year change", options)
        self.assertIn("set N 1983", options)
        self.assertEqual(judge("2013", [{"year": "1993", "runtime": 109}], 121)[1], "big")
        self.assertEqual(judge("1990", [{"year": "2020", "runtime": 100}], 100)[1], "big")

    def test_a_folder_year_this_year_or_later_is_clearly_wrong_so_it_is_renamed(self):
        judge = lambda *a, **k: self.tool.judge(*a, this_year=2026, **k)
        self.assertEqual(judge("2026", [{"year": "1964", "runtime": 87}], 87)[:2], ("1964", "the only film with this title"))
        self.assertEqual(judge("2026", [{"year": "1956", "runtime": 220}], 220)[0], "1956")

    def test_short_films_and_episodes_of_the_wrong_length_are_ignored(self):
        judge = lambda *a, **k: self.tool.judge(*a, this_year=2026, **k)
        cands = [{"year": "2009", "runtime": 9}, {"year": "2017", "runtime": 12}, {"year": "2023", "runtime": 2}]
        self.assertEqual(judge("1980", cands, 116)[1], "unknown")              # 9 to 5: none is a feature of that length
        mixed = [{"year": "2011", "runtime": 12}, {"year": "1986", "runtime": 107}]
        self.assertEqual(judge("2026", mixed, 108)[:2], ("1986", "the only film with this title"))

    def test_a_single_film_whose_length_does_not_fit_is_a_choice(self):
        judge = lambda *a, **k: self.tool.judge(*a, this_year=2026, **k)
        guess, reason, options = judge("2026", [{"year": "2005", "runtime": 60}], 100)
        self.assertEqual((guess, reason), ("", "choose"))
        self.assertIn("the length does not fit well", options)

    def test_only_dated_folders_with_a_video_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            for name, video in (("Contagion (2026)", True), ("Heat (1995)", True), ("Empty (2000)", False),
                                ("No Year", True), ("_wrong (2001)", True)):
                (Path(temp) / name).mkdir()
                if video:
                    (Path(temp) / name / "f.mp4").write_bytes(b"x")
            names = [n for n, _t, _y in self.tool.dated_folders(temp)]
            self.assertEqual(names, ["Contagion (2026)", "Heat (1995)"])

    def test_change_year_renames_the_folder_and_files_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Contagion (2026)"
            folder.mkdir()
            (folder / "Contagion (2026).mp4").write_bytes(b"x")
            (folder / "poster.jpg").write_bytes(b"p")
            new = self.tool.change_year(temp, "Contagion (2026)", "Contagion", "2011")
            self.assertEqual(new, "Contagion (2011)")
            self.assertEqual(sorted(p.name for p in (Path(temp) / new).iterdir()), ["Contagion (2011).mp4", "poster.jpg"])
            (Path(temp) / "Heat (2026)").mkdir()
            (Path(temp) / "Heat (1995)").mkdir()
            (Path(temp) / "Heat (1995)" / "keep.mp4").write_bytes(b"k")
            with self.assertRaises(FileExistsError):
                self.tool.change_year(temp, "Heat (2026)", "Heat", "1995")
            self.assertTrue((Path(temp) / "Heat (1995)" / "keep.mp4").exists())
            with self.assertRaises(ValueError):
                self.tool.change_year(temp, "Heat (2026)", "Heat", "95")

    def test_an_empty_folder_of_the_new_name_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "Spider-Man 3 (2026)").mkdir()
            (Path(temp) / "Spider-Man 3 (2026)" / "s.mp4").write_bytes(b"s")
            (Path(temp) / "Spider-Man 3 (2007)").mkdir()           # an empty leftover
            self.tool.change_year(temp, "Spider-Man 3 (2026)", "Spider-Man 3", "2007")
            self.assertEqual([p.name for p in (Path(temp) / "Spider-Man 3 (2007)").iterdir()], ["s.mp4"])
            self.assertFalse((Path(temp) / "Spider-Man 3 (2026)").exists())

    def _library(self, temp):
        root = Path(temp) / "Movies"
        for name in ("Contagion (2026)", "Arthur (2026)", "Nobody s Fool (1995)", "Mystery Film (2001)", "Heat (1995)",
                     "Verified Film (1999)", "Big Jump (1961)"):
            (root / name).mkdir(parents=True)
            (root / name / "f.mp4").write_bytes(name.encode())
        return root

    CANDIDATES = {
        "Contagion": [{"year": "2011", "runtime": 106}],
        "Arthur": [{"year": "1981", "runtime": 97}, {"year": "2011", "runtime": 110}],
        "Nobody s Fool": [{"year": "1994", "runtime": 110}],            # one year off: fine
        "Big Jump": [{"year": "1983", "runtime": 100}],
        "Mystery Film": [],
        "Heat": [{"year": "1995", "runtime": 170}],
        "Verified Film": [{"year": "1999", "runtime": 90}],
    }

    def _run(self, root, answers, *flags, minutes=0, copying=(), cache=None):
        out = []
        asked = []

        def fake_get(server, path, params=None, timeout=20):
            asked.append(params["title"])
            return {"candidates": self.CANDIDATES.get(params["title"], [])}
        cache_path = Path(root).parent / "cache.json"
        cache_path.write_text(json.dumps(cache or {}))
        with mock.patch.object(self.tool.scan, "get_json", side_effect=fake_get), \
             mock.patch.object(self.tool, "file_minutes", return_value=minutes), \
             mock.patch.object(self.tool.base, "recorder_copying", return_value=list(copying)), \
             mock.patch("builtins.input", side_effect=list(answers)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            rc = self.tool.main(["--movies", str(root), "--server", "http://x", "--cache", str(cache_path), *flags])
        return rc, "\n".join(out), asked

    def _names(self, root):
        return sorted(p.name for p in Path(root).iterdir())

    def test_the_recap_sorts_the_folders_and_skips_confirmed_ones(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            cache = {"Verified Film (1999)": {"found": True}}
            _rc, out, asked = self._run(root, ["q"], cache=cache)
            self.assertNotIn("Verified Film", asked)                           # confirmed by the earlier scan
            self.assertIn("Contagion (2026)  ->  Contagion (2011)   [the only film with this title]", out)
            self.assertIn("2 of them have a year that matches a real film", out)    # Heat and Nobody s Fool (a year off)
            self.assertNotIn("Nobody s Fool", out)
            self.assertIn("Big changes, check these first", out)
            self.assertIn("Big Jump (1961)", out)
            self.assertIn("Several films share the title: you choose (1)", out)
            self.assertIn("Arthur (2026)", out)
            self.assertIn("Could not check, the title is not in the film database (1)", out)
            self.assertIn("Mystery Film (2001)", out)
            self.assertEqual(self._names(root)[:1], ["Arthur (2026)"])         # nothing renamed on q

    def test_y_renames_the_certain_ones_and_leaves_the_rest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out, _asked = self._run(root, ["y"])
            names = self._names(root)
            self.assertIn("Contagion (2011)", names)
            self.assertIn("Big Jump (1961)", names)                            # a big change: not renamed by y
            self.assertIn("Arthur (2026)", names)                              # a choice: not renamed
            self.assertIn("Mystery Film (2001)", names)
            self.assertIn("Heat (1995)", names)
            self.assertIn("Renamed 1", out)

    def test_the_file_length_resolves_a_choice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, ["y"], minutes=96)             # 97 min fits the 1981 film, 110 min does not
            self.assertIn("Arthur (1981)", self._names(root))

    def test_set_resolves_a_choice_and_skip_leaves_one_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            # the list is numbered alphabetically by folder: 1 Arthur, 2 Big Jump, 3 Contagion, 4 Mystery Film
            self._run(root, ["set 1 2011", "skip 3", "y"])
            names = self._names(root)
            self.assertIn("Arthur (2011)", names)
            self.assertIn("Contagion (2026)", names)

    def test_a_dry_run_renames_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            _rc, out, _asked = self._run(root, ["y"], "--dry-run")
            self.assertIn("would rename", out)
            self.assertIn("Contagion (2026)", self._names(root))

    def test_yes_renames_without_asking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            self._run(root, [], "--yes")
            self.assertIn("Contagion (2011)", self._names(root))

    def test_it_refuses_while_the_recorder_is_copying(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            rc, out, _asked = self._run(root, ["y"], copying=["Heat"])
            self.assertEqual(rc, 1)
            self.assertIn("copying", out)
            self.assertIn("Contagion (2026)", self._names(root))

    def test_when_everything_is_confirmed_it_says_so(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            cache = {n: {"found": True} for n in self._names(root)}
            _rc, out, asked = self._run(root, [], cache=cache)
            self.assertIn("already confirmed", out)
            self.assertEqual(asked, [])

    def test_set_confirms_a_big_change_and_renames(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp)
            # 1 Arthur, 2 Big Jump, 3 Contagion, 4 Mystery Film
            self._run(root, ["set 2 1983", "y"])
            self.assertIn("Big Jump (1983)", self._names(root))

    def test_bad_commands_are_explained(self):
        plan = [{"n": 1, "name": "A (2026)", "title": "A", "year": "2026", "new": "", "reason": "choose",
                 "options": "", "skip": False}]
        self.assertIn("is not a number", self.tool.parse_command("skip 9", plan)[1])
        self.assertIn("set needs", self.tool.parse_command("set 1 20", plan)[1])
        self.assertIn("Unknown command", self.tool.parse_command("banana", plan)[1])
        self.assertEqual(self.tool.parse_command("q", plan)[0], "quit")



class LooseMatchToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        mac = Path(__file__).resolve().parent.parent / "mac"
        sys.path.insert(0, str(mac))
        spec = importlib.util.spec_from_file_location("private_movies_loose", mac / "private_movies.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def test_an_ampersand_is_and_in_the_agent_and_the_tool(self):
        self.assertEqual(recording_agent.normalized_title("Dumb & Dumber (1994)"), recording_agent.normalized_title("Dumb and Dumber"))
        self.assertEqual(self.tool.base.fold_title("Dumb & Dumber"), "dumbanddumber")

    def test_a_loose_match_says_so_in_the_reasons(self):
        score, reasons = self.tool.adult_signals({"found": True, "match": "loose", "title": "Some Film", "keywords": ["softcore"]})
        self.assertEqual(score, 2)
        self.assertIn('close title match, found as "Some Film": check it is the same film', reasons[-1])
        exact = self.tool.adult_signals({"found": True, "match": "exact", "title": "Some Film", "keywords": ["softcore"]})
        self.assertEqual(len(exact[1]), 1)
        self.assertEqual(self.tool.adult_signals({"found": True, "match": "loose", "title": "X"}), (0, []))      # nothing to check


class TidyLibraryTests(unittest.TestCase):
    """Loose files into folders, and films that have two folders."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "tidy_library", Path(__file__).resolve().parent.parent / "mac" / "tidy_library.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def test_file_names_are_cleaned(self):
        clean = self.tool.clean_file_name
        self.assertEqual(clean("The_Descendants__2011_.mp4"), ("The Descendants", "2011", False))
        self.assertEqual(clean("The Count Of Monte Cristo (2002).mp4"), ("The Count Of Monte Cristo", "2002", False))
        self.assertEqual(clean("Joker-Copy(1).mp4"), ("Joker", "", True))
        self.assertEqual(clean("FUGITIVE_16X9.mp4"), ("FUGITIVE", "", False))
        self.assertEqual(clean("2012.mp4"), ("2012", "", False))            # a title that is a year stays a title
        self.assertEqual(clean("IF.mp4"), ("IF", "", False))
        self.assertEqual(clean("Ferris Bueller s Day Off.mp4")[0], "Ferris Bueller's Day Off")
        self.assertEqual(clean("Won t Back Down.mp4")[0], "Won't Back Down")
        self.assertEqual(clean("Kill Bill Vol1.mp4")[0], "Kill Bill Vol 1")

    def test_only_loose_videos_are_listed(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = Path(temp)
            (movies / "Heat (1995)").mkdir()
            for name in ("A.mp4", "b.mkv", "notes.txt", ".hidden.mp4"):
                (movies / name).write_bytes(b"x")
            self.assertEqual(self.tool.loose_files(str(movies)), ["A.mp4", "b.mkv"])

    def test_year_is_picked_only_when_one_film_fits(self):
        pick = self.tool.pick_year
        self.assertEqual(pick([], 100)[0], "")
        self.assertEqual(pick([{"year": 1987, "runtime": 100}], 101)[0], "1987")
        both = [{"year": 1960, "runtime": 130}, {"year": 2001, "runtime": 98}]
        self.assertEqual(pick(both, 99)[0], "2001")                        # the length decides
        self.assertEqual(pick(both, 0)[0], "")                             # no length: you choose
        self.assertEqual(pick([{"year": 1987, "runtime": 100}], 150)[0], "")   # does not fit at all

    def test_file_is_filed_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = Path(temp)
            (movies / "Cleaner__2025_.MP4").write_bytes(b"x")
            target = self.tool.file_into_folder(str(movies), "Cleaner__2025_.MP4", "Cleaner", "2025")
            self.assertEqual(Path(target), movies / "Cleaner (2025)" / "Cleaner.mp4")
            self.assertTrue(Path(target).exists())
            (movies / "again.mp4").write_bytes(b"y")
            with self.assertRaises(FileExistsError):
                self.tool.file_into_folder(str(movies), "again.mp4", "Cleaner", "2025")
            self.assertTrue((movies / "again.mp4").exists())
            with self.assertRaises(ValueError):
                self.tool.file_into_folder(str(movies), "again.mp4", "Cleaner", "")

    def test_existing_folder_is_not_merged(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = Path(temp)
            (movies / "Heat (1995)").mkdir()
            (movies / "Heat_1995.mp4").write_bytes(b"x")
            (movies / "Alien (1979).mp4").write_bytes(b"x")
            plan = self.tool.build_loose_plan(str(movies), self.tool.loose_files(str(movies)), "", "ffprobe")
            states = {r["file"]: r["state"] for r in plan}
            self.assertEqual(states["Heat_1995.mp4"], "exists")
            self.assertEqual(states["Alien (1979).mp4"], "ready")
            recap = self.tool.format_loose_recap(plan)
            self.assertIn("Alien (1979).mp4  ->  Alien (1979)/Alien.mp4", recap)
            self.assertIn("already has a folder", recap)
            self.assertEqual([r["file"] for r in self.tool.loose_to_move(plan)], ["Alien (1979).mp4"])

    def test_commands_change_the_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = Path(temp)
            for name in ("Odd.mp4", "Joker-Copy(1).mp4", "Alien (1979).mp4"):
                (movies / name).write_bytes(b"x")
            plan = self.tool.build_loose_plan(str(movies), self.tool.loose_files(str(movies)), "", "ffprobe")
            keys = self.tool.existing_keys(str(movies))
            number = {r["file"]: r["n"] for r in plan}
            parse = lambda text: self.tool.parse_loose_command(text, plan, keys, str(movies))
            self.assertEqual(plan[number["Odd.mp4"] - 1]["state"], "noyear")
            parse(f"set {number['Odd.mp4']} 1987")
            self.assertEqual(plan[number["Odd.mp4"] - 1]["state"], "ready")
            self.assertIn("Odd.mp4", [r["file"] for r in self.tool.loose_to_move(plan)])
            parse(f"skip {number['Odd.mp4']}")
            self.assertNotIn("Odd.mp4", [r["file"] for r in self.tool.loose_to_move(plan)])
            parse(f"out {number['Joker-Copy(1).mp4']}")
            self.assertEqual([r["file"] for r in self.tool.loose_to_send_out(plan)], ["Joker-Copy(1).mp4"])
            self.assertIn("not a number", parse("skip 99")[1])
            self.assertEqual(parse("q")[0], "quit")
            self.assertEqual(parse("y")[0], "apply")

    def test_duplicate_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = Path(temp)
            for name in ("Tom & Jerry (2021)", "Tom and Jerry (2021)", "Heat (1995)", "Heat (1996)",
                         "Alien (1979)", "Alien (1990)", "Solo (2030)", "Solo (1999)", "Empty (2001)", "Empty (2001) 2"):
                (movies / name).mkdir()
                if "Empty" not in name:
                    (movies / name / "a.mp4").write_bytes(b"x")
            groups = self.tool.find_groups(str(movies), this_year=2026)
            self.assertIn(["Tom & Jerry (2021)", "Tom and Jerry (2021)"], groups)
            self.assertIn(["Heat (1995)", "Heat (1996)"], groups)
            self.assertIn(["Solo (1999)", "Solo (2030)"], groups)           # a future year is plainly wrong
            self.assertNotIn(["Alien (1979)", "Alien (1990)"], groups)      # 11 years apart: two films
            self.assertEqual(len(groups), 3)

    def test_keep_suggestion(self):
        suggest = self.tool.suggest_keep
        self.assertEqual(suggest([{"size": 900, "minutes": 100}, {"size": 500, "minutes": 100}]), 0)
        self.assertEqual(suggest([{"size": 500, "minutes": 100}, {"size": 900, "minutes": 99}]), 1)
        self.assertIsNone(suggest([{"size": 900, "minutes": 60}, {"size": 500, "minutes": 100}]))   # bigger but shorter
        self.assertIsNone(suggest([{"size": 500, "minutes": 100}, {"size": 505, "minutes": 100}]))  # same size

    def test_duplicate_commands_and_move(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = Path(temp) / "Movies"
            for name, size in (("Heat (1995)", 900), ("Heat (1996)", 500)):
                (movies / name).mkdir(parents=True)
                (movies / name / "Heat.mp4").write_bytes(b"x" * size)
            plan = self.tool.build_dupe_plan(str(movies), "no-such-ffprobe")
            self.assertEqual(plan[0]["suggest"], 0)
            self.assertIn("<- suggested", self.tool.format_dupe_recap(plan))
            self.assertEqual(self.tool.parse_dupe_command("keep 1 z", plan)[0], "continue")
            self.assertIsNone(plan[0]["keep"])
            self.tool.parse_dupe_command("auto", plan)
            self.assertEqual(plan[0]["keep"], 0)
            move_root = Path(temp) / "Movies Needing Attention"
            args = type("A", (), {"dry_run": False, "yes": True})()
            self.tool.run_dupes(args, str(movies), "no-such-ffprobe", str(move_root))
            self.assertTrue((movies / "Heat (1995)" / "Heat.mp4").exists())
            self.assertFalse((movies / "Heat (1996)").exists())
            self.assertTrue((move_root / "Duplicates" / "Heat (1996)" / "Heat.mp4").exists())

    def test_dry_run_moves_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = Path(temp) / "Movies"
            for name, size in (("Heat (1995)", 900), ("Heat (1996)", 500)):
                (movies / name).mkdir(parents=True)
                (movies / name / "Heat.mp4").write_bytes(b"x" * size)
            args = type("A", (), {"dry_run": True, "yes": True})()
            self.tool.run_dupes(args, str(movies), "no-such-ffprobe", str(Path(temp) / "out"))
            self.assertTrue((movies / "Heat (1996)").exists())
            (movies / "Alien (1979).mp4").write_bytes(b"x")
            self.tool.run_loose(args, str(movies), "", "no-such-ffprobe", str(Path(temp) / "out"))
            self.assertTrue((movies / "Alien (1979).mp4").exists())

    def test_loose_file_is_compared_with_its_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = Path(temp)
            (movies / "Cleaner (2025)").mkdir()
            (movies / "Cleaner (2025)" / "Cleaner.mp4").write_bytes(b"x" * 3000)
            (movies / "Cleaner__2025_.mp4").write_bytes(b"x" * 1000)
            (movies / "Joker (2019)").mkdir()
            (movies / "Joker (2019)" / "Joker.mp4").write_bytes(b"x" * 1000)
            (movies / "Joker-Copy(1).mp4").write_bytes(b"x" * 3000)
            (movies / "Odd-Copy(1).mp4").write_bytes(b"x" * 10)
            plan = self.tool.build_loose_plan(str(movies), self.tool.loose_files(str(movies)), "", "no-such-ffprobe")
            rows = {r["file"]: r for r in plan}
            self.assertEqual(rows["Cleaner__2025_.mp4"]["compare"][0]["verdict"], "folder")
            self.assertEqual(rows["Joker-Copy(1).mp4"]["compare"][0]["folder"], "Joker (2019)")
            self.assertEqual(rows["Joker-Copy(1).mp4"]["compare"][0]["verdict"], "loose")
            self.assertEqual(rows["Odd-Copy(1).mp4"]["compare"], [])
            recap = self.tool.format_loose_recap(plan)
            self.assertIn('folder "Cleaner (2025)"', recap)
            self.assertIn("the folder copy is better, so out N is safe", recap)
            self.assertIn("the loose file is the larger one", recap)
            self.assertIn("no folder with this title found", recap)
            self.assertEqual(self.tool.compare_note({"size": 500, "minutes": 100}, {"size": 505, "minutes": 100}), "unsure")


class StreamEndReasonTests(unittest.TestCase):
    """Why a capture stopped early, read from FFmpeg's log."""

    def _reason(self, text):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.ffmpeg.log"
            path.write_text(text, encoding="utf-8")
            return recording_agent.ffmpeg_end_reason(str(path))

    def _progress(self, media, elapsed, frame=100):
        return (f"frame={frame} fps= 31 q=-1.0 size= 1700KiB time={media} bitrate=6821kbits/s "
                f"speed=1.03x elapsed={elapsed}\n")

    def test_a_refused_connection_is_named(self):
        text = (self._progress("00:10:00.00", "0:09:50.00")
                + "[http @ 0x1] HTTP error 403 Forbidden\n"
                  "[in#0 @ 0x2] Error during demuxing: Server returned 403 Forbidden (access denied)\n")
        reason = self._reason(text)
        self.assertIn("403", reason)
        self.assertTrue(reason.startswith('FFmpeg reported "'))

    def test_the_stream_address_is_never_included(self):
        text = "[http @ 0x1] Will reconnect at 123 in 0 second(s), error=Connection reset by peer http://host:826/live/user/secret/1.ts\n"
        reason = self._reason(text)
        self.assertNotIn("secret", reason)
        self.assertNotIn("http://", reason)

    def test_a_stream_that_just_ends_is_said_to_end_with_no_error(self):
        text = self._progress("00:34:11.00", "0:33:09.00") + self._progress("00:34:25.00", "0:33:39.00")
        self.assertEqual(self._reason(text), "the provider ended the stream with no error")

    def test_a_pause_in_the_video_is_reported(self):
        text = "".join(self._progress("00:34:11.93", f"0:33:{s:02d}.00") for s in range(9, 21))
        text += self._progress("00:34:25.00", "0:33:39.00")
        reason = self._reason(text)
        self.assertIn("paused up to 11 s", reason)
        short = "".join(self._progress("00:34:11.93", f"0:33:0{s}.00") for s in range(1, 4))
        self.assertNotIn("paused", self._reason(short))

    def test_no_log_gives_no_reason(self):
        self.assertEqual(recording_agent.ffmpeg_end_reason("/no/such/file.log"), "")
        self.assertEqual(self._reason("   \n"), "")
