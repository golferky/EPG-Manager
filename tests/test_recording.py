import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock


TEST_DATA_DIR = tempfile.mkdtemp(prefix="epg-manager-tests-")
os.environ["EPG_BASE_DIR"] = TEST_DATA_DIR

import server  # noqa: E402


class RecordingTests(unittest.TestCase):
    def setUp(self):
        with server._rec_lock:
            server._recs.clear()
            server._rec_cancel_events.clear()
        server._init_recordings_table()
        self.db_path = server._guide_db_path()
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM recordings")
        conn.commit()
        conn.close()

    def test_verbose_scheduled_status_is_active(self):
        rec = {"status": "scheduled (12m away)"}
        self.assertEqual(server._rec_status_base(rec["status"]), "scheduled")
        self.assertTrue(server._rec_is_active(rec))

    def test_premium_channel_classifier(self):
        self.assertTrue(server._is_premium_channel("HBO Movies"))
        self.assertTrue(server._is_premium_channel("MGM+ Hits HD"))
        self.assertTrue(server._is_premium_channel("Showtime Extreme"))
        self.assertTrue(server._is_premium_channel("Sky Cinema Greats"))
        self.assertFalse(server._is_premium_channel("ANTENNA TV"))

    def test_non_english_channel_classifier(self):
        self.assertTrue(server._is_foreign_recording_feed("HBO Latino HD"))
        self.assertTrue(server._is_foreign_recording_feed("CINE ESPAÑOL"))
        self.assertTrue(server._is_foreign_recording_feed("TV5 French"))
        self.assertTrue(server._is_foreign_recording_feed("Arabic Movies"))
        self.assertFalse(server._is_foreign_recording_feed("A&E Canada HD"))
        self.assertFalse(server._is_foreign_recording_feed("HBO Drama"))

    def test_sd_duplicate_channel_helpers(self):
        self.assertTrue(server._is_sd_channel_name("A&E (SD)"))
        self.assertFalse(server._is_sd_channel_name("A&E HD"))
        self.assertEqual(server._sd_duplicate_channel_key("A&E (SD)"),
                         server._sd_duplicate_channel_key("A&E"))

    def test_commercial_report_parser_reads_frame_ranges(self):
        with tempfile.TemporaryDirectory() as temp:
            report = os.path.join(temp, "review.txt")
            with open(report, "w") as handle:
                handle.write("FILE PROCESSING COMPLETE  1000 FRAMES AT  2997\n---\n2997\t5994\n")
            self.assertEqual(server._commercial_breaks_from_report(report, 29.97), [
                {'start': 100.0, 'end': 200.0, 'duration': 100.0},
            ])

    def test_commercial_copy_keeps_parts_outside_proposed_breaks(self):
        self.assertEqual(server._commercial_keep_segments(100, [
            {'start': 10, 'end': 20}, {'start': 60, 'end': 70},
        ]), [(0.0, 10.0), (20.0, 60.0), (70.0, 100)])

    def test_agent_transfer_status_is_active(self):
        self.assertTrue(server._rec_is_active({"status": "awaiting_transfer"}))

    def test_unsuffixed_stream_maps_to_hd_guide_sibling(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            movies_db = os.path.join(temp, "movies.db")
            server.ensure_guide_db(guide_db)
            guide = sqlite3.connect(guide_db)
            guide.executemany(
                """INSERT INTO guide
                   (title,channel_id,channel_name,start_utc,end_utc)
                   VALUES (?,?,?,?,?)""",
                [
                    ("F/X", "mgmhits.us", "MGM+ HITS", "20260812145500", "20260812164500"),
                    ("F/X", "67929", "MGM+ Hits HD", "20260812145500", "20260812164500"),
                ],
            )
            guide.commit()
            guide.close()
            movies = sqlite3.connect(movies_db)
            movies.execute(
                "CREATE TABLE channels (guide_channel TEXT, stream_id TEXT)"
            )
            movies.execute(
                "INSERT INTO channels VALUES (?,?)", ("mgmhits.us", "98755")
            )
            movies.commit()
            movies.close()

            mapped = server.get_ps_channel_ids(guide_db, movies_db)
            self.assertIn("mgmhits.us", mapped)
            self.assertIn("67929", mapped)

    def test_plex_episode_index_uses_show_season_episode_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            episode = os.path.join(
                temp, "Dutton Ranch", "Season 01",
                "Dutton Ranch - S01E02 - Earn Another Day.mp4",
            )
            os.makedirs(os.path.dirname(episode))
            open(episode, "wb").close()
            server._plex_episode_cache.update({"root": "", "loaded_at": 0, "episodes": set()})
            episodes = server._plex_episode_keys(temp)
            self.assertIn("duttonranch|1|2", episodes)

    def test_wanted_plex_index_includes_movies_and_tv_shows(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = os.path.join(temp, "Movies")
            shows = os.path.join(temp, "TV Shows")
            os.makedirs(os.path.join(movies, "F X (1986)"))
            os.makedirs(os.path.join(shows, "Bewitched"))
            server._plex_title_cache.update({
                "roots": (), "loaded_at": 0, "movies": set(), "shows": set(),
                "movie_versions": set(), "unyearred_movies": set(),
            })
            with mock.patch.object(server, "load_config", return_value={
                "plex_path": movies, "plex_tv_path": shows,
            }):
                titles = server._plex_wanted_title_index()
            self.assertIn("fx", titles["movies"])
            self.assertIn(("fx", "1986"), titles["movie_versions"])
            self.assertIn("bewitched", titles["shows"])

    def test_plex_info_matches_movie_folder_when_guide_title_has_year(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = os.path.join(temp, "Ace Ventura Pet Detective (1994)")
            os.makedirs(folder)
            open(os.path.join(folder, "Ace Ventura Pet Detective.mp4"), "wb").close()
            server._plex_info_cache.clear()
            with mock.patch.object(server, "load_config", return_value={"plex_path": temp}):
                response = server.app.test_client().get(
                    "/epg-web/api/plex/info?title=Ace%20Ventura%20Pet%20Detective%20(1994)"
                )
            self.assertTrue(response.get_json()["found"])

    def test_plex_info_tolerates_unyearred_folder_and_extra_spaces(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = os.path.join(temp, "Alvin and the Chipmunks  Chipwrecked")
            os.makedirs(folder)
            open(os.path.join(folder, "movie.mp4"), "wb").close()
            server._plex_info_cache.clear()
            with mock.patch.object(server, "load_config", return_value={"plex_path": temp}):
                response = server.app.test_client().get(
                    "/epg-web/api/plex/info?title=Alvin%20and%20the%20Chipmunks%3A%20Chipwrecked%20(2011)"
                )
            self.assertTrue(response.get_json()["found"])

    def test_stream_info_returns_safe_incoming_quality(self):
        server._stream_info_cache.clear()
        media = {
            "width": 1920, "height": 1080, "fps": 29.97,
            "video_codec": "h264", "audio_codec": "aac", "audio_channels": 2,
            "total_bitrate": 5500000, "video_bitrate": 0,
        }
        with mock.patch.object(server, "_stream_url", return_value=("https://private.example/live.ts", None, {})), \
             mock.patch("recording_agent.probe_media", return_value=media):
            response = server.app.test_client().get("/epg-web/api/stream-info?channel_id=test-channel")
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["width"], 1920)
        self.assertEqual(data["fps"], 29.97)
        self.assertEqual(data["video_codec"], "H264")
        self.assertNotIn("private", str(data))

    def test_airings_merge_hd_duplicate_and_identify_movie(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            server.ensure_guide_db(guide_db)
            conn = sqlite3.connect(guide_db)
            for column, typedef in (
                ("episode_title", "TEXT"), ("season_num", "INTEGER"),
                ("episode_num", "INTEGER"), ("prog_type", "TEXT"),
            ):
                conn.execute(f"ALTER TABLE guide ADD COLUMN {column} {typedef}")
            conn.executemany(
                """INSERT INTO guide
                   (title,channel_id,channel_name,start_utc,end_utc,prog_type)
                   VALUES (?,?,?,?,?,?)""",
                [
                    ("F/X", "mgmhits.us", "MGM+ HITS", "20990812145500", "20990812164500", ""),
                    ("F/X", "67929", "MGM+ Hits HD", "20990812145500", "20990812164500", "MV"),
                ],
            )
            conn.commit()
            conn.close()
            with mock.patch.object(server, "load_config", return_value={
                "guide_db_path": guide_db, "timezone": "America/New_York"
            }), mock.patch.object(server, "_stream_url", return_value=(
                "private-url", None,
                {"matched_guide_channel": "mgmhits.us"},
            )):
                response = server.app.test_client().get(
                    "/epg-web/api/airings?title=F%2FX"
                )
            data = response.get_json()
            self.assertEqual(data["prog_type"], "MV")
            self.assertFalse(data["is_series"])
            self.assertEqual(len(data["airings"]), 1)
            self.assertEqual(data["airings"][0]["channel_name"], "MGM+ Hits HD")
            self.assertTrue(data["airings"][0]["can_record"])
            self.assertTrue(data["airings"][0]["commercial_free"])

    def test_airings_inherit_episode_data_from_matching_sd_listing(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            server.ensure_guide_db(guide_db)
            conn = sqlite3.connect(guide_db)
            for column, typedef in (
                ("episode_title", "TEXT"), ("season_num", "INTEGER"),
                ("episode_num", "INTEGER"), ("prog_type", "TEXT"),
            ):
                conn.execute(f"ALTER TABLE guide ADD COLUMN {column} {typedef}")
            conn.executemany(
                """INSERT INTO guide
                   (title,channel_id,channel_name,start_utc,end_utc,episode_title,
                    season_num,episode_num,prog_type)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    ("Bewitched", "fetv.us", "FAMILY ENTERTAINMENT TELEVISION",
                     "20990812165000", "20990812172500", "", None, None, ""),
                    ("Bewitched", "93195", "Family Entertainment Television",
                     "20990812165000", "20990812172500",
                     "The Short Happy Circuit of Aunt Clara", 3, 9, "EP"),
                ],
            )
            conn.commit()
            conn.close()
            with mock.patch.object(server, "load_config", return_value={
                "guide_db_path": guide_db, "timezone": "America/New_York"
            }), mock.patch.object(server, "_stream_url", return_value=(
                "private-url", None, {"matched_guide_channel": "fetv.us"}
            )):
                response = server.app.test_client().get("/epg-web/api/airings?title=Bewitched")
            data = response.get_json()
            self.assertTrue(data["is_series"])
            self.assertEqual(data["airings"][0]["season_num"], 3)
            self.assertEqual(data["airings"][0]["episode_num"], 9)

    def test_movie_ui_hides_recurring_batch_action(self):
        self.assertIn("batchBtn.style.display = isSeries ? '' : 'none';", server.HTML)
        self.assertNotIn("Record Series", server.HTML)

    def test_wanted_ui_allows_movie_and_series_choices(self):
        self.assertIn("addWanted('movie')", server.HTML)
        self.assertIn("addWanted('series')", server.HTML)

    def test_wanted_ui_separates_movies_and_series(self):
        self.assertIn("['MOVIES', recs.filter", server.HTML)
        self.assertIn("['MOVIES — IN PLEX', recs.filter", server.HTML)
        self.assertIn("['SERIES — EPISODE TRACKING', recs.filter", server.HTML)

    def test_wanted_movies_can_check_for_a_better_copy(self):
        self.assertIn("checkWantedMovieUpgrade", server.HTML)
        self.assertIn("/epg-web/api/recommendations/movie-upgrade", server.HTML)

    def test_showtime_rebrand_channel_aliases(self):
        self.assertEqual(
            server._channel_match_base("Paramount+ with Showtime HD"),
            server._channel_match_base("showtime.us"),
        )
        self.assertEqual(
            server._channel_match_base("Paramount+ with Showtime HD (Pacific)"),
            server._channel_match_base("showtimewest.us"),
        )
        self.assertEqual(
            server._channel_match_base("SHO 2 HD"),
            server._channel_match_base("showtime2.us"),
        )

    def test_airings_ui_lists_playable_rows_and_uses_series_flag(self):
        self.assertIn("const isSeries = !!ar.is_series;", server.HTML)
        # A live usable stream remains playable even when it is not eligible
        # for scheduled recording (for example, a commercial-supported feed).
        self.assertIn("window._allAirings = ar.airings.filter(a => a.can_record || (a.can_play && a.on_now));", server.HTML)

    def test_active_in_memory_recording_is_not_marked_stale(self):
        self.assertIn(
            "const isStale   = !r._mem && s === 'recording' && isPast;",
            server.HTML,
        )

    @mock.patch.object(server, "_db_update_rec_status", return_value=True)
    def test_scheduled_recording_can_be_cancelled(self, update_status):
        rec_id = "test1234"
        event = threading.Event()
        with server._rec_lock:
            server._recs[rec_id] = {
                "title": "Test",
                "status": "scheduled (5m away)",
                "pid": None,
                "file": None,
                "log": [],
            }
            server._rec_cancel_events[rec_id] = event

        response = server.app.test_client().post(
            "/epg-web/api/record/cancel", json={"id": rec_id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["cancelled"])
        self.assertTrue(event.is_set())
        self.assertEqual(server._recs[rec_id]["status"], "cancelled")
        update_status.assert_called_once()

    @mock.patch.object(server, "_db_update_rec_status", return_value=True)
    def test_cancel_wakes_waiting_recording_thread(self, _update_status):
        rec_id = "waiting1"
        with server._rec_lock:
            server._recs[rec_id] = {
                "title": "Waiting Test",
                "channel_id": "test.channel",
                "channel": "Test Channel",
                "start_ts": time.time() + 30,
                "stop_ts": time.time() + 90,
                "status": "queued",
                "progress": 0,
                "log": [],
                "pid": None,
                "file": None,
            }
            server._rec_cancel_events[rec_id] = threading.Event()

        worker = threading.Thread(target=server._run_recording, args=(rec_id,))
        worker.start()
        time.sleep(0.05)
        response = server.app.test_client().post(
            "/epg-web/api/record/cancel", json={"id": rec_id}
        )
        worker.join(timeout=1)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(worker.is_alive())
        self.assertEqual(server._recs[rec_id]["status"], "cancelled")

    def test_stale_recordings_are_reconciled(self):
        db_path = server._guide_db_path()
        server._init_recordings_table()
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM recordings")
        past = time.time() - 3600
        rows = [
            ("oldrec", "Old Recording", "recording"),
            ("oldqueue", "Old Queue", "queued"),
        ]
        for rec_id, title, status in rows:
            conn.execute(
                """INSERT INTO recordings
                   (rec_id,title,channel,channel_id,start_ts,stop_ts,status)
                   VALUES (?,?,?,?,?,?,?)""",
                (rec_id, title, "Test", "test", past - 3600, past, status),
            )
        conn.execute(
            """INSERT INTO recordings
               (rec_id,title,channel,channel_id,start_ts,stop_ts,status)
               VALUES (?,?,?,?,?,?,?)""",
            ("resume", "Resume Recording", "Test", "test", past, time.time() + 3600, "recording"),
        )
        conn.commit()
        conn.close()

        server._reconcile_stale_recordings()

        conn = sqlite3.connect(db_path)
        statuses = dict(conn.execute("SELECT rec_id,status FROM recordings"))
        conn.close()
        self.assertEqual(statuses["oldrec"], "failed")
        self.assertEqual(statuses["oldqueue"], "skipped_too_short")
        self.assertEqual(statuses["resume"], "queued")

    @mock.patch.object(server, "load_config")
    def test_agent_claim_and_heartbeat(self, load_config):
        load_config.return_value = {
            "guide_db_path": self.db_path,
            "recording_backend": "agent",
            "recording_agent_token": "test-secret",
        }
        rec = {
            "title": "Agent Test", "channel": "Test Channel",
            "channel_id": "test.channel", "stream_id": "12345",
            "start_ts": time.time() + 60, "stop_ts": time.time() + 3600,
            "status": "queued", "backend": "agent", "file": "",
        }
        self.assertTrue(server._db_upsert_rec("agentjob", rec))
        client = server.app.test_client()
        headers = {"Authorization": "Bearer test-secret"}

        claim = client.post("/epg-web/api/agent/jobs/claim", headers=headers, json={
            "agent_id": "mac-test", "claim_ahead_seconds": 300,
            "lease_seconds": 90,
        })
        self.assertEqual(claim.status_code, 200)
        job = claim.get_json()["job"]
        self.assertEqual(job["id"], "agentjob")
        self.assertEqual(job["stream_id"], "12345")

        heartbeat = client.post(
            "/epg-web/api/agent/jobs/agentjob/heartbeat",
            headers=headers,
            json={"agent_id": "mac-test", "status": "recording", "progress": 12},
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.assertFalse(heartbeat.get_json()["cancel_requested"])
        conn = sqlite3.connect(server._guide_db_path())
        status = conn.execute(
            "SELECT status FROM recordings WHERE rec_id='agentjob'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "recording")

        cancel = client.post(
            "/epg-web/api/record/cancel", json={"id": "agentjob"}
        )
        self.assertEqual(cancel.status_code, 200)
        heartbeat = client.post(
            "/epg-web/api/agent/jobs/agentjob/heartbeat",
            headers=headers,
            json={"agent_id": "mac-test", "status": "recording"},
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.assertTrue(heartbeat.get_json()["cancel_requested"])

    @mock.patch.object(server, "load_config")
    def test_agent_api_rejects_bad_token(self, load_config):
        load_config.return_value = {
            "recording_backend": "agent", "recording_agent_token": "test-secret"
        }
        response = server.app.test_client().get(
            "/epg-web/api/agent/health",
            headers={"Authorization": "Bearer wrong-secret"},
        )
        self.assertEqual(response.status_code, 401)

    @mock.patch.object(server, "load_config")
    def test_config_api_does_not_return_secrets(self, load_config):
        load_config.return_value = {
            "guide_path": "/guide.xml", "epg_pass": "provider-secret",
            "sd_pass": "schedule-secret", "omdb_key": "movie-secret",
            "tmdb_key": "other-secret", "recording_agent_token": "agent-secret",
        }
        data = server.app.test_client().get("/epg-web/api/config").get_json()
        self.assertEqual(data["guide_path"], "/guide.xml")
        self.assertNotIn("epg_pass", data)
        self.assertNotIn("recording_agent_token", data)
        self.assertTrue(data["secrets_configured"]["recording_agent_token"])

    @mock.patch.object(server, "save_config")
    @mock.patch.object(server, "load_config")
    def test_config_partial_update_preserves_secrets(self, load_config, save_config):
        load_config.return_value = {
            "guide_path": "/old.xml", "epg_pass": "provider-secret",
            "recording_agent_token": "agent-secret",
        }
        response = server.app.test_client().post(
            "/epg-web/api/config", json={"guide_path": "/new.xml"}
        )
        self.assertEqual(response.status_code, 200)
        saved = save_config.call_args.args[0]
        self.assertEqual(saved["guide_path"], "/new.xml")
        self.assertEqual(saved["epg_pass"], "provider-secret")
        self.assertEqual(saved["recording_agent_token"], "agent-secret")


if __name__ == "__main__":
    unittest.main()
