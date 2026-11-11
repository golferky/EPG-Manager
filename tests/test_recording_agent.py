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

    def test_a_folder_with_no_year_still_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._library(temp, "Employee of the Month")
            self.assertEqual(len(recording_agent.find_plex_candidates(root, "Employee of the Month", "2004")), 1)

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
