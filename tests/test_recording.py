import json
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
            open(os.path.join(movies, "F X (1986)", "F X.mp4"), "wb").close()       # a folder counts when a film is in it
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

    def _plex_info(self, temp, query):
        server._plex_info_cache.clear()
        with mock.patch.object(server, "load_config", return_value={"plex_path": temp}):
            return server.app.test_client().get("/epg-web/api/plex/info?" + query).get_json()

    def test_plex_info_tolerates_extra_spaces_when_no_year_is_asked_for(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = os.path.join(temp, "Alvin and the Chipmunks  Chipwrecked")
            os.makedirs(folder)
            open(os.path.join(folder, "movie.mp4"), "wb").close()
            data = self._plex_info(temp, "title=Alvin%20and%20the%20Chipmunks%3A%20Chipwrecked")
            self.assertTrue(data["found"])

    def test_plex_info_does_not_assume_a_folder_without_a_year_is_the_asked_for_year(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = os.path.join(temp, "Employee of the Month")
            os.makedirs(folder)
            open(os.path.join(folder, "movie.mp4"), "wb").close()
            self.assertFalse(self._plex_info(temp, "title=Employee%20of%20the%20Month&year=2004")["found"])
            self.assertFalse(self._plex_info(temp, "title=Employee%20of%20the%20Month%20(2004)")["found"])

    def test_plex_info_picks_the_folder_of_the_asked_for_year(self):
        with tempfile.TemporaryDirectory() as temp:
            for name, content in (("Employee of the Month (2004)", b"a"), ("Employee of the Month (2022)", b"bb")):
                os.makedirs(os.path.join(temp, name))
                with open(os.path.join(temp, name, name + ".mp4"), "wb") as handle:
                    handle.write(content)
            data = self._plex_info(temp, "title=Employee%20of%20the%20Month&year=2022")
            self.assertEqual(data["file"], "Employee of the Month (2022).mp4")
            data = self._plex_info(temp, "title=Employee%20of%20the%20Month&year=2004")
            self.assertEqual(data["file"], "Employee of the Month (2004).mp4")

    def test_recommendation_with_a_year_is_not_in_plex_for_a_yearless_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            os.makedirs(os.path.join(temp, "Employee of the Month"))
            open(os.path.join(temp, "Employee of the Month", "film.mp4"), "wb").close()
            server._plex_title_cache.update({"roots": None, "loaded_at": 0})
            with mock.patch.object(server, "load_config", return_value={"plex_path": temp, "plex_tv_path": temp + "/tv"}):
                idx = server._plex_wanted_title_index()
            key = server._norm_plex_show("Employee of the Month")
            self.assertIn(key, idx["unyearred_movies"])
            self.assertNotIn((key, "2004"), idx["movie_versions"])
            self.assertEqual(idx["movie_counts"][key], 1)

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

    def test_conflict_items_carry_the_ids_of_the_jobs_in_the_way(self):
        a = {"title": "Joyride", "channel": "Starz", "start_ts": 20, "stop_ts": 90, "status": "scheduled"}
        b = {"title": "Head Office", "channel": "HBO", "start_ts": 10, "stop_ts": 90, "status": "recording"}
        c = {"title": "Elsewhere", "start_ts": 500, "stop_ts": 600, "status": "scheduled"}
        items = server._slot_conflict_items([("id-a", a), ("id-b", b), ("id-c", c)], [a, b])
        self.assertEqual([i["id"] for i in items], ["id-b", "id-a"])          # in time order
        self.assertTrue(items[0]["running"])
        self.assertFalse(items[1]["running"])
        self.assertEqual(items[1]["title"], "Joyride")

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


class GuideKeepPastTests(unittest.TestCase):
    def _db(self, temp):
        import datetime as dt
        import sd_guide
        db = os.path.join(temp, "guide.db")
        sd_guide.ensure_guide_db(db)
        now = dt.datetime.now(dt.timezone.utc)
        fmt = "%Y%m%d%H%M%S"
        conn = sqlite3.connect(db)
        for title, start in (("Old", now - dt.timedelta(days=60)), ("Recent", now - dt.timedelta(days=1)),
                             ("Soon", now + dt.timedelta(hours=2)), ("Later", now + dt.timedelta(days=10))):
            conn.execute("insert into guide(title,channel_id,channel_name,start_utc,end_utc,desc,category) "
                         "values (?,?,?,?,?,?,?)",
                         (title, "c1", "One", start.strftime(fmt),
                          (start + dt.timedelta(hours=1)).strftime(fmt), "", ""))
        conn.commit()
        conn.close()
        return db

    def _titles(self, cfg):
        with tempfile.TemporaryDirectory() as temp:
            db = self._db(temp)
            with mock.patch.object(server, "load_config", return_value=cfg):
                server.load_epg_from_db(db)
            return sorted(p["title"] for p in server._epg["programmes"])

    def test_the_full_load_skips_old_history_but_keeps_the_recent_past_and_the_future(self):
        self.assertEqual(self._titles({}), ["Later", "Recent", "Soon"])

    def test_a_custom_number_of_past_days(self):
        self.assertEqual(self._titles({"guide_keep_past_days": 0.5}), ["Later", "Soon"])
        self.assertEqual(self._titles({"guide_keep_past_days": 90}), ["Later", "Old", "Recent", "Soon"])

    def test_zero_keeps_everything(self):
        self.assertEqual(self._titles({"guide_keep_past_days": 0}), ["Later", "Old", "Recent", "Soon"])


class RecordingsOverviewTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    @staticmethod
    def _row(title, status, start, stop, provider="primestreams", note=""):
        return {"rec_id": title[:4], "title": title, "channel": "Ch", "stream_provider": provider,
                "start_ts": start, "stop_ts": stop, "status": status, "failure_reason": note}

    def test_rows_are_sorted_into_the_four_groups(self):
        n = self.NOW
        rows = [
            self._row("Now", "recording", n - 600, n + 1800),
            self._row("Finishing", "transferring", n - 4000, n - 100),
            self._row("Later", "queued", n + 7200, n + 14400),
            self._row("Soon", "scheduled (5m away)", n + 300, n + 3900),
            self._row("Claimed", "agent_claimed", n + 100, n + 3700),
            self._row("Good", "done", n - 7200, n - 3600),
            self._row("Bad", "failed", n - 5000, n - 4000, note="Recording ended early"),
        ]
        d = server._group_recordings(rows, n)
        self.assertEqual([r["title"] for r in d["recording"]], ["Now"])
        self.assertEqual([r["title"] for r in d["finishing"]], ["Finishing"])
        self.assertEqual([r["title"] for r in d["upcoming"]], ["Claimed", "Soon", "Later"])
        self.assertEqual([r["title"] for r in d["recent"]], ["Good", "Bad"])   # newest first
        self.assertEqual(d["upcoming_total"], 3)

    def test_stale_rows_are_not_shown_as_running_or_coming_up(self):
        n = self.NOW
        rows = [
            self._row("Zombie", "recording", n - 90000, n - 86000),   # stuck "recording" a day ago
            self._row("Missed", "queued", n - 90000, n - 86000),      # queued in the past
            self._row("OldDone", "done", n - 200000, n - 190000),     # finished days ago
        ]
        d = server._group_recordings(rows, n)
        self.assertEqual((d["recording"], d["upcoming"], d["recent"]), ([], [], []))

    def test_upcoming_is_limited_and_counted(self):
        n = self.NOW
        rows = [self._row(f"S{i:02d}", "queued", n + 100 * (i + 1), n + 100 * (i + 1) + 50) for i in range(30)]
        d = server._group_recordings(rows, n, upcoming_limit=20)
        self.assertEqual(len(d["upcoming"]), 20)
        self.assertEqual(d["upcoming_total"], 30)

    def test_endpoint_reads_the_recordings_table(self):
        server._init_recordings_table()
        now = time.time()
        rec = {"title": "Overview Test Movie", "channel_id": "5", "channel": "SHO", "start_ts": now + 600,
               "stop_ts": now + 4200, "status": "queued", "progress": 0, "log": [], "pid": None, "file": None,
               "backend": "agent", "stream_id": "1", "stream_provider": "eaglecast", "stream_extension": "ts",
               "episode_title": "", "season_num": None, "episode_num": None, "is_series": False,
               "auto_upgrade": False}
        server._db_upsert_rec("ovw12345", rec)
        data = server.app.test_client().get("/epg-web/api/recordings/overview").get_json()
        self.assertTrue(data["ok"])
        titles = [r["title"] for r in data["upcoming"]]
        self.assertIn("Overview Test Movie", titles)
        item = next(r for r in data["upcoming"] if r["title"] == "Overview Test Movie")
        self.assertEqual(item["provider"], "eaglecast")


class VersionLabelTests(unittest.TestCase):
    def test_version_is_v_date_letter(self):
        import re as _re
        self.assertRegex(server.VERSION, r"^v\d{8}[a-z]$")
        import datetime as dt
        dt.datetime.strptime(server.VERSION[1:9], "%Y%m%d")      # the date part is a real date

    def test_the_header_shows_the_version(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn(server.VERSION, html)

    def test_slot_full_dialog_lets_you_remove_one_and_retries(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("function showSlotFullModal(d, retry)", html)
        self.assertIn("showSlotFullModal(d, () => quickRecord(key, btnEl))", html)
        self.assertIn("showSlotFullModal(r, () => recordAiring(airing, title, btn, allowCommercialMovie, year))", html)
        self.assertIn("/epg-web/api/record/cancel", html)

    def test_the_recordings_window_has_a_refresh_button(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn('id="rec-window-refresh"', html)
        self.assertIn("onclick=\"refreshRecWindowNow()\"", html)
        self.assertIn("async function refreshRecWindowNow()", html)


class AgentSeenTests(unittest.TestCase):
    def setUp(self):
        server._init_recordings_table()
        server._agent_seen.update({'id': '', 'version': '', 'at': 0.0})

    def _post(self, path, body):
        with mock.patch.object(server, "_agent_authorized", return_value=True):
            return server.app.test_client().post(path, json=body)

    def test_a_claim_records_the_recorders_version_and_time(self):
        resp = self._post("/epg-web/api/agent/jobs/claim",
                          {"agent_id": "mac-mini", "agent_version": "v20261110c"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(server._agent_seen["id"], "mac-mini")
        self.assertEqual(server._agent_seen["version"], "v20261110c")
        self.assertAlmostEqual(server._agent_seen["at"], time.time(), delta=5)

    def test_a_heartbeat_records_it_too_even_for_an_unknown_job(self):
        self._post("/epg-web/api/agent/jobs/nope/heartbeat",
                   {"agent_id": "mac-mini", "agent_version": "v20261110c", "status": "recording"})
        self.assertEqual(server._agent_seen["version"], "v20261110c")

    def test_an_old_recorder_without_a_version_is_still_recorded(self):
        self._post("/epg-web/api/agent/jobs/claim", {"agent_id": "mac-mini"})
        self.assertEqual(server._agent_seen["version"], "")
        self.assertGreater(server._agent_seen["at"], 0)

    def test_overview_reports_the_recorder_or_none_if_it_has_not_checked_in(self):
        data = server.app.test_client().get("/epg-web/api/recordings/overview").get_json()
        self.assertIsNone(data["agent"])
        server._agent_seen.update({'id': 'mac-mini', 'version': 'v20261110c', 'at': time.time() - 12})
        data = server.app.test_client().get("/epg-web/api/recordings/overview").get_json()
        self.assertEqual(data["agent"]["version"], "v20261110c")
        self.assertAlmostEqual(data["agent"]["seconds_ago"], 12, delta=3)


class HtmlEntityTextTests(unittest.TestCase):
    """OMDb sometimes returns names such as D&apos;Onofrio: show them as plain text."""

    def test_entities_become_characters(self):
        self.assertEqual(server._decode_entities("Vincent D&apos;Onofrio, Tom &amp; Jerry &quot;Hi&quot;"),
                         "Vincent D'Onofrio, Tom & Jerry \"Hi\"")
        self.assertEqual(server._decode_entities("Plain text"), "Plain text")
        self.assertEqual(server._decode_entities(2023), 2023)
        self.assertEqual(server._decode_info({"actors": "D&apos;Onofrio", "year": "2023", "n": 5}),
                         {"actors": "D'Onofrio", "year": "2023", "n": 5})

    def test_the_decorator_cleans_a_json_answer_and_keeps_the_status(self):
        @server._decode_response_text
        def view():
            return server.jsonify({"actors": "Vincent D&apos;Onofrio", "error": "x &amp; y"}), 502

        @server._decode_response_text
        def plain():
            return server.jsonify({"ok": True})
        with server.app.test_request_context("/"):
            response, status = view(), None
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.get_json(), {"actors": "Vincent D'Onofrio", "error": "x & y"})
            self.assertEqual(plain().status_code, 200)

    def test_programme_info_answers_go_through_the_decorator(self):
        self.assertTrue(hasattr(server.api_prog_info, "__wrapped__"))


class ActiveRecordingsPanelTests(unittest.TestCase):
    """The Active Recordings panel shows when, where and what the stream looks like."""

    def setUp(self):
        server._init_recordings_table()

    def test_stream_info_keeps_only_the_expected_clean_fields(self):
        clean = server._clean_stream_info({
            "width": 1920, "height": "1080", "fps": 29.97003, "video_codec": "h264<script>", "audio_codec": "aac",
            "audio_channels": 2, "total_bitrate": 6821000, "login": "secret", "width_x": 5})
        self.assertEqual(clean, {"width": 1920, "height": 1080, "fps": 29.97, "video_codec": "h264script",
                                 "audio_codec": "aac", "audio_channels": 2, "total_bitrate": 6821000})
        self.assertEqual(server._clean_stream_info("nope"), {})
        self.assertEqual(server._clean_stream_info({"width": "abc", "fps": "x"}), {})

    def test_a_heartbeat_carries_the_stream_info_to_the_panel(self):
        rec = {"title": "Info Test", "channel": "HBO", "channel_id": "hbo", "stream_id": "1",
               "start_ts": time.time() - 60, "stop_ts": time.time() + 3600, "status": "queued",
               "backend": "agent", "file": ""}
        server._db_upsert_rec("infojob", rec)
        with mock.patch.object(server, "_agent_authorized", return_value=True):
            resp = server.app.test_client().post(
                "/epg-web/api/agent/jobs/infojob/heartbeat",
                json={"agent_id": "a", "status": "recording",
                      "stream_info": {"width": 1920, "height": 1080, "fps": 29.97, "video_codec": "h264"}})
        self.assertEqual(resp.status_code, 200)
        data = server.app.test_client().get("/epg-web/api/record/status").get_json()
        self.assertEqual(data["recordings"]["infojob"]["stream_info"]["height"], 1080)

    def test_the_panel_script_shows_time_countdown_and_stream_details(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        for needle in ("function _recStreamText(s)", "starts in ", " left'", "_recDay(r.start_ts, now)",
                       "r.stream_info"):
            self.assertIn(needle, html)

    def test_the_panel_starts_when_the_page_opens(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("setInterval(refreshRecBadge, 30000);\nrefreshRecBadge();", html)
        self.assertRegex(html, r"refreshRecBadge\(\);\n(?://[^\n]*\n)+startRecPoll\(\);")

    def test_the_stream_text_is_formatted(self):
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        source = re.search(r"function _recStreamText\(s\) \{.*?\n\}\n", server.HTML, re.S).group(0)
        script = source + ("console.log(_recStreamText({width:1920,height:1080,fps:29.97,video_codec:'h264',"
                           "audio_codec:'aac',audio_channels:2,total_bitrate:6821000}));"
                           "console.log(_recStreamText({fps:30}));console.log('['+_recStreamText(null)+']');")
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30).stdout.split("\n")
        self.assertEqual(out[0], "1920\u00d71080 \u00b7 29.97 fps \u00b7 H264 \u00b7 AAC 2ch \u00b7 6.8 Mbps")
        self.assertEqual(out[1], "30 fps")
        self.assertEqual(out[2], "[]")


class MarkFinishedTests(unittest.TestCase):
    def setUp(self):
        server._init_recordings_table()

    def _rec(self, rec_id, status):
        now = time.time()
        rec = {"title": "Mark Test", "channel_id": "5", "channel": "SHO", "start_ts": now - 3600,
               "stop_ts": now - 1800, "status": status, "progress": 0, "log": [], "pid": None,
               "file": "/x/mark_test.mp4", "backend": "agent", "stream_id": "1",
               "stream_provider": "primestreams", "stream_extension": "ts", "episode_title": "",
               "season_num": None, "episode_num": None, "is_series": False, "auto_upgrade": False,
               "failure_reason": "Recording agent heartbeat expired"}
        server._db_upsert_rec(rec_id, rec)

    def _status(self, rec_id):
        conn = sqlite3.connect(server._guide_db_path())
        row = conn.execute("SELECT status, failure_reason, file FROM recordings WHERE rec_id=?", (rec_id,)).fetchone()
        conn.close()
        return row

    def _mark(self, rec_id):
        return server.app.test_client().post("/epg-web/api/recordings/mark-finished", json={"id": rec_id})

    def test_a_failed_recording_becomes_done_and_keeps_its_file(self):
        self._rec("mk_failed1", "failed")
        self.assertEqual(self._mark("mk_failed1").status_code, 200)
        status, reason, file = self._status("mk_failed1")
        self.assertEqual(status, "done")
        self.assertEqual(file, "/x/mark_test.mp4")
        self.assertIn("by hand", reason)

    def test_a_recording_in_progress_cannot_be_marked(self):
        self._rec("mk_live1", "recording")
        self.assertEqual(self._mark("mk_live1").status_code, 409)
        self.assertEqual(self._status("mk_live1")[0], "recording")

    def test_unknown_or_missing_id(self):
        self.assertEqual(self._mark("nope_nope").status_code, 404)
        self.assertEqual(self._mark("").status_code, 400)


class RecordingYearTests(unittest.TestCase):
    def setUp(self):
        server._init_recordings_table()

    def test_clean_year(self):
        self.assertEqual(server._clean_year("2004"), "2004")
        self.assertEqual(server._clean_year(" (1999) "), "1999")
        self.assertEqual(server._clean_year("2004-05-01"), "2004")
        self.assertEqual(server._clean_year(None), "")
        self.assertEqual(server._clean_year("n/a"), "")
        self.assertEqual(server._clean_year("12345"), "")

    def test_the_year_is_saved_and_handed_to_the_recorder(self):
        now = time.time()
        rec = {"title": "Year Test Movie", "channel_id": "5", "channel": "SHO", "start_ts": now + 600,
               "stop_ts": now + 4200, "status": "queued", "progress": 0, "log": [], "pid": None,
               "file": None, "backend": "agent", "stream_id": "1", "stream_provider": "primestreams",
               "stream_extension": "ts", "episode_title": "", "season_num": None, "episode_num": None,
               "is_series": False, "auto_upgrade": False, "year": "2004"}
        server._db_upsert_rec("year12345", rec)
        conn = sqlite3.connect(server._guide_db_path())
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM recordings WHERE rec_id=?", ("year12345",)).fetchone()
        conn.close()
        self.assertEqual(server._agent_job_dict(row)["year"], "2004")

    def test_a_status_update_does_not_wipe_the_year(self):
        now = time.time()
        rec = {"title": "Year Keep Movie", "channel_id": "5", "start_ts": now, "stop_ts": now + 3600,
               "status": "queued", "backend": "agent", "year": "2004"}
        server._db_upsert_rec("year_keep1", rec)
        rec2 = dict(rec, status="recording", year="")
        server._db_upsert_rec("year_keep1", rec2)
        conn = sqlite3.connect(server._guide_db_path())
        year = conn.execute("SELECT year FROM recordings WHERE rec_id='year_keep1'").fetchone()[0]
        conn.close()
        self.assertEqual(year, "2004")

    def test_a_row_without_a_year_gives_an_empty_year(self):
        now = time.time()
        server._db_upsert_rec("year_none1", {"title": "No Year", "channel_id": "5", "start_ts": now,
                                             "stop_ts": now + 3600, "status": "queued", "backend": "agent"})
        conn = sqlite3.connect(server._guide_db_path())
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM recordings WHERE rec_id='year_none1'").fetchone()
        conn.close()
        self.assertEqual(server._agent_job_dict(row)["year"], "")


class WantedNeedsYearTests(unittest.TestCase):
    def _add(self, **body):
        data = {"action": "add", "title": "Needs Year Test Film", "type": "movie"}
        data.update(body)
        return server.app.test_client().post("/epg-web/api/wanted", json=data)

    def test_a_movie_without_a_year_is_refused(self):
        resp = self._add(year="")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("needs a year", resp.get_json()["error"])
        self.assertEqual(self._add(year="n/a").status_code, 400)

    def test_a_movie_with_a_year_is_accepted(self):
        server._init_recordings_table()
        with mock.patch.object(server, "db_run") as db_run:
            resp = self._add(year="2004")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db_run.call_args.args[1][2], "2004")

    def test_a_series_needs_a_year_too(self):
        resp = self._add(year="", type="series")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("series needs a year", resp.get_json()["error"].lower())

    def test_a_series_range_keeps_its_first_year(self):
        with mock.patch.object(server, "db_run") as db_run:
            resp = self._add(year="1964\u20131972", type="series")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db_run.call_args.args[1][2], "1964")


class SeriesYearTests(unittest.TestCase):
    def test_a_year_in_the_folder_name_does_not_change_the_title_key(self):
        n = server._norm_plex_show
        self.assertEqual(n("Bewitched (1964)"), n("Bewitched"))
        self.assertEqual(n("Star Trek: The Next Generation (1987)"), n("Star Trek The Next Generation"))
        self.assertNotEqual(n("Blade Runner 2049"), n("Blade Runner"))

    def test_episode_keys_ignore_the_year_in_the_show_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            season = os.path.join(temp, "Bewitched (1964)", "Season 01")
            os.makedirs(season)
            open(os.path.join(season, "Bewitched - S01E02.mp4"), "wb").close()
            server._plex_episode_cache.update({"root": None, "loaded_at": 0})
            self.assertIn("bewitched|1|2", server._plex_episode_keys(temp))

    def test_the_show_index_tracks_years_and_yearless_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("Hawaii Five-0 (1968)", "Magnum PI"):
                os.makedirs(os.path.join(temp, "tv", name))
            os.makedirs(os.path.join(temp, "Movies"))
            server._plex_title_cache.update({"roots": None, "loaded_at": 0})
            with mock.patch.object(server, "load_config", return_value={"plex_path": temp + "/Movies", "plex_tv_path": temp + "/tv"}):
                idx = server._plex_wanted_title_index()
            n = server._norm_plex_show
            self.assertIn((n("Hawaii Five-0"), "1968"), idx["show_versions"])
            self.assertNotIn((n("Hawaii Five-0"), "2010"), idx["show_versions"])
            self.assertIn(n("Magnum PI"), idx["unyearred_shows"])
            self.assertIn(n("Hawaii Five-0"), idx["shows"])

    def test_the_wanted_series_year_is_used_when_there_is_exactly_one(self):
        rows = [{"title": "Perry Mason", "year": "2020"}]
        with mock.patch.object(server, "db_rows", return_value=rows):
            self.assertEqual(server._wanted_series_year("Perry Mason (2020)"), "2020")
        rows = [{"title": "Perry Mason", "year": "1957"}, {"title": "Perry Mason", "year": "2020"}]
        with mock.patch.object(server, "db_rows", return_value=rows):
            self.assertEqual(server._wanted_series_year("Perry Mason"), "")
        with mock.patch.object(server, "db_rows", side_effect=RuntimeError("db")):
            self.assertEqual(server._wanted_series_year("Perry Mason"), "")


class GuideYearRecordingTests(unittest.TestCase):
    CHANNEL = "yr-chan-1"

    def setUp(self):
        with server._rec_lock:
            server._recs.clear()
            server._rec_cancel_events.clear()
        server._init_recordings_table()
        conn = sqlite3.connect(server._guide_db_path())
        conn.execute("DELETE FROM guide WHERE channel_id IN (?, ?)", (self.CHANNEL, "yr-sd-1"))
        conn.execute("DELETE FROM recordings")
        conn.commit()
        conn.close()
        self.start = time.time() + 7200
        self.start_key = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.start))
        self.end_key = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.start + 6000))

    def _guide(self, title, year, channel=None, shift=0):
        key = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.start + shift))
        conn = sqlite3.connect(server._guide_db_path())
        conn.execute("INSERT INTO guide(title, channel_id, channel_name, start_utc, end_utc, desc, category, prog_type, year) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     (title, channel or self.CHANNEL, "Chan", key, self.end_key, "", "", "MV", year))
        conn.commit()
        conn.close()

    def _record(self, **extra):
        body = {"title": "Year Film", "channel_id": self.CHANNEL, "channel_name": "Chan",
                "start_ts": self.start, "stop_ts": self.start + 6000}
        body.update(extra)
        with mock.patch.object(server, "_recording_backend", return_value="agent"), \
             mock.patch.object(server, "_resolve_recording_source",
                               return_value=("http://x/1.ts", None, {"stream_id": "1", "provider": "primestreams"})):
            return server.app.test_client().post("/epg-web/api/record", json=body)

    def _saved_year(self):
        conn = sqlite3.connect(server._guide_db_path())
        row = conn.execute("SELECT year FROM recordings ORDER BY created_at DESC LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else None

    def test_the_guide_year_is_found_for_the_airing(self):
        self._guide("Year Film", "2004")
        found = server._guide_year_for_airing(server._guide_db_path(), self.CHANNEL, "Year Film", self.start)
        self.assertEqual(found, "2004")

    def test_the_year_is_found_from_another_channel_within_15_minutes(self):
        self._guide("Year Film", "2004", channel="yr-sd-1", shift=300)
        found = server._guide_year_for_airing(server._guide_db_path(), self.CHANNEL, "Year Film", self.start)
        self.assertEqual(found, "2004")
        far = server._guide_year_for_airing(server._guide_db_path(), self.CHANNEL, "Year Film", self.start + 7200)
        self.assertEqual(far, "")

    def test_two_different_years_nearby_is_a_guess_so_blank(self):
        self._guide("Year Film", "2004", channel="yr-sd-1")
        self._guide("Year Film", "2022", channel="yr-sd-2")
        found = server._guide_year_for_airing(server._guide_db_path(), "elsewhere", "Year Film", self.start)
        self.assertEqual(found, "")

    def test_an_airing_of_a_different_film_is_refused(self):
        self._guide("Year Film", "2022")
        resp = self._record(year="2004")
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertTrue(data["year_mismatch"])
        self.assertIn("2022", data["error"])
        self.assertIsNone(self._saved_year())

    def test_a_year_one_apart_is_the_same_film_and_the_guide_year_is_used(self):
        self._guide("Year Film", "2016")
        resp = self._record(year="2017")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._saved_year(), "2016")

    def test_the_matching_year_is_recorded(self):
        self._guide("Year Film", "2004")
        resp = self._record(year="2004")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._saved_year(), "2004")

    def test_the_guide_year_is_used_when_the_caller_sends_none(self):
        self._guide("Year Film", "2004")
        self.assertEqual(self._record().status_code, 200)
        self.assertEqual(self._saved_year(), "2004")

    def test_no_guide_year_and_none_sent_still_records_without_a_year(self):
        self.assertEqual(self._record().status_code, 200)
        self.assertEqual(self._saved_year(), "")

    def test_a_caller_year_is_kept_when_the_guide_has_none(self):
        self.assertEqual(self._record(year="2004").status_code, 200)
        self.assertEqual(self._saved_year(), "2004")


class GuideYearLoadTests(unittest.TestCase):
    def _programmes(self, rows):
        db = os.path.join(TEST_DATA_DIR, "guide.db")
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM guide WHERE channel_id LIKE 'ld-%'")
        for title, channel, start, year, ptype in rows:
            conn.execute("INSERT INTO guide(title, channel_id, channel_name, start_utc, end_utc, desc, category, prog_type, year) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (title, channel, channel, start, "20990101020000", "", "", ptype, year))
        conn.commit()
        conn.close()
        server.load_epg_from_db(db)
        return {(p["channel_id"]): p for p in server._epg["programmes"] if p["channel_id"].startswith("ld-")}

    def test_the_sd_year_reaches_the_provider_listing_of_the_same_film(self):
        progs = self._programmes([
            ("Employee of the Month", "ld-sd", "20990101000000", "2004", "MV"),
            ("Employee of the Month", "ld-provider", "20990101000500", "", ""),
        ])
        self.assertEqual(progs["ld-sd"]["year"], "2004")
        self.assertEqual(progs["ld-provider"]["year"], "2004")

    def test_a_listing_with_no_matching_year_stays_blank(self):
        progs = self._programmes([
            ("Employee of the Month", "ld-sd", "20990101000000", "2004", "MV"),
            ("Employee of the Month", "ld-provider", "20990101040000", "", ""),
        ])
        self.assertEqual(progs["ld-provider"]["year"], "")

    def test_two_films_in_one_window_are_not_guessed(self):
        progs = self._programmes([
            ("Employee of the Month", "ld-sd1", "20990101000000", "2004", "MV"),
            ("Employee of the Month", "ld-sd2", "20990101000600", "2022", "MV"),
            ("Employee of the Month", "ld-provider", "20990101000300", "", ""),
        ])
        self.assertEqual(progs["ld-provider"]["year"], "")


class UpgradeYearGateTests(unittest.TestCase):
    INDEX = {"movies": {"employeeofthemonth", "heat"},
             "movie_versions": {("employeeofthemonth", "2022"), ("heat", "1995")},
             "movie_counts": {"employeeofthemonth": 1, "heat": 2}}

    def test_a_known_year_must_match_the_plex_folder_year(self):
        gate = server._upgrade_matches_plex
        self.assertTrue(gate(self.INDEX, "employeeofthemonth", "2022"))
        self.assertFalse(gate(self.INDEX, "employeeofthemonth", "2004"))      # the remake is not an upgrade
        self.assertTrue(gate(self.INDEX, "heat", "1995"))                     # two copies, but the year picks one

    def test_with_no_year_a_title_held_once_is_matched(self):
        self.assertTrue(server._upgrade_matches_plex(self.INDEX, "employeeofthemonth", ""))

    def test_with_no_year_a_title_held_twice_is_left_alone(self):
        self.assertFalse(server._upgrade_matches_plex(self.INDEX, "heat", ""))

    def test_a_title_not_in_plex_is_never_matched(self):
        self.assertFalse(server._upgrade_matches_plex(self.INDEX, "unknown", ""))
        self.assertFalse(server._upgrade_matches_plex(self.INDEX, "unknown", "2004"))


class RecommendationAiringYearTests(unittest.TestCase):
    def _recs(self, wanted_year, airings):
        wanted = [{"id": 1, "title": "Employee of the Month", "normalized_title": "employee of the month",
                   "year": wanted_year, "type": "movie", "status": "wanted", "notes": "", "source": "imdb",
                   "imdb_id": "", "updated_at": ""}]
        far = time.time() + 86400
        progs = [dict(title="Employee of the Month", channel_id="c", stop_ts=far + 7200, start_ts=far + shift,
                      channel="HBO", start_fmt="", **extra) for shift, extra in airings]
        index = {"movies": set(), "movie_versions": set(), "unyearred_movies": set(), "shows": set(),
                 "movie_counts": {}, "show_versions": set(), "unyearred_shows": set()}
        with mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
             mock.patch.dict(server._epg, {"programmes": progs}):
            data = server.app.test_client().get("/epg-web/api/recommendations").get_json()
        return data["recommendations"][0]["next_airing"]

    def test_an_airing_of_another_year_is_not_offered(self):
        self.assertIsNone(self._recs("2004", [(0, {"year": "2022"})]))

    def test_the_matching_year_is_offered(self):
        airing = self._recs("2004", [(0, {"year": "2022"}), (3600, {"year": "2004"})])
        self.assertEqual(airing["year"], "2004")

    def test_an_airing_with_no_year_is_still_offered(self):
        airing = self._recs("2004", [(0, {"year": ""})])
        self.assertIsNotNone(airing)


class FoldedTitleTests(unittest.TestCase):
    def test_accents_and_ligatures_fold_to_plain_letters(self):
        fold = server.fold_title
        self.assertEqual(fold("Æon Flux"), "aeonflux")
        self.assertEqual(fold("Aeon Flux"), "aeonflux")
        self.assertEqual(fold("Amélie"), fold("Amelie"))
        self.assertEqual(fold("Léon: The Professional"), fold("Leon The Professional"))
        self.assertEqual(fold("Crème Brûlée"), "cremebrulee")
        self.assertEqual(fold("Søren œuvre Straße"), "sorenoeuvrestrasse")
        self.assertEqual(fold(None), "")

    def test_plain_titles_are_unchanged(self):
        self.assertEqual(server.fold_title("The Matrix (1999)"), "thematrix1999")
        self.assertEqual(server.fold_title("Mad Max: Beyond Thunderdome"), "madmaxbeyondthunderdome")

    def test_the_provider_title_with_a_ligature_matches_the_plex_title(self):
        self.assertTrue(server._metadata_title_matches("Aeon Flux", "Æon Flux"))
        self.assertTrue(server._metadata_title_matches("Amelie", "Amélie"))
        self.assertFalse(server._metadata_title_matches("Wolves", "Dances with Wolves"))
        self.assertFalse(server._metadata_title_matches("", "Anything"))

    def test_plex_keys_ignore_accents(self):
        self.assertEqual(server._norm_plex_show("Æon Flux (2005)"), server._norm_plex_show("Aeon Flux"))

    def test_a_plex_folder_without_the_accent_is_found(self):
        with tempfile.TemporaryDirectory() as temp:
            os.makedirs(os.path.join(temp, "Aeon Flux (2005)"))
            os.makedirs(os.path.join(temp, "Heat (1995)"))
            found = server._find_plex_movie_folders(temp, "Æon Flux", "2005")
            self.assertEqual([os.path.basename(p) for p in found], ["Aeon Flux (2005)"])


class TmdbFallbackTests(unittest.TestCase):
    """The provider lookup finds a film stored with a special spelling, and survives a failed extra request."""

    def _lookup(self, title, search_results, credits_fails=False):
        import io

        def fake_urlopen(url, timeout=0):
            url = url if isinstance(url, str) else url.full_url
            if "omdbapi" in url:
                return io.BytesIO(json.dumps({"Response": "False", "Error": "Request limit reached!"}).encode())
            if "/search/" in url:
                return io.BytesIO(json.dumps({"results": search_results}).encode())
            if "/credits" in url:
                if credits_fails:
                    raise OSError("credits timed out")
                return io.BytesIO(json.dumps({"cast": [{"name": "Charlize Theron"}], "crew": []}).encode())
            raise AssertionError(url)
        cfg = {"omdb_key": "x", "tmdb_key": "y"}
        with mock.patch.object(server, "load_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch.object(server, "db_rows", return_value=[]):
            return server.app.test_client().get("/epg-web/api/prog-info?content_type=movie&title=" + title)

    RESULT = [{"id": 8202, "title": "Æon Flux", "release_date": "2005-12-01", "overview": "x"}]

    def test_a_ligature_title_is_found_through_tmdb(self):
        resp = self._lookup("Aeon%20Flux", self.RESULT)
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertEqual(data["year"], "2005")
        self.assertEqual(data["source"], "tmdb")

    def test_a_failed_credits_request_does_not_lose_the_year(self):
        resp = self._lookup("Aeon%20Flux", self.RESULT, credits_fails=True)
        self.assertEqual(resp.get_json()["year"], "2005")

    def test_a_different_film_is_still_refused(self):
        resp = self._lookup("Aeon%20Flux", [{"id": 1, "title": "Aeon Flux Returns", "release_date": "2010-01-01"}])
        self.assertEqual(resp.status_code, 404)

    def test_an_omdb_limit_is_written_to_the_log(self):
        with mock.patch("builtins.print") as printed:
            self._lookup("Aeon%20Flux", self.RESULT)
        logged = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("[OMDb] Request limit reached!", logged)


class EmptyPlexFolderTests(unittest.TestCase):
    def _library(self, temp):
        movies = os.path.join(temp, "Movies")
        for name, files in (("Heat (1995)", ["Heat.mp4"]), ("Minority Report (2002)", []),
                            ("Quantum of Solace (2008)", ["Quantum.convert.ffmpeg.log"]),
                            ("Sahara (2005)", ["Sahara.mp4.partial"]), ("Alien (1979)", ["Alien.MKV"]),
                            ("Nested (2001)", ["sub/Nested.mp4"])):
            for rel in files or [""]:
                path = os.path.join(movies, name, rel)
                if rel:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    open(path, "wb").close()
                else:
                    os.makedirs(path, exist_ok=True)
        os.makedirs(os.path.join(temp, "tv"))
        return movies

    def test_folder_has_video(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            has = lambda name: server._folder_has_video(os.path.join(movies, name))
            self.assertTrue(has("Heat (1995)"))
            self.assertTrue(has("Alien (1979)"))            # extension in capitals
            self.assertTrue(has("Nested (2001)"))           # one level down
            self.assertFalse(has("Minority Report (2002)"))  # empty
            self.assertFalse(has("Quantum of Solace (2008)"))   # only a log
            self.assertFalse(has("Sahara (2005)"))          # only an unfinished copy

    def test_an_unreadable_folder_is_assumed_to_have_a_film(self):
        self.assertTrue(server._folder_has_video("/no/such/folder"))

    def test_empty_folders_are_not_in_plex(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            server._plex_title_cache.update({"roots": None, "loaded_at": 0})
            with mock.patch.object(server, "load_config", return_value={"plex_path": movies, "plex_tv_path": temp + "/tv"}):
                idx = server._plex_wanted_title_index()
            n = server._norm_plex_show
            self.assertIn((n("Heat"), "1995"), idx["movie_versions"])
            self.assertIn((n("Nested"), "2001"), idx["movie_versions"])
            self.assertNotIn((n("Minority Report"), "2002"), idx["movie_versions"])
            self.assertNotIn(n("Quantum of Solace"), idx["movies"])
            self.assertNotIn(n("Sahara"), idx["movies"])
            self.assertNotIn(n("Sahara"), idx["movie_counts"])

    def test_the_title_list_for_the_browser_skips_empty_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            with mock.patch.object(server, "load_config", return_value={"plex_path": movies}):
                data = server.app.test_client().get("/epg-web/api/plex/titles").get_json()
            self.assertIn("Heat", data["titles"])
            self.assertNotIn("Minority Report", data["titles"])

    def test_plex_info_says_not_found_for_an_empty_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            server._plex_info_cache.clear()
            with mock.patch.object(server, "load_config", return_value={"plex_path": movies}):
                data = server.app.test_client().get("/epg-web/api/plex/info?title=Minority%20Report&year=2002").get_json()
            self.assertFalse(data["found"])

    def test_a_wanted_film_with_only_an_empty_folder_is_not_marked_in_plex(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = self._library(temp)
            server._plex_title_cache.update({"roots": None, "loaded_at": 0})
            wanted = [{"id": 1, "title": "Minority Report", "normalized_title": "minority report", "year": "2002",
                       "type": "movie", "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""}]
            with mock.patch.object(server, "load_config", return_value={"plex_path": movies, "plex_tv_path": temp + "/tv"}), \
                 mock.patch.object(server, "db_rows", return_value=wanted), \
                 mock.patch.dict(server._epg, {"programmes": []}):
                rec = server.app.test_client().get("/epg-web/api/recommendations").get_json()["recommendations"][0]
            self.assertFalse(rec["in_plex"])


class PrivateMoviesDashboardTests(unittest.TestCase):
    def test_the_private_path_default_and_override(self):
        self.assertEqual(server._private_movies_path({"plex_path": "/Volumes/Plex/Movies"}), "/Volumes/Plex/Movies Private")
        self.assertEqual(server._private_movies_path({"plex_path": "/x/Movies", "private_movies_path": "/y/Hidden"}), "/y/Hidden")

    def test_a_private_film_counts_as_in_the_library_for_duplicate_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            movies, private = os.path.join(temp, "Movies"), os.path.join(temp, "Movies Private")
            os.makedirs(os.path.join(movies, "Heat (1995)"))
            open(os.path.join(movies, "Heat (1995)", "Heat.mp4"), "wb").close()
            os.makedirs(os.path.join(private, "Minority Report (2002)"))
            open(os.path.join(private, "Minority Report (2002)", "m.mp4"), "wb").close()
            os.makedirs(os.path.join(temp, "tv"))
            server._plex_title_cache.update({"roots": None, "loaded_at": 0})
            with mock.patch.object(server, "load_config", return_value={"plex_path": movies, "plex_tv_path": temp + "/tv"}):
                idx = server._plex_wanted_title_index()
            n = server._norm_plex_show
            self.assertIn((n("Heat"), "1995"), idx["movie_versions"])
            self.assertIn((n("Minority Report"), "2002"), idx["movie_versions"])

    def test_a_missing_private_folder_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            movies = os.path.join(temp, "Movies")
            os.makedirs(os.path.join(movies, "Heat (1995)"))
            open(os.path.join(movies, "Heat (1995)", "Heat.mp4"), "wb").close()
            os.makedirs(os.path.join(temp, "tv"))
            server._plex_title_cache.update({"roots": None, "loaded_at": 0})
            with mock.patch.object(server, "load_config", return_value={"plex_path": movies, "plex_tv_path": temp + "/tv"}):
                idx = server._plex_wanted_title_index()
            self.assertEqual(len(idx["movie_versions"]), 1)


class PrivateRecordingTests(unittest.TestCase):
    CHANNEL = "pv-chan-1"

    def setUp(self):
        with server._rec_lock:
            server._recs.clear()
            server._rec_cancel_events.clear()
        server._init_recordings_table()
        conn = sqlite3.connect(server._guide_db_path())
        conn.execute("DELETE FROM recordings")
        conn.execute("DELETE FROM private_titles")
        conn.commit()
        conn.close()
        self.start = time.time() + 7200

    def _record(self, **extra):
        body = {"title": "Private Film", "channel_id": self.CHANNEL, "channel_name": "Chan",
                "start_ts": self.start, "stop_ts": self.start + 6000, "year": "2004"}
        body.update(extra)
        with mock.patch.object(server, "_recording_backend", return_value="agent"), \
             mock.patch.object(server, "_resolve_recording_source",
                               return_value=("http://x/1.ts", None, {"stream_id": "1", "provider": "primestreams"})):
            return server.app.test_client().post("/epg-web/api/record", json=body)

    def _saved(self):
        conn = sqlite3.connect(server._guide_db_path())
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM recordings ORDER BY created_at DESC LIMIT 1").fetchone()
        conn.close()
        return row

    def _mark(self, private, title="Private Film", year="2004"):
        return server.app.test_client().post("/epg-web/api/wanted/private",
                                             json={"title": title, "year": year, "private": private})

    def test_a_recording_asked_to_be_private_is_saved_private_and_sent_to_the_recorder(self):
        self.assertEqual(self._record(private=True).status_code, 200)
        row = self._saved()
        self.assertEqual(row["private"], 1)
        self.assertTrue(server._agent_job_dict(row)["private"])

    def test_a_plain_recording_is_not_private(self):
        self._record()
        row = self._saved()
        self.assertEqual(row["private"], 0)
        self.assertFalse(server._agent_job_dict(row)["private"])

    def test_a_wanted_film_marked_private_records_privately_without_being_asked(self):
        self.assertTrue(self._mark(True).get_json()["ok"])
        self._record()
        self.assertEqual(self._saved()["private"], 1)

    def test_marking_it_public_again_stops_that(self):
        self._mark(True)
        self._mark(False)
        self._record()
        self.assertEqual(self._saved()["private"], 0)

    def test_privacy_belongs_to_the_year_not_just_the_title(self):
        self._mark(True, year="2022")
        self._record(year="2004")
        self.assertEqual(self._saved()["private"], 0)

    def test_the_mark_needs_a_title_and_a_year(self):
        self.assertEqual(self._mark(True, year="").status_code, 400)
        self.assertEqual(self._mark(True, title="").status_code, 400)

    def test_accents_do_not_change_which_film_is_private(self):
        self._mark(True, title="Æon Flux", year="2005")
        self.assertTrue(server._is_private_title("Aeon Flux", "2005"))

    def test_a_tv_episode_is_never_private(self):
        self._record(private=True, season_num=1, episode_num=2, episode_title="Pilot")
        row = self._saved()
        self.assertEqual(row["private"], 0)

    def test_recommendations_show_which_films_are_private(self):
        self._mark(True, title="Private Film", year="2004")
        wanted = [{"id": 1, "title": "Private Film", "normalized_title": "private film", "year": "2004",
                   "type": "movie", "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""},
                  {"id": 2, "title": "Other Film", "normalized_title": "other film", "year": "1999",
                   "type": "movie", "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""}]
        index = {"movies": set(), "movie_versions": set(), "unyearred_movies": set(), "shows": set(),
                 "movie_counts": {}, "show_versions": set(), "unyearred_shows": set()}
        with mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
             mock.patch.dict(server._epg, {"programmes": []}):
            recs = server.app.test_client().get("/epg-web/api/recommendations").get_json()["recommendations"]
        self.assertEqual([r["private"] for r in recs], [True, False])

    def test_the_page_has_the_private_checkbox_and_toggle(self):
        self.assertIn('id="pm-private"', server.HTML)
        self.assertIn("togglePrivateWanted", server.HTML)
        self.assertIn("/epg-web/api/wanted/private", server.HTML)


class RerecordChannelTests(unittest.TestCase):
    def test_only_commercial_free_english_channels_qualify(self):
        ok = server._is_rerecord_channel
        self.assertTrue(ok("HBO"))
        self.assertTrue(ok("Showtime East HD"))
        self.assertTrue(ok("MGM+ Hits HD"))
        for name in ("Flix", "FLIX HD", "TCM (SD)", "The Movie Channel East", "TMC Xtra", "Crave1",
                     "SHO 2 HD", "SKY CINEMA FAMILY | VIP", "Starz Kids & Family HD",
                     "CINEMAX CLASSICS", "Hollywood Suite 70s"):
            self.assertTrue(ok(name), name)
        for name in ("AMC", "Hallmark Movies", "FXM", "Freeform", "Showcase"):
            self.assertFalse(ok(name), name)
        self.assertFalse(ok("ANTENNA TV"))                 # ad-supported
        self.assertFalse(ok("Paramount Network HD"))       # ad-supported
        self.assertFalse(ok("HBO Latino"))                 # premium, but a Spanish feed
        self.assertFalse(ok("Showtime Español"))
        self.assertFalse(ok(""))
        self.assertFalse(ok(None))

    def _recs(self, in_plex, airings):
        wanted = [{"id": 1, "title": "Heat", "normalized_title": "heat", "year": "1995", "type": "movie",
                   "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""}]
        far = time.time() + 86400
        progs = [dict(title="Heat", channel_id="c", channel=channel, stop_ts=far + 7200, start_ts=far + shift, start_fmt="")
                 for shift, channel in airings]
        key = server._norm_plex_show("Heat")
        index = {"movies": {key} if in_plex else set(), "movie_versions": {(key, "1995")} if in_plex else set(),
                 "unyearred_movies": set(), "shows": set(), "movie_counts": {}, "show_versions": set(),
                 "unyearred_shows": set()}
        with mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
             mock.patch.dict(server._epg, {"programmes": progs}):
            data = server.app.test_client().get("/epg-web/api/recommendations").get_json()
        return data["recommendations"][0]["next_airing"]

    def test_a_film_we_have_is_offered_again_only_from_a_commercial_free_english_channel(self):
        airing = self._recs(True, [(0, "ANTENNA TV"), (60, "HBO Latino"), (120, "HBO")])
        self.assertEqual(airing["channel"], "HBO")

    def test_a_film_we_have_gets_no_offer_when_only_ad_supported_airings_exist(self):
        self.assertIsNone(self._recs(True, [(0, "ANTENNA TV"), (60, "Paramount Network HD")]))

    def test_a_film_we_do_not_have_is_offered_only_from_a_commercial_free_channel_too(self):
        airing = self._recs(False, [(0, "AMC"), (60, "ANTENNA TV"), (120, "HBO")])
        self.assertEqual(airing["channel"], "HBO")
        self.assertIsNone(self._recs(False, [(0, "AMC"), (60, "ANTENNA TV")]))

    def test_a_film_that_airs_only_with_ads_says_so(self):
        wanted = [{"id": 1, "title": "The Green Mile", "normalized_title": "the green mile", "year": "1999",
                   "type": "movie", "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""},
                  {"id": 2, "title": "Not Aired", "normalized_title": "not aired", "year": "1999",
                   "type": "movie", "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""}]
        far = time.time() + 86400
        progs = [dict(title="The Green Mile", channel_id="c", channel="AMC", stop_ts=far + 7200,
                      start_ts=far, start_fmt="")]
        index = {"movies": set(), "movie_versions": set(), "unyearred_movies": set(), "shows": set(),
                 "movie_counts": {}, "show_versions": set(), "unyearred_shows": set()}
        with mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
             mock.patch.dict(server._epg, {"programmes": progs}):
            recs = server.app.test_client().get("/epg-web/api/recommendations").get_json()["recommendations"]
        self.assertIsNone(recs[0]["next_airing"])
        self.assertTrue(recs[0]["ad_supported_only"])
        self.assertFalse(recs[1]["ad_supported_only"])
        self.assertIn("Only on channels with ads", server.HTML)

    def test_a_series_is_still_offered_from_any_channel(self):
        wanted = [{"id": 1, "title": "Doug", "normalized_title": "doug", "year": "1991", "type": "series",
                   "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""}]
        far = time.time() + 86400
        progs = [dict(title="Doug", channel_id="c", channel="Nickelodeon", stop_ts=far + 1800,
                      start_ts=far, start_fmt="")]
        index = {"movies": set(), "movie_versions": set(), "unyearred_movies": set(), "shows": set(),
                 "movie_counts": {}, "show_versions": set(), "unyearred_shows": set()}
        with mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
             mock.patch.dict(server._epg, {"programmes": progs}):
            recs = server.app.test_client().get("/epg-web/api/recommendations").get_json()["recommendations"]
        self.assertEqual(recs[0]["next_airing"]["channel"], "Nickelodeon")

    def test_the_page_only_shows_check_upgrade_when_there_is_an_airing(self):
        self.assertIn('(a ? `<button class="btn btn-ghost btn-sm" title="Next commercial-free English airing', server.HTML)


class WestFeedYearTests(unittest.TestCase):
    """A West feed airs a film three hours after the East listing; the year still reaches it."""

    def _programmes(self, rows):
        db = os.path.join(TEST_DATA_DIR, "guide.db")
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM guide WHERE channel_id LIKE 'wf-%' OR channel_id LIKE 'ld-%'")   # other tests' rows
        for title, channel, start, year, ptype in rows:
            conn.execute("INSERT INTO guide(title, channel_id, channel_name, start_utc, end_utc, desc, category, prog_type, year) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (title, channel, channel, start, "20990101200000", "", "", ptype, year))
        conn.commit()
        conn.close()
        server.load_epg_from_db(db)
        return {p["channel_id"]: p for p in server._epg["programmes"] if p["channel_id"].startswith("wf-")}

    def test_the_year_reaches_the_west_feed_three_hours_later(self):
        progs = self._programmes([
            ("Grumpier Old Men", "wf-sd-east", "20990101000000", "1995", "MV"),
            ("Grumpier Old Men", "wf-prov-west", "20990101030000", "", ""),
        ])
        self.assertEqual(progs["wf-prov-west"]["year"], "1995")

    def test_it_reaches_an_earlier_feed_too(self):
        progs = self._programmes([
            ("Grumpier Old Men", "wf-sd-west", "20990101030000", "1995", "MV"),
            ("Grumpier Old Men", "wf-prov-east", "20990101000000", "", ""),
        ])
        self.assertEqual(progs["wf-prov-east"]["year"], "1995")

    def test_some_other_time_does_not_match(self):
        progs = self._programmes([
            ("Grumpier Old Men", "wf-sd-east", "20990101000000", "1995", "MV"),
            ("Grumpier Old Men", "wf-prov-late", "20990101060000", "", ""),
        ])
        self.assertEqual(progs["wf-prov-late"]["year"], "")

    def test_a_match_at_the_same_time_wins_over_a_shifted_one(self):
        progs = self._programmes([
            ("Employee of the Month", "wf-sd-a", "20990101000000", "2004", "MV"),
            ("Employee of the Month", "wf-sd-b", "20990101030000", "2022", "MV"),
            ("Employee of the Month", "wf-prov", "20990101000500", "", ""),
        ])
        self.assertEqual(progs["wf-prov"]["year"], "2004")

    def test_two_films_only_at_the_shifted_times_are_not_guessed(self):
        progs = self._programmes([
            ("Employee of the Month", "wf-sd-a", "20990101000000", "2004", "MV"),
            ("Employee of the Month", "wf-sd-b", "20990101060000", "2022", "MV"),
            ("Employee of the Month", "wf-prov", "20990101030000", "", ""),
        ])
        self.assertEqual(progs["wf-prov"]["year"], "")

    def test_the_record_check_finds_the_year_across_the_three_hour_shift(self):
        conn = sqlite3.connect(server._guide_db_path())
        conn.execute("DELETE FROM guide WHERE channel_id LIKE 'wf-%'")
        conn.execute("INSERT INTO guide(title, channel_id, channel_name, start_utc, end_utc, desc, category, prog_type, year) "
                     "VALUES ('Shifted Film','wf-sd','East','20990301000000','20990301020000','','','MV','1995')")
        conn.commit()
        conn.close()
        west = time.mktime(time.strptime("20990301030000", "%Y%m%d%H%M%S")) - time.timezone      # 03:00 UTC
        found = server._guide_year_for_airing(server._guide_db_path(), "wf-west", "Shifted Film", west)
        self.assertEqual(found, "1995")
        none = server._guide_year_for_airing(server._guide_db_path(), "wf-west", "Shifted Film", west + 6 * 3600)
        self.assertEqual(none, "")


class FilmYearByTitleTests(unittest.TestCase):
    """A film with no year from the guide: look it up by title, but only when that is certain."""

    def setUp(self):
        server._film_year_cache.clear()

    def _urlopen(self, tmdb_results=None, runtimes=None, omdb=None, calls=None, fail_tmdb=False):
        import io
        runtimes = runtimes or {}

        def fake(url, timeout=0):
            url = url if isinstance(url, str) else url.full_url
            if calls is not None:
                calls.append(url)
            if "themoviedb.org/3/search" in url:
                if fail_tmdb:
                    raise OSError("tmdb down")
                return io.BytesIO(json.dumps({"results": tmdb_results or []}).encode())
            if "themoviedb.org/3/movie/" in url:
                ident = int(url.split("/movie/")[1].split("?")[0])
                return io.BytesIO(json.dumps({"runtime": runtimes.get(ident, 0)}).encode())
            if "omdbapi.com" in url:
                return io.BytesIO(json.dumps(omdb or {"Response": "False", "Error": "Movie not found!"}).encode())
            raise AssertionError(url)
        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def _lookup(self, title, minutes, **kw):
        with mock.patch.object(server, "load_config", return_value={"tmdb_key": "t", "omdb_key": "o"}), \
             self._urlopen(**kw):
            return server._film_year_by_title(title, minutes)

    def test_one_film_with_that_title_gives_its_year(self):
        results = [{"id": 1, "title": "Aeon Flux", "release_date": "2005-12-01"}]
        self.assertEqual(self._lookup("Aeon Flux", 93, tmdb_results=results), "2005")

    def test_a_longer_title_is_not_taken(self):
        results = [{"id": 1, "title": "Aeon Flux Returns", "release_date": "2010-01-01"}]
        self.assertEqual(self._lookup("Aeon Flux", 93, tmdb_results=results), "")

    def test_the_special_spelling_matches(self):
        results = [{"id": 8202, "title": "Æon Flux", "release_date": "2005-12-01"}]
        self.assertEqual(self._lookup("Aeon Flux", 93, tmdb_results=results), "2005")

    def test_two_films_are_told_apart_by_running_time(self):
        results = [{"id": 10, "title": "Employee of the Month", "release_date": "2004-10-08"},
                   {"id": 20, "title": "Employee of the Month", "release_date": "2022-01-01"}]
        runtimes = {10: 103, 20: 88}
        self.assertEqual(self._lookup("Employee of the Month", 104, tmdb_results=results, runtimes=runtimes), "2004")
        self.assertEqual(self._lookup("Employee of the Month ", 88, tmdb_results=results, runtimes=runtimes), "2022")

    def test_two_films_of_a_similar_length_are_not_guessed(self):
        results = [{"id": 10, "title": "Arthur", "release_date": "1981-09-25"},
                   {"id": 20, "title": "Arthur", "release_date": "2011-04-08"}]
        self.assertEqual(self._lookup("Arthur", 108, tmdb_results=results, runtimes={10: 97, 20: 110}), "")

    def test_two_films_with_no_airing_length_are_not_guessed(self):
        results = [{"id": 10, "title": "Arthur", "release_date": "1981-09-25"},
                   {"id": 20, "title": "Arthur", "release_date": "2011-04-08"}]
        self.assertEqual(self._lookup("Arthur", 0, tmdb_results=results), "")

    def test_omdb_is_used_when_tmdb_has_nothing(self):
        omdb = {"Search": [{"Title": "Heat", "Year": "1995", "imdbID": "tt1"}, {"Title": "Heat Wave", "Year": "1990", "imdbID": "tt2"}]}
        self.assertEqual(self._lookup("Heat", 170, omdb=omdb), "1995")

    def test_a_failing_service_gives_no_year_and_no_error(self):
        self.assertEqual(self._lookup("Heat", 170, fail_tmdb=True), "")

    def test_an_answer_is_remembered(self):
        calls = []
        results = [{"id": 1, "title": "Aeon Flux", "release_date": "2005-12-01"}]
        self.assertEqual(self._lookup("Aeon Flux", 93, tmdb_results=results, calls=calls), "2005")
        n = len(calls)
        self.assertEqual(self._lookup("Aeon Flux", 93, tmdb_results=results, calls=calls), "2005")
        self.assertEqual(len(calls), n)

    def test_nothing_found_is_not_asked_again_straight_away(self):
        calls = []
        self.assertEqual(self._lookup("Nonexistent Film", 90, calls=calls), "")
        n = len(calls)
        self.assertEqual(self._lookup("Nonexistent Film", 90, calls=calls), "")
        self.assertEqual(len(calls), n)


class RecordUsesTitleLookupTests(unittest.TestCase):
    CHANNEL = "tl-chan-1"

    def setUp(self):
        with server._rec_lock:
            server._recs.clear()
            server._rec_cancel_events.clear()
        server._init_recordings_table()
        conn = sqlite3.connect(server._guide_db_path())
        conn.execute("DELETE FROM recordings")
        conn.execute("DELETE FROM guide WHERE channel_id LIKE 'tl-%'")
        conn.commit()
        conn.close()
        self.start = time.time() + 7200

    def _record(self, minutes=110, lookup="1995", **extra):
        body = {"title": "Lookup Film", "channel_id": self.CHANNEL, "channel_name": "Chan",
                "start_ts": self.start, "stop_ts": self.start + minutes * 60}
        body.update(extra)
        with mock.patch.object(server, "_recording_backend", return_value="agent"), \
             mock.patch.object(server, "_resolve_recording_source",
                               return_value=("http://x/1.ts", None, {"stream_id": "1", "provider": "primestreams"})), \
             mock.patch.object(server, "_film_year_by_title", return_value=lookup) as looked:
            resp = server.app.test_client().post("/epg-web/api/record", json=body)
        conn = sqlite3.connect(server._guide_db_path())
        row = conn.execute("SELECT year FROM recordings ORDER BY created_at DESC LIMIT 1").fetchone()
        conn.close()
        return resp, (row[0] if row else None), looked

    def test_a_film_with_no_year_is_looked_up_and_saved_with_the_year(self):
        resp, year, looked = self._record()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(year, "1995")
        self.assertEqual(looked.call_args.args[0], "Lookup Film")
        self.assertAlmostEqual(looked.call_args.args[1], 110, delta=1)

    def test_an_uncertain_lookup_records_without_a_year(self):
        _resp, year, _looked = self._record(lookup="")
        self.assertEqual(year, "")

    def test_a_year_from_the_caller_is_not_looked_up_or_overridden(self):
        _resp, year, looked = self._record(year="2004")
        self.assertEqual(year, "2004")
        looked.assert_not_called()

    def test_a_year_from_the_guide_is_not_looked_up(self):
        key = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.start))
        conn = sqlite3.connect(server._guide_db_path())
        conn.execute("INSERT INTO guide(title, channel_id, channel_name, start_utc, end_utc, desc, category, prog_type, year) "
                     "VALUES ('Lookup Film', ?, 'Chan', ?, ?, '', '', 'MV', '1988')", (self.CHANNEL, key, key))
        conn.commit()
        conn.close()
        _resp, year, looked = self._record()
        self.assertEqual(year, "1988")
        looked.assert_not_called()

    def test_a_short_programme_is_not_looked_up(self):
        _resp, year, looked = self._record(minutes=30)
        looked.assert_not_called()

    def test_a_tv_episode_is_not_looked_up_as_a_film(self):
        _resp, _year, looked = self._record(season_num=1, episode_num=2, episode_title="Pilot")
        looked.assert_not_called()


class FilmSignalsTests(unittest.TestCase):
    def setUp(self):
        server._film_signals_cache.clear()

    def _get(self, query, search, details=None, tmdb_key="t", calls=None):
        import io

        def fake(url, timeout=0):
            url = url if isinstance(url, str) else url.full_url
            if calls is not None:
                calls.append(url)
            if "/search/movie" in url:
                return io.BytesIO(json.dumps({"results": search}).encode())
            if "/movie/" in url:
                return io.BytesIO(json.dumps(details or {}).encode())
            raise AssertionError(url)
        with mock.patch.object(server, "load_config", return_value={"tmdb_key": tmdb_key}), \
             mock.patch("urllib.request.urlopen", side_effect=fake):
            return server.app.test_client().get("/epg-web/api/film-signals?" + query)

    SEARCH = [{"id": 5, "title": "Some Film", "release_date": "1998-03-01"},
              {"id": 6, "title": "Some Film", "release_date": "2010-01-01"}]
    DETAILS = {"title": "Some Film", "release_date": "1998-03-01", "adult": False,
               "genres": [{"name": "Thriller"}, {"name": "Drama"}],
               "release_dates": {"results": [
                   {"iso_3166_1": "GB", "release_dates": [{"certification": "18"}]},
                   {"iso_3166_1": "US", "release_dates": [{"certification": ""}, {"certification": "NC-17"}]}]},
               "keywords": {"keywords": [{"name": "erotic thriller"}, {"name": "softcore"}]}}

    def test_it_returns_the_flag_rating_genres_and_keywords(self):
        data = self._get("title=Some%20Film&year=1998", self.SEARCH, self.DETAILS).get_json()
        self.assertTrue(data["found"])
        self.assertEqual(data["id"], 5)
        self.assertEqual(data["year"], "1998")
        self.assertFalse(data["adult"])
        self.assertEqual(data["certification"], "NC-17")          # the US rating, skipping an empty one
        self.assertEqual(data["genres"], ["Thriller", "Drama"])
        self.assertEqual(data["keywords"], ["erotic thriller", "softcore"])

    def test_the_year_picks_between_films_with_one_title(self):
        data = self._get("title=Some%20Film&year=2010", self.SEARCH, dict(self.DETAILS, release_date="2010-01-01")).get_json()
        self.assertEqual(data["id"], 6)

    def test_no_match_for_the_year_is_not_found(self):
        self.assertFalse(self._get("title=Some%20Film&year=1975", self.SEARCH, self.DETAILS).get_json()["found"])

    def test_a_longer_title_is_not_taken(self):
        search = [{"id": 9, "title": "Some Film Returns", "release_date": "1998-03-01"}]
        self.assertFalse(self._get("title=Some%20Film", search, self.DETAILS).get_json()["found"])

    def test_the_adult_flag_comes_through(self):
        data = self._get("title=Some%20Film", self.SEARCH[:1], dict(self.DETAILS, adult=True)).get_json()
        self.assertTrue(data["adult"])

    def test_an_answer_is_remembered(self):
        calls = []
        self._get("title=Some%20Film&year=1998", self.SEARCH, self.DETAILS, calls=calls)
        n = len(calls)
        self._get("title=Some%20Film&year=1998", self.SEARCH, self.DETAILS, calls=calls)
        self.assertEqual(len(calls), n)

    def test_no_key_and_no_title_are_explained(self):
        self.assertEqual(self._get("title=Some%20Film", [], tmdb_key="").status_code, 503)
        self.assertEqual(self._get("title=", []).status_code, 400)


class FilmYearsEndpointTests(unittest.TestCase):
    def setUp(self):
        server._film_years_cache.clear()

    def _get(self, query, search, runtimes=None, calls=None):
        import io
        runtimes = runtimes or {}

        def fake(url, timeout=0):
            url = url if isinstance(url, str) else url.full_url
            if calls is not None:
                calls.append(url)
            if "/search/movie" in url:
                return io.BytesIO(json.dumps({"results": search}).encode())
            if "themoviedb.org/3/movie/" in url:
                ident = int(url.split("/movie/")[1].split("?")[0])
                return io.BytesIO(json.dumps({"runtime": runtimes.get(ident, 0)}).encode())
            raise AssertionError(url)
        with mock.patch.object(server, "load_config", return_value={"tmdb_key": "t"}), \
             mock.patch("urllib.request.urlopen", side_effect=fake):
            return server.app.test_client().get("/epg-web/api/film-years?" + query)

    SEARCH = [{"id": 1, "title": "Arthur", "release_date": "2011-04-08"},
              {"id": 2, "title": "Arthur", "release_date": "1981-09-25"},
              {"id": 3, "title": "Arthur Christmas", "release_date": "2011-11-23"}]

    def test_it_lists_the_films_with_exactly_that_title_by_year(self):
        data = self._get("title=Arthur", self.SEARCH).get_json()
        self.assertEqual([c["year"] for c in data["candidates"]], ["1981", "2011"])      # not "Arthur Christmas"

    def test_runtime_is_added_when_asked_for(self):
        data = self._get("title=Arthur&runtime=1", self.SEARCH, runtimes={1: 110, 2: 97}).get_json()
        self.assertEqual([(c["year"], c["runtime"]) for c in data["candidates"]], [("1981", 97), ("2011", 110)])

    def test_no_runtime_means_no_extra_requests(self):
        calls = []
        self._get("title=Arthur", self.SEARCH, calls=calls)
        self.assertEqual(len(calls), 1)

    def test_a_title_with_no_match_gives_an_empty_list_and_no_title_is_refused(self):
        self.assertEqual(self._get("title=Nothing%20Here", []).get_json()["candidates"], [])
        self.assertEqual(self._get("title=", []).status_code, 400)

    def test_an_answer_is_remembered(self):
        calls = []
        self._get("title=Arthur", self.SEARCH, calls=calls)
        n = len(calls)
        self._get("title=Arthur", self.SEARCH, calls=calls)
        self.assertEqual(len(calls), n)


class AmpersandAndLooseTitleTests(unittest.TestCase):
    def setUp(self):
        server._film_signals_cache.clear()

    def test_an_ampersand_is_and(self):
        self.assertEqual(server.fold_title("Dumb & Dumber"), server.fold_title("Dumb and Dumber"))
        self.assertEqual(server.fold_title("Ex-Husbands & Wives"), "exhusbandsandwives")
        self.assertTrue(server._metadata_title_matches("Dumb & Dumber", "Dumb and Dumber"))
        self.assertEqual(server._norm_plex_show("Dumb & Dumber (1994)"), server._norm_plex_show("Dumb and Dumber"))

    def test_loose_title_rules(self):
        loose = server._loose_title_match
        self.assertTrue(loose(server.fold_title("Superman The Movie"), {"title": "Superman"}))
        self.assertTrue(loose(server.fold_title("Superman"), {"title": "Superman The Movie"}))
        self.assertFalse(loose(server.fold_title("It"), {"title": "It Follows"}))              # too short for a loose match
        self.assertFalse(loose(server.fold_title("Heat"), {"title": "Heat Wave"}))
        self.assertFalse(loose(server.fold_title("Jaws III"), {"title": "Jaws 3-D"}))          # not close enough: left alone

    def _signals(self, query, search, details=None):
        import io

        def fake(url, timeout=0):
            url = url if isinstance(url, str) else url.full_url
            if "/search/movie" in url:
                return io.BytesIO(json.dumps({"results": search}).encode())
            return io.BytesIO(json.dumps(details or {"title": search[0]["title"], "release_date": search[0]["release_date"],
                                                      "adult": False, "genres": [], "keywords": {"keywords": []}}).encode())
        with mock.patch.object(server, "load_config", return_value={"tmdb_key": "t"}), \
             mock.patch("urllib.request.urlopen", side_effect=fake):
            return server.app.test_client().get("/epg-web/api/film-signals?" + query).get_json()

    def test_a_close_title_in_the_same_year_is_found_and_marked_loose(self):
        data = self._signals("title=Superman%20The%20Movie&year=1978",
                             [{"id": 1, "title": "Superman", "release_date": "1978-12-15"}])
        self.assertTrue(data["found"])
        self.assertEqual(data["match"], "loose")
        self.assertEqual(data["title"], "Superman")

    def test_a_close_title_is_accepted_one_year_off_but_not_two(self):
        film = [{"id": 1, "title": "Superman", "release_date": "1978-12-15"}]
        self.assertTrue(self._signals("title=Superman%20The%20Movie&year=1979", film)["found"])
        self.assertFalse(self._signals("title=Superman%20The%20Movie&year=1981", film)["found"])

    def test_a_close_title_needs_a_year(self):
        film = [{"id": 1, "title": "Superman", "release_date": "1978-12-15"}]
        self.assertFalse(self._signals("title=Superman%20The%20Movie", film)["found"])

    def test_an_exact_match_is_preferred_and_marked_exact(self):
        film = [{"id": 1, "title": "Dumb and Dumber", "release_date": "1994-12-16"}]
        data = self._signals("title=Dumb%20%26%20Dumber&year=1994", film)
        self.assertTrue(data["found"])
        self.assertEqual(data["match"], "exact")


class FetchSdProgressTests(unittest.TestCase):
    def _wait_until_idle(self):
        deadline = time.time() + 10
        while time.time() < deadline and server._sd_status["running"]:
            time.sleep(0.02)

    def test_the_fetch_reports_its_slow_steps_and_the_loading_progress(self):
        seen = {}

        def fake_fetch(user, password, db, days=21, log=print):
            log("Writing to guide.db…")
            log("Done — 5 new rows inserted (0 superseded rows replaced).")
            return {"inserted": 5}

        def fake_load(db, tz="America/New_York", progress=None, **kwargs):
            progress("Reading guide listings", 25000, 100000)
            seen["during"] = (list(server._sd_status["log"]), dict(server._sd_status["load"]))
            return 100000
        cfg = {"sd_user": "u", "sd_pass": "p", "guide_db_path": os.path.join(TEST_DATA_DIR, "guide.db")}
        with mock.patch.object(server, "load_config", return_value=cfg), \
             mock.patch("sd_guide.fetch_sd_guide", side_effect=fake_fetch), \
             mock.patch.object(server, "load_epg_from_db", side_effect=fake_load), \
             mock.patch.object(server, "_schedule_active_series"):
            self.assertTrue(server.app.test_client().post("/epg-web/api/fetch-sd", json={"days": 1}).get_json()["ok"])
            self._wait_until_idle()
        log, load = seen["during"]
        self.assertEqual(log[-1], "Loading the guide into memory…")           # the bar can say what is happening
        self.assertEqual(load, {"label": "Reading guide listings", "done": 25000, "total": 100000})
        final = server._sd_status
        self.assertFalse(final["running"])
        self.assertIsNone(final["load"])
        self.assertEqual(final["log"][-1], "Checking recurring recordings…")
        self.assertEqual(final["result"]["total_loaded"], 100000)
        self.assertIsNone(final["error"])

    def test_the_status_carries_the_load_field(self):
        self.assertIn("load", server.app.test_client().get("/epg-web/api/fetch-sd/status").get_json())

    def test_an_error_clears_the_loading_progress(self):
        cfg = {"sd_user": "u", "sd_pass": "p", "guide_db_path": os.path.join(TEST_DATA_DIR, "guide.db")}
        with mock.patch.object(server, "load_config", return_value=cfg), \
             mock.patch("sd_guide.fetch_sd_guide", side_effect=RuntimeError("down")):
            server.app.test_client().post("/epg-web/api/fetch-sd", json={"days": 1})
            self._wait_until_idle()
        self.assertEqual(server._sd_status["error"], "down")
        self.assertIsNone(server._sd_status["load"])
        self.assertFalse(server._sd_status["running"])


class SdProgressBarMappingTests(unittest.TestCase):
    """The bar's percentage for each real step, run in node (skipped where node is missing)."""

    def _percent(self, log, load=None):
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        match = re.search(r"function sdProgressFromLog\(log, load\) \{.*?\n\}\n", server.HTML, re.S)
        self.assertIsNotNone(match)
        script = match.group(0) + f"\nconsole.log(sdProgressFromLog({json.dumps(log)}, {json.dumps(load)}));"
        return int(subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30).stdout.strip())

    def test_each_step_has_its_place_and_the_order_never_goes_backwards(self):
        steps = ["Authenticating with Schedules Direct…", "Fetching lineups…", "Found 1 lineup(s): ['x']",
                 "Getting stations for x…", "Total stations: 900", "Fetching schedules batch 3…",
                 "Unique programs to fetch: 90000", "Fetching program details 1/180…", "Fetching program details 90/180…",
                 "Fetching program details 180/180…", "Writing to guide.db…",
                 "Removed 5 superseded Schedule Direct listings.",
                 "Done — 427068 new rows inserted (427068 superseded rows replaced).",
                 "Loading the guide into memory…", "Checking recurring recordings…"]
        values = [self._percent(step) for step in steps]
        self.assertEqual(values, sorted(values), values)
        self.assertGreater(len(set(values)), 10)

    def test_the_done_line_is_not_mistaken_for_importing(self):
        self.assertEqual(self._percent("Done — 427068 new rows inserted (427068 superseded rows replaced)."), 88)
        self.assertGreater(self._percent("Done — 1 new rows inserted (0 superseded rows replaced)."), 72)

    def test_loading_the_guide_moves_the_bar_as_it_goes(self):
        self.assertEqual(self._percent("Loading the guide into memory…"), 90)
        self.assertEqual(self._percent("Loading the guide into memory…", {"done": 0, "total": 100}), 90)
        self.assertEqual(self._percent("Loading the guide into memory…", {"done": 50, "total": 100}), 93)
        self.assertEqual(self._percent("Loading the guide into memory…", {"done": 100, "total": 100}), 96)
        self.assertEqual(self._percent("Checking recurring recordings…"), 97)

    def test_program_details_use_their_own_fraction(self):
        self.assertEqual(self._percent("Fetching program details 90/180…"), 60)
        self.assertEqual(self._percent("Fetching program details 180/180…"), 80)


class WantedChoicesFallbackTests(unittest.TestCase):
    def _get(self, query, omdb, tmdb_search=None, tmdb_details=None, cfg=None, omdb_raises=False):
        import io

        def fake(url, timeout=0):
            url = url if isinstance(url, str) else url.full_url
            if "omdbapi.com" in url:
                if omdb_raises:
                    raise OSError("omdb down")
                if "?s=" in url:
                    return io.BytesIO(json.dumps(omdb).encode())
                return io.BytesIO(json.dumps({"Response": "True", "Title": "From OMDb", "Year": "1999", "imdbID": "ttO"}).encode())
            if "themoviedb.org/3/search" in url:
                return io.BytesIO(json.dumps({"results": tmdb_search or []}).encode())
            if "themoviedb.org/3/" in url:
                return io.BytesIO(json.dumps(tmdb_details or {}).encode())
            raise AssertionError(url)
        cfg = {"omdb_key": "o", "tmdb_key": "t"} if cfg is None else cfg
        with mock.patch.object(server, "load_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", side_effect=fake):
            return server.app.test_client().get("/epg-web/api/wanted/movie-matches?" + query)

    TOMMY = [{"id": 11, "title": "Tommy", "release_date": "1975-03-19", "poster_path": "/p.jpg",
              "overview": "A deaf, dumb and blind boy...", "vote_average": 6.6},
             {"id": 12, "title": "Tommy Boy", "release_date": "1995-03-31", "overview": "x", "vote_average": 6.5}]
    DETAILS = {"imdb_id": "tt0073891", "runtime": 111, "genres": [{"name": "Drama"}, {"name": "Music"}],
               "credits": {"cast": [{"name": "Roger Daltrey"}, {"name": "Ann-Margret"}],
                           "crew": [{"job": "Director", "name": "Ken Russell"}, {"job": "Editor", "name": "X"}]}}

    def test_omdb_not_found_falls_back_to_tmdb(self):
        data = self._get("q=Tommy&type=movie", {"Response": "False", "Error": "Movie not found!"}, self.TOMMY, self.DETAILS).get_json()
        self.assertEqual(data["source"], "tmdb")
        first = data["matches"][0]
        self.assertEqual((first["title"], first["year"], first["imdb_id"]), ("Tommy", "1975", "tt0073891"))
        self.assertEqual(first["runtime"], "111 min")
        self.assertEqual(first["director"], "Ken Russell")
        self.assertIn("Roger Daltrey", first["actors"])
        self.assertTrue(first["poster"].endswith("/p.jpg"))
        self.assertEqual([m["title"] for m in data["matches"]], ["Tommy", "Tommy Boy"])        # the exact title first

    def test_omdb_rate_limit_also_falls_back(self):
        data = self._get("q=Tommy&type=movie", {"Response": "False", "Error": "Request limit reached!"}, self.TOMMY, self.DETAILS).get_json()
        self.assertEqual(data["source"], "tmdb")

    def test_omdb_unreachable_falls_back(self):
        data = self._get("q=Tommy&type=movie", {}, self.TOMMY, self.DETAILS, omdb_raises=True).get_json()
        self.assertEqual(data["source"], "tmdb")

    def test_omdb_answers_are_still_used_when_it_works(self):
        omdb = {"Response": "True", "Search": [{"Title": "From OMDb", "Year": "1999", "imdbID": "ttO"}]}
        data = self._get("q=Tommy&type=movie", omdb, self.TOMMY, self.DETAILS).get_json()
        self.assertNotEqual(data.get("source"), "tmdb")
        self.assertEqual(data["matches"][0]["title"], "From OMDb")

    def test_a_series_asks_tmdb_for_tv(self):
        data = self._get("q=Bewitched&type=series", {"Response": "False", "Error": "Series not found!"},
                         [{"id": 5, "name": "Bewitched", "first_air_date": "1964-09-17", "overview": "x"}],
                         {"external_ids": {"imdb_id": "tt0057733"}, "episode_run_time": [30], "genres": [{"name": "Comedy"}],
                          "created_by": [{"name": "Sol Saks"}]}).get_json()
        first = data["matches"][0]
        self.assertEqual((first["title"], first["year"], first["imdb_id"], first["director"]), ("Bewitched", "1964", "tt0057733", "Sol Saks"))

    def test_with_nothing_anywhere_the_old_message_stays(self):
        data = self._get("q=Zzzz&type=movie", {"Response": "False", "Error": "Movie not found!"}, []).get_json()
        self.assertEqual(data["matches"], [])
        self.assertEqual(data["message"], "Movie not found!")

    def test_with_only_a_tmdb_key_it_still_works(self):
        data = self._get("q=Tommy&type=movie", {}, self.TOMMY, self.DETAILS, cfg={"tmdb_key": "t"}).get_json()
        self.assertEqual(data["source"], "tmdb")

    def test_a_year_in_the_title_is_not_searched_for_and_that_year_comes_first(self):
        import io
        seen = []
        omdb = {"Response": "True", "Search": [{"Title": "Bohemian Rhapsody", "Year": "1975", "imdbID": "tt1"},
                                              {"Title": "Bohemian Rhapsody", "Year": "2018", "imdbID": "tt2"}]}

        def fake(url, timeout=0):
            url = url if isinstance(url, str) else url.full_url
            seen.append(url)
            if "?s=" in url:
                return io.BytesIO(json.dumps(omdb).encode())
            return io.BytesIO(json.dumps({"Response": "True", "Title": "Bohemian Rhapsody",
                                          "Year": "2018" if "tt2" in url else "1975", "imdbID": "tt2" if "tt2" in url else "tt1"}).encode())
        with mock.patch.object(server, "load_config", return_value={"omdb_key": "o", "tmdb_key": "t"}), \
             mock.patch("urllib.request.urlopen", side_effect=fake):
            data = server.app.test_client().get(
                "/epg-web/api/wanted/movie-matches?q=Bohemian%20Rhapsody%20(2018)&type=movie").get_json()
        search = [u for u in seen if "?s=" in u][0]
        self.assertIn("s=Bohemian%20Rhapsody&", search)
        self.assertNotIn("2018", search)
        self.assertEqual([m["year"] for m in data["matches"]], ["2018", "1975"])

    def test_a_year_in_the_title_also_works_through_tmdb(self):
        data = self._get("q=Tommy%20(1975)&type=movie", {"Response": "False", "Error": "Movie not found!"},
                         self.TOMMY, self.DETAILS).get_json()
        self.assertEqual(data["source"], "tmdb")
        self.assertEqual(data["matches"][0]["year"], "1975")

    def test_a_title_that_is_only_a_year_is_left_alone(self):
        data = self._get("q=%282012%29&type=movie", {"Response": "False", "Error": "Movie not found!"}, []).get_json()
        self.assertEqual(data["matches"], [])

    def test_with_no_keys_at_all_it_says_so(self):
        resp = self._get("q=Tommy&type=movie", {}, cfg={})
        self.assertEqual(resp.status_code, 503)


class AutoRecordWantedTests(unittest.TestCase):
    """A Wanted film is recorded without anyone pressing Record."""

    def setUp(self):
        with server._rec_lock:
            server._recs.clear()
            server._rec_cancel_events.clear()
        server._AUTO_WANTED_FAILED.clear()
        server._init_recordings_table()
        self.conn = sqlite3.connect(server._guide_db_path())
        self.conn.execute("DELETE FROM guide WHERE channel_id LIKE 'aw-%'")
        self.conn.execute("DELETE FROM recordings")
        self.conn.commit()
        self.base = time.time() + 7200
        self.tmp = tempfile.TemporaryDirectory()
        self.movies = self.tmp.name

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _airing(self, title, year, channel="HBO", shift=0, channel_id=None):
        start = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.base + shift))
        end = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.base + shift + 6000))
        self.conn.execute("INSERT INTO guide(title, channel_id, channel_name, start_utc, end_utc, desc, category, prog_type, year) "
                          "VALUES (?,?,?,?,?,?,?,?,?)",
                          (title, channel_id or f"aw-{channel}-{shift}", channel, start, end, "", "", "MV", year))
        self.conn.commit()

    def _wanted(self, title, year, kind="movie"):
        return {"id": 1, "title": title, "normalized_title": title.lower(), "year": year, "type": kind,
                "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""}

    def _run(self, wanted, plex_titles=(), config=None):
        key = lambda t: server._norm_plex_show(t[0])
        index = {"movies": {key(t) for t in plex_titles}, "movie_versions": {(key(t), t[1]) for t in plex_titles},
                 "unyearred_movies": set(), "shows": set(), "movie_counts": {}, "show_versions": set(),
                 "unyearred_shows": set()}
        cfg = {"plex_path": self.movies}
        cfg.update(config or {})
        with mock.patch.object(server, "load_config", return_value=cfg), \
             mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
             mock.patch.object(server, "_recording_backend", return_value="agent"), \
             mock.patch.object(server, "_resolve_recording_source",
                               return_value=("http://x/1.ts", None, {"stream_id": "1", "provider": "primestreams"})):
            return server._auto_record_wanted_movies()

    def _recorded_titles(self):
        return sorted(r["title"] for r in server._recs.values())

    def test_a_wanted_film_with_a_commercial_free_airing_is_scheduled(self):
        self._airing("Auto Film", "2004", "HBO")
        summary = self._run([self._wanted("Auto Film", "2004")])
        self.assertEqual(self._recorded_titles(), ["Auto Film"])
        self.assertIn("Auto Film (2004) on HBO", summary["scheduled"][0])

    def test_a_film_already_in_plex_is_not_recorded_again(self):
        self._airing("Owned Film", "2004", "HBO")
        self._run([self._wanted("Owned Film", "2004")], plex_titles=[("Owned Film", "2004")])
        self.assertEqual(self._recorded_titles(), [])

    def test_a_copy_in_plex_for_another_year_does_not_block_it(self):
        self._airing("Remake Film", "2022", "HBO")
        self._run([self._wanted("Remake Film", "2022")], plex_titles=[("Remake Film", "2004")])
        self.assertEqual(self._recorded_titles(), ["Remake Film"])

    def test_ad_supported_channels_are_never_used(self):
        self._airing("Ads Film", "2004", "AMC")
        self._run([self._wanted("Ads Film", "2004")])
        self.assertEqual(self._recorded_titles(), [])

    def test_the_commercial_free_airing_is_chosen_over_an_earlier_ad_supported_one(self):
        self._airing("Pick Film", "2004", "AMC", shift=0)
        self._airing("Pick Film", "2004", "Flix", shift=3600)
        self._run([self._wanted("Pick Film", "2004")])
        self.assertEqual([r["channel"] for r in server._recs.values()], ["Flix"])

    def test_an_airing_of_another_year_is_not_recorded(self):
        self._airing("Year Film", "2022", "HBO")
        self._run([self._wanted("Year Film", "2004")])
        self.assertEqual(self._recorded_titles(), [])
        self._airing("Year Film", "2005", "HBO", shift=900)               # one year apart is the same film
        self._run([self._wanted("Year Film", "2004")])
        self.assertEqual(self._recorded_titles(), ["Year Film"])

    def test_a_film_that_already_has_an_active_recording_is_not_scheduled_twice(self):
        self._airing("Twice Film", "2004", "HBO", shift=0)
        self._airing("Twice Film", "2004", "Starz", shift=86400)
        wanted = [self._wanted("Twice Film", "2004")]
        self._run(wanted)
        self._run(wanted)
        self.assertEqual(self._recorded_titles(), ["Twice Film"])

    def test_a_series_on_wanted_is_left_to_the_series_scheduler(self):
        self._airing("Some Show", "1991", "HBO")
        self._run([self._wanted("Some Show", "1991", kind="series")])
        self.assertEqual(self._recorded_titles(), [])

    def test_a_full_slot_set_skips_one_film_but_not_the_rest(self):
        self._airing("Early Film", "2001", "HBO", shift=0)
        self._airing("Late Film", "2002", "Starz", shift=100000)
        wanted = [self._wanted("Early Film", "2001"), self._wanted("Late Film", "2002")]
        real = server._recording_slot_conflicts
        def fake(recs, start_ts, stop_ts, limit):
            return [{"title": "Busy"}] * 6 if start_ts < self.base + 50000 else real(recs, start_ts, stop_ts, limit)
        with mock.patch.object(server, "_recording_slot_conflicts", fake):
            self._run(wanted)
        self.assertEqual(self._recorded_titles(), ["Late Film"])

    def test_it_can_be_turned_off(self):
        self._airing("Off Film", "2004", "HBO")
        summary = self._run([self._wanted("Off Film", "2004")], config={"auto_record_wanted": False})
        self.assertTrue(summary["disabled"])
        self.assertEqual(self._recorded_titles(), [])

    def test_nothing_is_scheduled_when_plex_is_not_mounted(self):
        self._airing("Unmounted Film", "2004", "HBO")
        self._run([self._wanted("Unmounted Film", "2004")], config={"plex_path": "/no/such/plex"})
        self.assertEqual(self._recorded_titles(), [])

    def test_an_airing_that_starts_too_soon_is_not_scheduled(self):
        self.base = time.time() + 60
        self._airing("Soon Film", "2004", "HBO")
        self._run([self._wanted("Soon Film", "2004")])
        self.assertEqual(self._recorded_titles(), [])

    def test_a_refused_airing_is_not_retried_straight_away(self):
        self._airing("Refused Film", "2004", "HBO")
        wanted = [self._wanted("Refused Film", "2004")]
        with mock.patch.object(server, "load_config", return_value={"plex_path": self.movies}), \
             mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index",
                               return_value={"movies": set(), "movie_versions": set(), "unyearred_movies": set()}), \
             mock.patch.object(server, "_recording_backend", return_value="agent"), \
             mock.patch.object(server, "_resolve_recording_source", return_value=(None, "no stream", {})) as resolve:
            server._auto_record_wanted_movies()
            server._auto_record_wanted_movies()
        self.assertEqual(self._recorded_titles(), [])
        self.assertEqual(resolve.call_count, 1)

    def test_the_passes_are_wired_in(self):
        import inspect
        source = inspect.getsource(server)
        self.assertGreaterEqual(source.count("target=_auto_record_wanted_movies"), 4)     # 3 guide refreshes + adding a title
        self.assertIn("target=_auto_record_wanted_loop", source)
