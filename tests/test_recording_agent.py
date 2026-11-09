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

        def fake_run_process(api, job, status, cmd, cfg, log_path, file_path=""):
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
