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
        self.assertTrue(server._is_commercial_free_channel("Paramount+ with Showtime HD"))
        self.assertFalse(server._is_commercial_free_channel("Paramount Network HD"))
        self.assertTrue(server._is_premium_channel("Sky Cinema Greats"))
        self.assertFalse(server._is_premium_channel("ANTENNA TV"))

    def test_non_english_channel_classifier(self):
        self.assertTrue(server._is_foreign_recording_feed("HBO Latino HD"))
        self.assertTrue(server._is_foreign_recording_feed("CINE ESPAÑOL"))
        self.assertTrue(server._is_foreign_recording_feed("TV5 French"))
        self.assertTrue(server._is_foreign_recording_feed("Arabic Movies"))
        self.assertFalse(server._is_foreign_recording_feed("A&E Canada HD"))
        self.assertFalse(server._is_foreign_recording_feed("HBO Drama"))

    def test_quarantined_eaglecast_stream_classifier(self):
        self.assertTrue(server._is_quarantined_eaglecast_stream(
            ("45503", "US| SHOWTIME HD", "ts")))
        self.assertFalse(server._is_quarantined_eaglecast_stream(
            ("92020", "US| SHOWTIME WEST HD", "ts")))

    def test_provider_authority_allows_verified_rebrand_but_rejects_wrong_feed(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            server.ensure_guide_db(guide_db)
            conn = sqlite3.connect(guide_db)
            conn.execute('''INSERT INTO provider_stream_authority
                         (provider,stream_id,epg_channel_id,provider_name,checked_at)
                         VALUES ('primestreams','89057','5starmax.us','CINEMAX CLASSICS',0)''')
            conn.commit(); conn.close()
            with mock.patch.object(server, '_guide_db_path', return_value=guide_db):
                server._invalidate_provider_stream_authority_cache()
                self.assertTrue(server._primestream_authoritative_channel(
                    'cinemaxclassics.us', '89057'))
                self.assertTrue(server._primestream_authoritative_channel(
                    '5starmax.us', '89057'))
                self.assertFalse(server._authority_id_matches(
                    'paramountwithshowtime.us', 'showtime.us', 'SHOWTIME'))

    def test_program_modal_offers_plex_play_without_waiting_for_probe(self):
        self.assertIn('pm-plex-play-btn', server.HTML)
        self.assertIn("document.getElementById('pm-plex-info').textContent = 'Plex copy found'", server.HTML)

    def test_weekly_recording_rule_uses_channel_and_weekly_time(self):
        with open(server.__file__) as handle:
            source = handle.read()
        self.assertIn('CREATE TABLE IF NOT EXISTS weekly_recordings', source)
        self.assertIn('recordWeekly()', server.HTML)
        self.assertIn('/epg-web/api/record/weekly', server.HTML)

    def test_provider_refresh_recognizes_xmltv_programme_tags(self):
        with open(server.__file__) as handle:
            source = handle.read()
        self.assertIn("br'<programme\\b[^>]*\\bstart=\"(\\d{14})'", source)
        self.assertNotIn("br'<programme\\\\b[^>]*\\\\bstart=\"(\\\\d{14})'", source)
        self.assertIn('attempt {attempt}/3', source)

    def test_same_title_metadata_prefers_named_people_over_generic_plot_words(self):
        with open(server.__file__) as handle:
            source = handle.read()
        self.assertIn('score = (actor_score > 0, actor_score, plot_score, runtime_score)', source)

    def test_detail_panel_keeps_record_action_when_source_lookup_is_slow(self):
        self.assertIn('directFallback: true', server.HTML)
        self.assertIn('const keepImmediateAction = !!_nextAiring;', server.HTML)

    def test_quality_duplicate_channel_helpers(self):
        self.assertTrue(server._is_quality_variant_channel_name("A&E (SD)"))
        self.assertTrue(server._is_quality_variant_channel_name("A&E HD"))
        self.assertFalse(server._is_quality_variant_channel_name("A&E West"))
        self.assertEqual(server._quality_duplicate_channel_key("HBO Comedy HD"),
                         server._quality_duplicate_channel_key("HBO Comedy"))
        self.assertNotEqual(server._quality_duplicate_channel_key("HBO West"),
                            server._quality_duplicate_channel_key("HBO"))

    def test_metadata_requires_exact_title(self):
        self.assertTrue(server._metadata_title_matches("Wolves", "Wolves"))
        self.assertTrue(server._metadata_title_matches("Spider-Man: Homecoming",
                                                        "Spider Man Homecoming"))
        self.assertFalse(server._metadata_title_matches("Wolves", "Dances with Wolves"))
        self.assertFalse(server._metadata_title_matches("Gravity", "Gravity Falls"))
        self.assertEqual(server._metadata_runtime_minutes("127 min"), 127)
        self.assertEqual(server._metadata_runtime_minutes("N/A"), 0)
        self.assertTrue(server._metadata_runtime_is_plausible(130, "127 min"))
        self.assertFalse(server._metadata_runtime_is_plausible(130, "10 min"))

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

    def test_sd_station_identity_beats_ambiguous_display_name(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            movies_db = os.path.join(temp, "movies.db")
            server.ensure_guide_db(guide_db)
            guide = sqlite3.connect(guide_db)
            guide.executemany(
                "INSERT INTO guide_channels(channel_id,channel_name) VALUES (?,?)",
                [("10241", "HBO Hits"), ("10242", "HBO Hits")],
            )
            guide.commit()
            guide.close()
            movies = sqlite3.connect(movies_db)
            movies.execute("""CREATE TABLE channels (
                guide_channel TEXT, stream_id TEXT, is_bad INTEGER,
                sd_station_id TEXT
            )""")
            movies.execute(
                "INSERT INTO channels VALUES (?,?,?,?)",
                ("hbo2.us", "95926", 0, "sd.10241"),
            )
            movies.commit()
            movies.close()

            server._ps_channel_cache.update({"paths": (), "loaded_at": 0, "ids": set()})
            mapped = server.get_ps_channel_ids(guide_db, movies_db)
            self.assertIn("10241", mapped)
            self.assertNotIn("10242", mapped)

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

    def test_wanted_titles_require_imdb_choice_before_save(self):
        self.assertIn('/epg-web/api/wanted/movie-matches', server.HTML)
        self.assertIn('showWantedTitlePicker', server.HTML)
        self.assertIn("type=${kind}", server.HTML)
        self.assertIn('imdb_id:movie.imdb_id', server.HTML)
        self.assertIn("type = window._wantedTitleType === 'series' ? 'series' : 'movie'", server.HTML)

    def test_series_scheduler_ignores_non_recording_runtime_entries(self):
        # Playback/UI state can share the in-memory registry but does not have
        # the channel/start pair used for recording deduplication.
        with open(server.__file__) as handle:
            source = handle.read()
        self.assertIn("r.get('channel_id') is not None", source)
        self.assertIn("r.get('start_ts') is not None", source)

    def test_schedule_has_text_filter(self):
        self.assertIn('id="sched-search"', server.HTML)
        self.assertIn('function filterSchedule()', server.HTML)

    def test_recording_health_uses_compact_rows_with_rerecord_action(self):
        self.assertIn('<th>Recording</th><th>Channel</th><th>When</th><th>Result</th><th>Actions</th>', server.HTML)
        self.assertIn('↻ Find re-record', server.HTML)

    def test_wanted_ui_separates_movies_and_series(self):
        self.assertIn("['MOVIES', recs.filter", server.HTML)
        self.assertIn("['MOVIES — IN PLEX', recs.filter", server.HTML)
        self.assertIn("['SERIES — EPISODE TRACKING', recs.filter", server.HTML)

    def test_wanted_movies_can_check_for_a_better_copy(self):
        self.assertIn("checkWantedMovieUpgrade", server.HTML)
        self.assertIn("/epg-web/api/recommendations/movie-upgrade", server.HTML)

    def test_plex_series_missing_episodes_show_guide_airings(self):
        self.assertIn('_add_missing_episode_guide_airings', server.__dict__)
        self.assertIn('Next guide airing:', server.HTML)
        self.assertIn('Queued to record', server.HTML)

    def test_plex_series_catalog_lookup_is_bounded_and_concurrent(self):
        with open(server.__file__) as handle:
            source = handle.read()
        self.assertIn("urlreq.urlopen(search_url, timeout=2)", source)
        self.assertIn("ThreadPoolExecutor(max_workers=min(6, len(shows))", source)

    def test_plex_series_scan_reports_progress(self):
        self.assertIn('/epg-web/api/plex/series-scan', server.__dict__['app'].url_map.__str__())
        self.assertIn('plex-series-progress-bar', server.HTML)
        self.assertIn('pollPlexSeriesScan()', server.HTML)

    def test_paramount_showtime_is_not_aliased_to_showtime(self):
        self.assertNotEqual(
            server._channel_match_base("Paramount+ with Showtime HD"),
            server._channel_match_base("showtime.us"),
        )
        self.assertNotEqual(
            server._channel_match_base("Paramount+ with Showtime HD (Pacific)"),
            server._channel_match_base("showtimewest.us"),
        )
        self.assertEqual(
            server._channel_match_base("SHO 2 HD"),
            server._channel_match_base("showtime2.us"),
        )

    def test_verified_legacy_premium_channel_ids_are_normalized(self):
        # These exact old provider IDs were checked against the provider's
        # authoritative XMLTV IDs, not inferred from a display-label match.
        self.assertEqual(server._channel_match_base("Cinemax Action"), "actionmax")
        self.assertEqual(server._channel_match_base("Cinemax Hits"), "moremax")
        self.assertEqual(server._channel_match_base("HBO Drama"), "hbosignature")
        self.assertEqual(server._channel_match_base("HBO Movies"), "hbozone")

    def test_live_play_button_survives_optional_airings_lookup(self):
        self.assertIn("const keepImmediateAction = !!_nextAiring;", server.HTML)
        self.assertIn("if (!keepImmediateAction) document.getElementById('pm-next-wrap').style.display = 'none';", server.HTML)

    def test_sports_recordings_have_planned_overtime_protection(self):
        self.assertEqual(server.SPORTS_OVERTIME_CUSHION_SECONDS, 90 * 60)
        self.assertTrue(server._is_sports_listing('Football', ''))
        self.assertTrue(server._is_sports_listing('', 'Cincinnati Bengals at Baltimore Ravens'))
        self.assertFalse(server._is_sports_listing('Movie', ''))

    def test_schedule_titles_open_the_exact_programme_details(self):
        self.assertIn("/epg-web/api/schedule/programme", server.HTML)
        self.assertIn("openScheduledRecording(window._scheduleRows", server.HTML)
        with open(server.__file__, encoding="utf-8") as source:
            self.assertIn("def api_scheduled_programme():", source.read())

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

    def test_weekly_recording_control_is_not_offered_for_movies(self):
        """Movies must never expose a weekly recording action in the modal."""
        self.assertIn('id="pm-weekly-btn"', server.HTML)
        self.assertIn('style="display:none;border-color:#f59e0b', server.HTML)
        self.assertIn("p.prog_type === 'EP'", server.HTML)
        self.assertIn("p.prog_type === 'SH'", server.HTML)
        self.assertIn("weeklyBtn.style.display = (sportsEvent || isSeries) ? '' : 'none';", server.HTML)

    def test_airings_lookup_cannot_hide_existing_record_action(self):
        """A valid clicked guide row remains recordable if lookup is slow."""
        self.assertIn('const keepImmediateAction = !!_nextAiring;', server.HTML)

    def test_recording_honors_the_source_selected_for_an_airing(self):
        """A PrimeStreams airing must not be silently changed to Eaglecast."""
        self.assertIn("stream_provider: airing.stream_provider || ''", server.HTML)
        with open(server.__file__, encoding='utf-8') as source:
            text = source.read()
        self.assertIn("preferred_provider=requested_provider", text)
        self.assertIn("if preferred_provider == 'primestreams':", text)

    def test_plot_setting_year_is_not_used_as_a_movie_release_year(self):
        with open(server.__file__, encoding='utf-8') as source:
            text = source.read()
        self.assertIn("A bare year in a\n    # plot is often part of the story", text)
        self.assertIn("(?:this|from|released", text)


if __name__ == "__main__":
    unittest.main()


class SlotLimitTests(unittest.TestCase):
    @staticmethod
    def _rec(title, start, stop, status="queued"):
        return {"title": title, "start_ts": start, "stop_ts": stop, "status": status}

    def test_room_available_returns_no_conflicts(self):
        recs = [self._rec(f"r{i}", 0, 100) for i in range(5)]
        self.assertEqual(server._recording_slot_conflicts(recs, 10, 50, 6), [])

    def test_full_slots_return_the_overlapping_jobs(self):
        recs = [self._rec(f"r{i}", 0, 100) for i in range(6)]
        self.assertEqual(len(server._recording_slot_conflicts(recs, 10, 50, 6)), 6)

    def test_jobs_that_do_not_overlap_are_ignored(self):
        recs = [self._rec(f"r{i}", 0, 100) for i in range(6)]
        self.assertEqual(server._recording_slot_conflicts(recs, 100, 200, 6), [])

    def test_peak_inside_window_counts_even_if_start_is_free(self):
        recs = [self._rec(f"r{i}", 50, 150) for i in range(6)]
        self.assertEqual(len(server._recording_slot_conflicts(recs, 0, 100, 6)), 6)

    def test_finished_jobs_do_not_count(self):
        recs = [self._rec(f"r{i}", 0, 100, status="done") for i in range(6)]
        self.assertEqual(server._recording_slot_conflicts(recs, 10, 50, 6), [])


class PlexStreamTitleTests(unittest.TestCase):
    def test_stream_finds_movie_when_title_includes_year(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "Indiana Jones and the Temple of Doom (1984)")
            os.makedirs(folder)
            with open(os.path.join(folder, "movie.mp4"), "wb") as fh:
                fh.write(b"0" * 64)
            cfg = {"plex_path": root}
            with mock.patch.object(server, "load_config", return_value=cfg):
                client = server.app.test_client()
                for title in ("Indiana Jones and the Temple of Doom (1984)",
                              "Indiana Jones and the Temple of Doom"):
                    resp = client.get("/epg-web/api/plex/stream", query_string={"title": title})
                    self.assertEqual(resp.status_code, 200, title)
                    resp.close()


class DuplicateRecordingTests(unittest.TestCase):
    @staticmethod
    def _rec(title, start, channel_id, stream_id="1402", status="queued", provider="primestreams"):
        return {"title": title, "start_ts": start, "stop_ts": start + 3600, "status": status,
                "channel_id": channel_id, "stream_id": stream_id, "stream_provider": provider}

    def test_same_title_and_start_on_another_guide_channel_is_a_duplicate(self):
        recs = {"a": self._rec("NFL GameDay Final", 1000, "34710")}
        self.assertEqual(server._find_duplicate_recording(
            recs.items(), "NFL GameDay Final", 1000, "45399"), "a")

    def test_same_provider_stream_and_start_is_a_duplicate(self):
        recs = {"a": self._rec("NFL GameDay Final", 1000, "34710")}
        self.assertEqual(server._find_duplicate_recording(
            recs.items(), "Another Title", 1002, "45399", "primestreams", "1402"), "a")

    def test_same_stream_id_on_a_different_provider_is_not_a_duplicate(self):
        recs = {"a": self._rec("Show One", 1000, "1", provider="eaglecast")}
        self.assertIsNone(server._find_duplicate_recording(
            recs.items(), "Show Two", 1000, "2", "primestreams", "1402"))

    def test_different_start_time_is_not_a_duplicate(self):
        recs = {"a": self._rec("NFL GameDay Final", 1000, "34710")}
        self.assertIsNone(server._find_duplicate_recording(
            recs.items(), "NFL GameDay Final", 5000, "34710", "primestreams", "1402"))

    def test_finished_or_cancelled_jobs_are_ignored(self):
        recs = {"a": self._rec("NFL GameDay Final", 1000, "34710", status="cancelled")}
        self.assertIsNone(server._find_duplicate_recording(
            recs.items(), "NFL GameDay Final", 1000, "34710", "primestreams", "1402"))


class PlaylistRouteTests(unittest.TestCase):
    def test_playlist_is_a_downloadable_m3u_with_the_stream_address(self):
        url = "http://provider.test/live/u/p/1.ts"
        with mock.patch.object(server, "_stream_url", return_value=(url, None, {})):
            resp = server.app.test_client().get(
                "/epg-web/api/play/playlist.m3u",
                query_string={"channel_id": "18086", "title": "Paycheck\nextra"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        body = resp.get_data(as_text=True)
        self.assertEqual(body.splitlines(), ["#EXTM3U", "#EXTINF:-1,Paycheck extra", url])

    def test_playlist_reports_a_stream_error(self):
        with mock.patch.object(server, "_stream_url", return_value=(None, "no stream", {})):
            resp = server.app.test_client().get(
                "/epg-web/api/play/playlist.m3u", query_string={"channel_id": "x"})
        self.assertEqual(resp.status_code, 400)


class ProviderBusyTests(unittest.TestCase):
    @staticmethod
    def _rec(title, provider, status):
        return {"title": title, "stream_provider": provider, "status": status}

    def test_only_eaglecast_jobs_holding_a_connection_count_as_busy(self):
        recs = [
            self._rec("A", "eaglecast", "recording"),
            self._rec("B", "eaglecast", "preflight"),
            self._rec("C", "eaglecast", "queued"),
            self._rec("D", "eaglecast", "done"),
            self._rec("E", "primestreams", "recording"),
        ]
        self.assertEqual(server._busy_eaglecast_titles(recs), ["A", "B"])

    def test_endpoint_reports_busy_for_an_eaglecast_channel(self):
        with server._rec_lock:
            server._recs.clear()
            server._recs["r1"] = self._rec("The Amateur", "eaglecast", "recording")
        with mock.patch.object(server, "_stream_url",
                               return_value=("http://x/1.ts", None, {"provider": "eaglecast"})):
            data = server.app.test_client().get(
                "/epg-web/api/play/provider", query_string={"channel_id": "18086"}).get_json()
        self.assertEqual(data, {"provider": "eaglecast", "busy": True, "busy_with": ["The Amateur"]})
        with server._rec_lock:
            server._recs.clear()

    def test_endpoint_is_never_busy_for_primestreams(self):
        with server._rec_lock:
            server._recs.clear()
            server._recs["r1"] = self._rec("The Amateur", "eaglecast", "recording")
        with mock.patch.object(server, "_stream_url",
                               return_value=("http://x/1.ts", None, {"provider": "primestreams"})):
            data = server.app.test_client().get(
                "/epg-web/api/play/provider", query_string={"channel_id": "1"}).get_json()
        self.assertFalse(data["busy"])
        with server._rec_lock:
            server._recs.clear()


class PlaylistKindsTests(unittest.TestCase):
    def test_plex_playlist_points_at_this_servers_stream_route(self):
        resp = server.app.test_client().get(
            "/epg-web/api/play/playlist.m3u",
            query_string={"plex_title": "Indiana Jones (1984)", "title": "Indiana Jones"},
            base_url="http://nas.test:5001")
        lines = resp.get_data(as_text=True).splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertEqual(lines[2],
                         "http://nas.test:5001/epg-web/api/plex/stream?title=Indiana%20Jones%20%281984%29")

    def test_eaglecast_playlist_only_serves_catalogue_feeds(self):
        catalogue = [{"stream_id": "7", "name": "ESPN", "extension": "ts"}]
        with mock.patch.object(server, "_eaglecast_live_sports", return_value=catalogue), \
             mock.patch.object(server, "_eaglecast_stream_url", return_value="http://eagle.test/7.ts"):
            client = server.app.test_client()
            ok = client.get("/epg-web/api/play/playlist.m3u", query_string={"eaglecast_id": "7"})
            bad = client.get("/epg-web/api/play/playlist.m3u", query_string={"eaglecast_id": "999"})
        self.assertEqual(ok.get_data(as_text=True).splitlines()[2], "http://eagle.test/7.ts")
        self.assertEqual(bad.status_code, 404)


class PlexReplaceTests(unittest.TestCase):
    def _plex(self, root, names):
        movies = os.path.join(root, "Movies")
        for name in names:
            os.makedirs(os.path.join(movies, name))
            with open(os.path.join(movies, name, "movie.mp4"), "wb") as fh:
                fh.write(b"0" * 16)
        return movies

    def _post(self, movies, body, candidate=None, detail=None):
        queued = {"channel": "SHO 2 HD", "start_ts": 1.0}
        with mock.patch.object(server, "load_config", return_value={"plex_path": movies}), \
             mock.patch.object(server, "_best_incomplete_rerecord",
                               return_value=(candidate, detail or {})) as best, \
             mock.patch.object(server, "_queue_best_retry", return_value=queued) as queue, \
             mock.patch.object(server, "_watch_abandoned_transfer") as wanted:
            resp = server.app.test_client().post("/epg-web/api/plex/replace", json=body)
        return resp, best, queue, wanted

    def test_moves_the_folder_aside_and_queues_a_fresh_recording(self):
        with tempfile.TemporaryDirectory() as root:
            movies = self._plex(root, ["Quantum of Solace (2008)", "Other (1999)"])
            resp, best, queue, wanted = self._post(
                movies, {"title": "Quantum of Solace", "year": "2008"},
                candidate={"channel": "SHO 2 HD"})
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(sorted(os.listdir(movies)), ["Other (1999)"])
            held = os.path.join(root, "_wrong_movies")
            self.assertEqual(len(os.listdir(held)), 1)
            self.assertTrue(os.path.exists(os.path.join(held, os.listdir(held)[0], "movie.mp4")))
            queue.assert_called_once()
            self.assertEqual(data["queued"]["channel"], "SHO 2 HD")

    def test_no_future_airing_adds_the_title_to_wanted(self):
        with tempfile.TemporaryDirectory() as root:
            movies = self._plex(root, ["Quantum of Solace (2008)"])
            resp, _best, queue, wanted = self._post(movies, {"title": "Quantum of Solace"})
            self.assertTrue(resp.get_json()["added_to_wanted"])
            wanted.assert_called_once()
            queue.assert_not_called()

    def test_same_title_in_two_years_needs_the_year(self):
        with tempfile.TemporaryDirectory() as root:
            movies = self._plex(root, ["The Untouchables (1987)", "The Untouchables (1959)"])
            resp, *_ = self._post(movies, {"title": "The Untouchables"})
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(len(os.listdir(movies)), 2)
            resp, *_ = self._post(movies, {"title": "The Untouchables", "year": "1987"})
            self.assertTrue(resp.get_json()["ok"])
            self.assertEqual(os.listdir(movies), ["The Untouchables (1959)"])

    def test_unknown_title_changes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            movies = self._plex(root, ["Other (1999)"])
            resp, *_ = self._post(movies, {"title": "Nope"})
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(os.listdir(movies), ["Other (1999)"])


class AutoRetryTests(unittest.TestCase):
    """A recording that failed because it was cut short or wrong looks for another airing."""

    def _heartbeat(self, message):
        server._init_recordings_table()
        rec = {"title": "Quantum of Solace", "channel_id": "5", "channel": "SHO",
               "start_ts": time.time() - 3600, "stop_ts": time.time() + 3600, "status": "recording",
               "progress": 0, "log": [], "pid": None, "file": None, "backend": "agent",
               "stream_id": "1", "stream_provider": "primestreams", "stream_extension": "ts",
               "episode_title": "", "season_num": None, "episode_num": None,
               "is_series": False, "auto_upgrade": False}
        rec_id = "retry" + str(int(time.time() * 1000) % 100000)
        server._db_upsert_rec(rec_id, rec)
        with mock.patch.object(server, "_agent_authorized", return_value=True), \
             mock.patch.object(server, "_best_incomplete_rerecord", return_value=(None, {})) as best, \
             mock.patch.object(server, "_watch_abandoned_transfer") as wanted:
            resp = server.app.test_client().post(
                f"/epg-web/api/agent/jobs/{rec_id}/heartbeat",
                json={"agent_id": "a", "status": "failed", "message": message})
        self.assertEqual(resp.status_code, 200)
        return best, wanted

    def test_early_end_looks_for_another_airing(self):
        best, wanted = self._heartbeat("Recording ended early: 22 min captured of 137 min scheduled")
        best.assert_called_once()
        wanted.assert_called_once()

    def test_wrong_programme_looks_for_another_airing(self):
        best, wanted = self._heartbeat('Wrong programme on stream: provider shows "The Terminator"')
        best.assert_called_once()

    def test_other_failures_do_not_retry(self):
        best, wanted = self._heartbeat("FFmpeg recording failed")
        best.assert_not_called()


class SdFetchGuardTests(unittest.TestCase):
    def test_a_second_fetch_is_not_started_while_one_is_running(self):
        server._sd_status.update({'running': True, 'log': [], 'result': None, 'error': None})
        try:
            with mock.patch.object(server, "load_config",
                                   return_value={"sd_user": "u", "sd_pass": "p"}), \
                 mock.patch.object(server.threading, "Thread") as thread:
                resp = server.app.test_client().post("/epg-web/api/fetch-sd", json={"days": 21})
            self.assertEqual(resp.status_code, 200)
            self.assertIn("already running", resp.get_json()["message"])
            thread.assert_not_called()
        finally:
            server._sd_status.update({'running': False})


class ThreadDumpSignalTests(unittest.TestCase):
    def test_sigusr1_writes_every_threads_stack_and_the_server_keeps_running(self):
        import signal
        import subprocess
        import sys
        code = ("import os, signal, time, tempfile\n"
                "os.environ['EPG_BASE_DIR'] = tempfile.mkdtemp()\n"
                "import server\n"
                "os.kill(os.getpid(), signal.SIGUSR1)\n"
                "time.sleep(0.5)\n"
                "print('still running')\n")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                timeout=60, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertIn("still running", result.stdout)
        self.assertIn("most recent call first", result.stderr)


class DailyGuideRefreshTests(unittest.TestCase):
    @staticmethod
    def _at(hour, minute=0, day=10):
        from datetime import datetime as _dt
        return _dt(2026, 11, day, hour, minute)

    def test_due_inside_the_window_on_a_day_it_has_not_run(self):
        self.assertTrue(server._daily_refresh_due(self._at(3, 5), "2026-11-09"))
        self.assertTrue(server._daily_refresh_due(self._at(5, 59), ""))

    def test_not_due_outside_the_window(self):
        for hour in (0, 2, 6, 14, 23):
            self.assertFalse(server._daily_refresh_due(self._at(hour, 30), "2026-11-09"), hour)

    def test_not_due_twice_in_one_day_or_when_switched_off(self):
        self.assertFalse(server._daily_refresh_due(self._at(3, 30), "2026-11-10"))
        self.assertFalse(server._daily_refresh_due(self._at(3, 30), "2026-11-09", enabled=False))

    def test_the_hour_is_configurable(self):
        self.assertTrue(server._daily_refresh_due(self._at(4, 0), "", hour=4))
        self.assertFalse(server._daily_refresh_due(self._at(3, 0), "", hour=4))

    def test_marker_round_trips_and_a_missing_file_means_never_run(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(server, "BASE_DIR", temp):
            self.assertEqual(server._read_daily_refresh_marker(), "")
            server._write_daily_refresh_marker("2026-11-10")
            self.assertEqual(server._read_daily_refresh_marker(), "2026-11-10")

    def test_runs_the_provider_guide_then_schedules_direct_even_if_the_first_fails(self):
        order = []

        def provider():
            order.append("provider")
            raise RuntimeError("provider down")

        def sd():
            order.append("sd")
            return server.jsonify({"ok": True})

        with mock.patch.object(server, "api_fetch_guide", provider), \
             mock.patch.object(server, "api_fetch_sd", sd), \
             mock.patch.object(server, "_wait_for_guide_load"):
            server._run_daily_guide_refresh()
        self.assertEqual(order, ["provider", "sd"])

    def test_the_loop_marks_the_day_before_it_starts_the_refresh(self):
        events = []
        with mock.patch.object(server, "_daily_refresh_due", return_value=True), \
             mock.patch.object(server, "_read_daily_refresh_marker", return_value=""), \
             mock.patch.object(server, "_write_daily_refresh_marker",
                               side_effect=lambda d: events.append(("mark", d))), \
             mock.patch.object(server, "_run_daily_guide_refresh",
                               side_effect=lambda: events.append(("run",))), \
             mock.patch.object(server, "load_config", return_value={}), \
             mock.patch.dict(server._startup_status, {"loading": False, "background_loading": False}), \
             mock.patch.object(server.time, "sleep", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                server._daily_guide_loop()
        self.assertEqual([e[0] for e in events], ["mark", "run"])

    def test_the_loop_waits_while_the_guide_is_loading(self):
        with mock.patch.object(server, "_daily_refresh_due", return_value=True) as due, \
             mock.patch.object(server, "_run_daily_guide_refresh") as run, \
             mock.patch.object(server, "load_config", return_value={}), \
             mock.patch.dict(server._startup_status, {"loading": False, "background_loading": True}), \
             mock.patch.object(server.time, "sleep", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                server._daily_guide_loop()
        run.assert_not_called()
