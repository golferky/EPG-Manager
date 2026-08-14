import tempfile
import unittest
from pathlib import Path
from unittest import mock

import recording_agent


class QualityDecisionTests(unittest.TestCase):
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
