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

server._AUTO_WANTED_BACKGROUND = False       # no background Wanted pass may run in the middle of a test


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

    def test_cinemax_flix_and_the_plex_channels_sit_with_the_premium_favorites(self):
        for name in ("Cinemax", "Cinemax Action", "Cinemax Hits", "Cinemax Hits (West)", "Flix", "INDIEPLEX", "MOVIEPLEX", "RETROPLEX",
                     "SHO x BET", "SHO Extreme", "THE MOVIE CHANNEL", "The Movie Channel", "The Movie Channel (SD)",
                     "The Movie Channel (West)", "The Movie Channel Xtra", "TMC"):
            self.assertTrue(server._is_premium_favorite_channel(name), name)
        for name in ("FAMILY ENTERTAINMENT TELEVISION", "AMC", "Flixster Movies", "Hallmark Channel", "Shop HQ", "ShopTV",
                     "Turner Classic Movies", "TCM (SD)"):
            self.assertFalse(server._is_premium_favorite_channel(name), name)

    def test_only_cinemax_gains_premium_status_for_recording_decisions(self):
        for name in ("Cinemax Action", "Cinemax Hits"):                          # a real fix: they are commercial-free premium
            self.assertTrue(server._is_premium_channel(name), name)
            self.assertTrue(server._is_commercial_free_channel(name), name)
        for name in ("Flix", "INDIEPLEX", "MOVIEPLEX", "RETROPLEX"):             # not premium networks, so not in the Premium movies view
            self.assertFalse(server._is_premium_channel(name), name)
            self.assertFalse(server._is_premium_movie_channel(name), name)
            self.assertTrue(server._is_commercial_free_channel(name), name)      # but commercial-free, so films can be recorded from them
            self.assertTrue(server._is_rerecord_channel(name), name)
        self.assertFalse(server._is_commercial_free_channel("FAMILY ENTERTAINMENT TELEVISION"))

    def test_showtimes_own_channels_are_premium_and_the_movie_channel_is_grouping_only(self):
        for name in ("SHO x BET", "SHO Extreme"):
            self.assertTrue(server._is_premium_channel(name), name)
            self.assertTrue(server._is_commercial_free_channel(name), name)
        self.assertFalse(server._is_premium_channel("Shop HQ"))                  # a bare "sho" must not catch shopping channels
        self.assertFalse(server._is_premium_channel("The Movie Channel"))        # grouping only: its recording rules are unchanged
        self.assertTrue(server._is_commercial_free_channel("The Movie Channel"))

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
    def test_agent_claim_skips_primestreams_bookings_while_switched_off(self, load_config):
        load_config.return_value = {
            "guide_db_path": self.db_path, "recording_backend": "agent",
            "recording_agent_token": "test-secret", "primestreams_enabled": False,
        }
        rec = {"title": "PS Test", "channel": "Test", "channel_id": "t", "stream_id": "1",
               "start_ts": time.time() + 60, "stop_ts": time.time() + 3600, "status": "queued",
               "backend": "agent", "file": "", "stream_provider": "primestreams"}
        self.assertTrue(server._db_upsert_rec("psjob", rec))
        client = server.app.test_client()
        body = {"agent_id": "mac-test", "claim_ahead_seconds": 300, "lease_seconds": 90}
        headers = {"Authorization": "Bearer test-secret"}
        self.assertIsNone(client.post("/epg-web/api/agent/jobs/claim", headers=headers, json=body).get_json()["job"])
        load_config.return_value["primestreams_enabled"] = True
        job = client.post("/epg-web/api/agent/jobs/claim", headers=headers, json=body).get_json()["job"]
        self.assertEqual(job["id"], "psjob")

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
    def test_playlist_is_a_downloadable_m3u_pointing_at_the_relay_not_the_provider(self):
        url = "http://provider.test/live/u/p/1.ts"
        with mock.patch.object(server, "_stream_url", return_value=(url, None, {})), \
             mock.patch.object(server, "load_config", return_value={}):
            resp = server.app.test_client().get(
                "/epg-web/api/play/playlist.m3u",
                query_string={"channel_id": "18086", "title": "Paycheck\nextra"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        body = resp.get_data(as_text=True)
        self.assertEqual(body.splitlines(), ["#EXTM3U", "#EXTINF:-1,Paycheck extra",
                                             "http://localhost/epg-web/api/play/proxy?channel_id=18086"])
        self.assertNotIn("provider.test", body)

    def test_playlist_gives_the_provider_address_directly_when_the_relay_is_switched_off(self):
        url = "http://provider.test/live/u/p/1.ts"
        with mock.patch.object(server, "_stream_url", return_value=(url, None, {})), \
             mock.patch.object(server, "load_config", return_value={"play_relay": False}):
            resp = server.app.test_client().get("/epg-web/api/play/playlist.m3u", query_string={"channel_id": "1", "title": "T"})
        self.assertEqual(resp.get_data(as_text=True).splitlines()[-1], url)

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

    def test_a_record_that_fails_is_reported_where_the_wanted_list_can_show_it(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("function reportRecordResult(text, cls)", html)
        self.assertIn("['pm-status', 'rec-status'].forEach", html)
        self.assertIn("reportRecordResult('❌ ' + (r.error || 'Failed'), 'err');", html)
        self.assertIn("is already scheduled", html)
        self.assertIn("Could not reach the dashboard: ", html)

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

    def test_the_wanted_list_says_when_the_guide_is_still_loading(self):
        wanted = [{"id": 1, "title": "Later Film", "normalized_title": "later film", "year": "2000", "type": "movie",
                   "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""}]
        index = {"movies": set(), "movie_versions": set(), "unyearred_movies": set(), "shows": set(),
                 "movie_counts": {}, "show_versions": set(), "unyearred_shows": set()}
        for loading, flag in (({"loading": False, "background_loading": True}, True),
                              ({"loading": True, "background_loading": False}, True),
                              ({"loading": False, "background_loading": False}, False)):
            with mock.patch.object(server, "db_rows", return_value=wanted), \
                 mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
                 mock.patch.dict(server._epg, {"programmes": []}), \
                 mock.patch.dict(server._startup_status, loading):
                data = server.app.test_client().get("/epg-web/api/recommendations").get_json()
            self.assertEqual(data["guide_loading"], flag)
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("Guide still loading…", html)
        self.assertIn("window._wantedReloadTimer", html)

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
             mock.patch.object(server, "_auto_wanted_lock", threading.Lock()), \
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

    def _channel_outcome(self, rec_id, channel_id, status, reason=""):
        self.conn.execute("INSERT OR REPLACE INTO recordings(rec_id, title, channel, channel_id, start_ts, stop_ts, status, failure_reason) "
                          "VALUES (?,?,?,?,?,?,?,?)", (rec_id, "T", channel_id, channel_id, time.time() - 3600, time.time(), status, reason))
        self.conn.commit()

    def test_a_damaged_recording_counts_double_against_its_channel(self):
        self._channel_outcome("o1", "aw-good", "done")
        self._channel_outcome("o2", "aw-damaged", "failed", "Video is damaged: 9000 decode errors")
        self._channel_outcome("o3", "aw-plain", "failed", "Recording ended early")
        stats = server._channel_recording_reliability()
        self.assertEqual(stats["aw-damaged"]["failed"], 2)
        self.assertEqual(stats["aw-plain"]["failed"], 1)
        self.assertEqual(stats["aw-good"]["level"], "reliable")
        self._channel_outcome("o4", "aw-damaged", "failed", "Video is damaged: 400 decode errors")
        self.assertEqual(server._channel_recording_reliability()["aw-damaged"]["level"], "suspect")    # two damaged: suspect

    def test_the_bad_channel_list_matches_names_and_ids(self):
        cfg = {"bad_channels": ["Flix", "aw-HBO-0"]}
        self.assertTrue(server._is_bad_channel(cfg, "x", "FLIX"))
        self.assertTrue(server._is_bad_channel(cfg, "aw-HBO-0", "Other"))
        self.assertFalse(server._is_bad_channel(cfg, "y", "Starz"))
        self.assertFalse(server._is_bad_channel({}, "y", "Flix"))
        self.assertTrue(server._is_bad_channel({"bad_channels": "Flix"}, "y", "Flix"))

    def test_a_bad_channel_is_never_used_automatically(self):
        self._airing("Bad Film", "2004", "Flix", shift=0)
        self._airing("Bad Film", "2004", "Starz", shift=3600)
        self._run([self._wanted("Bad Film", "2004")], config={"bad_channels": ["Flix"]})
        self.assertEqual([r["channel"] for r in server._recs.values()], ["Starz"])

    def test_a_channel_with_a_good_record_is_preferred_within_a_week(self):
        self._airing("Choice Film", "2004", "HBO", shift=0, channel_id="aw-new")
        self._airing("Choice Film", "2004", "Starz", shift=3 * 86400, channel_id="aw-proven")
        self._channel_outcome("p1", "aw-proven", "done")
        self._run([self._wanted("Choice Film", "2004")])
        self.assertEqual([r["channel"] for r in server._recs.values()], ["Starz"])

    def test_a_better_channel_more_than_a_week_later_is_not_waited_for(self):
        self._airing("Late Film", "2004", "HBO", shift=0, channel_id="aw-new2")
        self._airing("Late Film", "2004", "Starz", shift=9 * 86400, channel_id="aw-proven2")
        self._channel_outcome("p2", "aw-proven2", "done")
        self._run([self._wanted("Late Film", "2004")])
        self.assertEqual([r["channel"] for r in server._recs.values()], ["HBO"])

    def test_a_channel_with_failures_is_the_last_choice(self):
        self._airing("Rank Film", "2004", "HBO", shift=0, channel_id="aw-shaky")
        self._airing("Rank Film", "2004", "Starz", shift=3600, channel_id="aw-unknown")
        self._channel_outcome("s1", "aw-shaky", "done")
        self._channel_outcome("s2", "aw-shaky", "failed", "Recording ended early")
        self._run([self._wanted("Rank Film", "2004")])
        self.assertEqual([r["channel"] for r in server._recs.values()], ["Starz"])

    def test_the_passes_are_wired_in(self):
        import inspect
        source = inspect.getsource(server)
        self.assertGreaterEqual(source.count("_start_auto_wanted_pass()"), 4)     # 3 guide refreshes + adding a title
        self.assertIn("target=_auto_record_wanted,", source)
        self.assertIn("target=_auto_record_wanted_loop", source)


class ChannelWarningTests(unittest.TestCase):
    """Before a manual Record on a channel with a poor record, show the stats and the alternatives."""

    POOR = "cw-poor"

    def setUp(self):
        with server._rec_lock:
            server._recs.clear()
            server._rec_cancel_events.clear()
        server._init_recordings_table()
        self.conn = sqlite3.connect(server._guide_db_path())
        self.conn.execute("DELETE FROM guide WHERE channel_id LIKE 'cw-%'")
        self.conn.execute("DELETE FROM recordings")
        self.conn.commit()
        self.start = time.time() + 7200

    def tearDown(self):
        self.conn.close()

    def _history(self, rec_id, channel_id, status, reason=""):
        self.conn.execute("INSERT OR REPLACE INTO recordings(rec_id, title, channel, channel_id, start_ts, stop_ts, status, failure_reason) "
                          "VALUES (?,?,?,?,?,?,?,?)", (rec_id, "T", channel_id, channel_id, time.time() - 3600, time.time(), status, reason))
        self.conn.commit()

    def _make_poor(self):
        self._history("a1", self.POOR, "done")
        self._history("a2", self.POOR, "failed", "Recording ended early")
        self._history("a3", self.POOR, "failed", "Video is damaged: 9000 decode errors")

    def _airing(self, title, year, channel, channel_id, shift=0):
        key = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.start + shift))
        end = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.start + shift + 6000))
        self.conn.execute("INSERT INTO guide(title, channel_id, channel_name, start_utc, end_utc, desc, category, prog_type, year) "
                          "VALUES (?,?,?,?,?,?,?,?,?)", (title, channel_id, channel, key, end, "", "", "MV", year))
        self.conn.commit()

    def _record(self, **extra):
        body = {"title": "Warn Film", "channel_id": self.POOR, "channel_name": "Poor Channel",
                "start_ts": self.start, "stop_ts": self.start + 6000, "year": "2004"}
        body.update(extra)
        with mock.patch.object(server, "_recording_backend", return_value="agent"), \
             mock.patch.object(server, "_resolve_recording_source",
                               return_value=("http://x/1.ts", None, {"stream_id": "1", "provider": "primestreams"})):
            return server.app.test_client().post("/epg-web/api/record", json=body)

    def test_a_channel_with_no_trouble_gives_no_warning(self):
        self.assertIsNone(server._channel_history_warning("nowhere"))
        self._history("g1", "cw-fine", "done")
        self._history("g2", "cw-fine", "done")
        self._history("g3", "cw-fine", "done")
        self._history("g4", "cw-fine", "failed", "Recording ended early")
        self.assertIsNone(server._channel_history_warning("cw-fine"))             # one in four is not enough

    def test_the_warning_has_real_counts(self):
        self._make_poor()
        warning = server._channel_history_warning(self.POOR)
        self.assertEqual((warning["problems"], warning["total"], warning["damaged"]), (2, 3, 1))
        self.assertIn("2 of 3 recent recordings", warning["text"])
        self.assertIn("1 failed or ended early", warning["text"])
        self.assertIn("1 came out with damaged video", warning["text"])

    def test_a_manual_record_asks_first_and_records_nothing(self):
        self._make_poor()
        resp = self._record(ask_channel=True)
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertTrue(data["channel_warning"])
        self.assertIn("2 of 3", data["stats"]["text"])
        self.assertEqual(len(server._recs), 0)

    def test_confirming_records_it(self):
        self._make_poor()
        self.assertEqual(self._record(ask_channel=True, confirm_channel=True).status_code, 200)
        self.assertEqual(len(server._recs), 1)

    def test_automatic_scheduling_is_never_held_up(self):
        self._make_poor()
        self.assertEqual(self._record().status_code, 200)                           # no ask_channel

    def test_a_good_channel_records_without_asking(self):
        self._history("g1", self.POOR, "done")
        self.assertEqual(self._record(ask_channel=True).status_code, 200)

    def test_better_channels_are_offered_as_alternatives(self):
        self._make_poor()
        self._airing("Warn Film", "2004", "HBO", "cw-good", shift=86400)
        self._history("g1", "cw-good", "done")
        self._airing("Warn Film", "2004", "Showtime", "cw-new", shift=2 * 86400)
        self._airing("Warn Film", "2004", "AMC", "cw-ads", shift=3600)               # ad-supported: not offered
        self._airing("Warn Film", "2022", "Starz", "cw-remake", shift=3600)          # another film
        self._airing("Warn Film", "2004", "Poor Channel", self.POOR, shift=7200)      # the same channel
        self._airing("Other Film", "2004", "Starz", "cw-other", shift=3600)           # another title
        self._airing("Warn Film", "2004", "Cinemax", "cw-bad", shift=3600)
        for n, status in enumerate(("failed", "failed", "failed")):
            self._history(f"b{n}", "cw-bad", status, "Recording ended early")       # a poor channel too: not offered
        data = self._record(ask_channel=True).get_json()
        channels = [a["channel"] for a in data["alternatives"]]
        self.assertEqual(sorted(channels), ["HBO", "Showtime"])
        hbo = next(a for a in data["alternatives"] if a["channel"] == "HBO")
        self.assertIn("1 recent recording(s) came out fine", hbo["record"])
        showtime = next(a for a in data["alternatives"] if a["channel"] == "Showtime")
        self.assertIn("no recent recordings", showtime["record"])

    def test_the_warning_says_whether_waiting_can_put_the_film_on_wanted(self):
        self._make_poor()
        data = self._record(ask_channel=True).get_json()
        self.assertTrue(data["can_wait_in_wanted"])
        self.assertEqual((data["year"], data["wanted_title"]), ("2004", "Warn Film"))
        self._airing("Warn Film (2004)", "", "Showtime", "cw-new", shift=86400)
        data = self._record(ask_channel=True, title="Warn Film (2004)", year="").get_json()
        self.assertEqual(data["wanted_title"], "Warn Film")                       # a year in the title is dropped
        no_year = self._record(ask_channel=True, title="Nameless Film", year="").get_json()
        self.assertFalse(no_year["can_wait_in_wanted"])                           # Wanted needs a year
        series = self._record(ask_channel=True, is_series=True).get_json()
        self.assertFalse(series["can_wait_in_wanted"])                            # a series is not a Wanted film

    def test_finding_alternatives_does_not_fold_every_programme_in_the_guide(self):
        self._make_poor()
        self._airing("Warn Film (2004)", "", "HBO", "cw-good", shift=86400)
        self._history("g1", "cw-good", "done")
        for n in range(1500):
            self._airing(f"Unrelated Programme {n}", "", "HBO", f"cw-x{n % 7}", shift=3600 + n)
        calls = []
        real = server.fold_title
        def counting(value):
            calls.append(value)
            return real(value)
        with mock.patch.object(server, "fold_title", counting):
            found = server._better_channel_airings("Warn Film", "2004", self.POOR)
        self.assertEqual([a["channel"] for a in found], ["HBO"])                    # the "(2004)" listing is found
        self.assertLess(len(calls), 20)

    def test_a_poor_channel_is_not_used_for_a_wanted_film_either(self):
        self._make_poor()
        self._airing("Wait Film", "2004", "Poor Channel", self.POOR, shift=3600)
        auto = AutoRecordWantedTests("test_the_passes_are_wired_in")
        auto.setUp()
        try:
            auto._run([auto._wanted("Wait Film", "2004")])
            self.assertEqual(len(server._recs), 0)
        finally:
            auto.tearDown()

    def test_waiting_for_a_better_channel_puts_the_film_on_wanted(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("(add to Wanted)", html)
        self.assertIn("post('/epg-web/api/wanted', {action: 'add', title: d.wanted_title || d.title, year: d.year, type: 'movie'})", html)
        self.assertIn("is on Wanted. It will record from a channel with a good record.", html)

    def test_the_page_asks_and_can_go_ahead(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("function showChannelWarningModal(d, proceed)", html)
        self.assertIn("Wait for a better channel", html)
        self.assertIn("Record anyway", html)
        self.assertIn("ask_channel: true, confirm_channel: confirmChannel || undefined", html)
        self.assertIn("showChannelWarningModal(r, () => recordAiring(airing, title, btn, allowCommercialMovie, year, true))", html)
        self.assertIn("showChannelWarningModal(d, () => quickRecord(key, btnEl, true))", html)


class WantedSeriesAndStatusTests(unittest.TestCase):
    """Wanted shows record themselves too, and the list says what is happening."""

    def _wanted(self, title, kind, year="2000"):
        return {"id": 1, "title": title, "normalized_title": title.lower(), "year": year, "type": kind,
                "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""}

    def test_every_wanted_series_has_its_missing_episodes_scheduled(self):
        rows = [self._wanted("Show One", "series"), self._wanted("Show Two", "series")]
        calls = []
        def fake(title, guide_db, movies_db, tz):
            calls.append(title)
            return 2
        with mock.patch.object(server, "load_config", return_value={}), \
             mock.patch.object(server, "_auto_wanted_series_lock", threading.Lock()), \
             mock.patch.object(server, "db_rows", return_value=rows), \
             mock.patch.object(server, "_schedule_series_airings", fake):
            self.assertEqual(server._auto_record_wanted_series(), 4)
        self.assertEqual(calls, ["Show One", "Show Two"])

    def test_wanted_series_are_left_alone_when_automatic_recording_is_off(self):
        with mock.patch.object(server, "load_config", return_value={"auto_record_wanted": False}), \
             mock.patch.object(server, "_auto_wanted_series_lock", threading.Lock()), \
             mock.patch.object(server, "db_rows", return_value=[self._wanted("Show One", "series")]), \
             mock.patch.object(server, "_schedule_series_airings") as schedule:
            self.assertEqual(server._auto_record_wanted_series(), 0)
        schedule.assert_not_called()

    def test_one_pass_does_films_then_series(self):
        with mock.patch.object(server, "_auto_record_wanted_movies", return_value={"scheduled": []}) as films, \
             mock.patch.object(server, "_auto_record_wanted_series", return_value=3) as series:
            result = server._auto_record_wanted()
        films.assert_called_once()
        series.assert_called_once()
        self.assertEqual(result["series"], 3)

    def test_the_list_reports_what_is_scheduled(self):
        wanted = [self._wanted("Sched Film", "movie", "2004"), self._wanted("Idle Film", "movie", "2005"),
                  self._wanted("Sched Show", "series", "1999")]
        index = {"movies": set(), "movie_versions": set(), "unyearred_movies": set(), "shows": set(),
                 "movie_counts": {}, "show_versions": set(), "unyearred_shows": set()}
        soon = time.time() + 86400
        with server._rec_lock:
            server._recs.clear()
            server._recs["r1"] = {"title": "Sched Film (2004)", "channel": "HBO", "channel_id": "hbo", "start_ts": soon,
                                  "stop_ts": soon + 7200, "status": "queued"}
            server._recs["r2"] = {"title": "Sched Show", "channel": "TCM", "channel_id": "tcm", "start_ts": soon + 100,
                                  "stop_ts": soon + 3700, "status": "scheduled"}
            server._recs["r3"] = {"title": "Sched Show", "channel": "TCM", "channel_id": "tcm", "start_ts": soon + 9000,
                                  "stop_ts": soon + 12600, "status": "queued"}
            server._recs["r4"] = {"title": "Idle Film", "channel": "HBO", "channel_id": "hbo", "start_ts": soon,
                                  "stop_ts": soon + 7200, "status": "done"}
        try:
            with mock.patch.object(server, "db_rows", return_value=wanted), \
                 mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
                 mock.patch.object(server, "load_config", return_value={}), \
                 mock.patch.dict(server._epg, {"programmes": []}):
                recs = server.app.test_client().get("/epg-web/api/recommendations").get_json()["recommendations"]
        finally:
            with server._rec_lock:
                server._recs.clear()
        by_title = {r["title"]: r for r in recs}
        self.assertEqual(by_title["Sched Film"]["scheduled"]["channel"], "HBO")
        self.assertEqual(by_title["Sched Film"]["scheduled"]["count"], 1)
        self.assertIsNone(by_title["Idle Film"]["scheduled"])                       # a finished recording is not scheduled
        self.assertEqual(by_title["Sched Show"]["scheduled"]["count"], 2)
        self.assertTrue(all(r["auto_record"] for r in recs))

    def test_the_automatic_flag_follows_the_config(self):
        index = {"movies": set(), "movie_versions": set(), "unyearred_movies": set(), "shows": set(),
                 "movie_counts": {}, "show_versions": set(), "unyearred_shows": set()}
        with mock.patch.object(server, "db_rows", return_value=[self._wanted("Any Film", "movie", "2004")]), \
             mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
             mock.patch.object(server, "load_config", return_value={"auto_record_wanted": False}), \
             mock.patch.dict(server._epg, {"programmes": []}):
            recs = server.app.test_client().get("/epg-web/api/recommendations").get_json()["recommendations"]
        self.assertFalse(recs[0]["auto_record"])

    def test_the_page_shows_status_not_a_record_button_unless_automatic_recording_is_off(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("function wantedAutoStatus(r, a, airingArg, titleArg, yearArg)", html)
        self.assertIn("⏱ Scheduled</span>", html)
        self.assertIn("Will record automatically", html)
        self.assertIn("Waiting for a better channel", html)
        self.assertIn("if (!r.auto_record) {", html)
        self.assertIn(": wantedAutoStatus(r, a, airingArg, titleArg, yearArg);", html)
        self.assertIn("<th>Recording</th>", html)


class ReplaceWeakerScheduledRecordingTests(unittest.TestCase):
    """A queued recording on an ad-supported or poor channel gives way to a better airing."""

    def setUp(self):
        self.case = AutoRecordWantedTests("test_it_can_be_turned_off")
        self.case.setUp()
        self.now = time.time()

    def tearDown(self):
        with server._rec_lock:
            server._recs.clear()
        self.case.tearDown()

    def _queue(self, rec_id, title, channel, channel_id, start_in, status="queued"):
        with server._rec_lock:
            server._recs[rec_id] = {"title": title, "channel": channel, "channel_id": channel_id,
                                    "start_ts": self.now + start_in, "stop_ts": self.now + start_in + 6000,
                                    "status": status, "backend": "agent"}

    def _airing(self, title, year, channel, channel_id, start_in):
        self.case.base = self.now + start_in
        self.case._airing(title, year, channel, shift=0, channel_id=channel_id)

    def _go(self, title="Swap Film", year="2004", config=None):
        return self.case._run([self.case._wanted(title, year)], config=config)

    def _status(self, rec_id):
        return server._rec_status_base(server._recs[rec_id]["status"])

    def test_an_ad_supported_recording_is_replaced_by_an_earlier_commercial_free_airing(self):
        self._queue("old", "Swap Film", "AMC", "aw-amc", 6 * 86400)
        self._airing("Swap Film", "2004", "Starz", "aw-starz", 2 * 86400)
        summary = self._go()
        self.assertEqual(self._status("old"), "cancelled")
        others = [r for rid, r in server._recs.items() if rid != "old"]
        self.assertEqual([r["channel"] for r in others], ["Starz"])
        self.assertIn("AMC replaced by Starz", summary["replaced"][0])

    def test_a_better_airing_more_than_three_days_later_does_not_replace_it(self):
        self._queue("old", "Swap Film", "AMC", "aw-amc", 1 * 86400)
        self._airing("Swap Film", "2004", "Starz", "aw-starz", 6 * 86400)
        self._go()
        self.assertEqual(self._status("old"), "queued")
        self.assertEqual(len(server._recs), 1)

    def test_a_recording_that_has_started_or_is_about_to_is_never_touched(self):
        self._airing("Swap Film", "2004", "Starz", "aw-starz", 86400)
        self._queue("old", "Swap Film", "AMC", "aw-amc", 300)                      # starts in 5 minutes
        self._go()
        self.assertEqual(self._status("old"), "queued")
        self._queue("old", "Swap Film", "AMC", "aw-amc", -600, status="recording")   # already recording
        self._go()
        self.assertEqual(self._status("old"), "recording")
        self.assertEqual(len(server._recs), 1)

    def test_a_recording_on_a_good_commercial_free_channel_is_left_alone(self):
        self._queue("old", "Swap Film", "HBO", "aw-hbo", 5 * 86400)
        self._airing("Swap Film", "2004", "Starz", "aw-starz", 2 * 86400)
        self._go()
        self.assertEqual(self._status("old"), "queued")
        self.assertEqual(len(server._recs), 1)

    def test_a_recording_on_a_poor_channel_is_replaced(self):
        for n, (status, reason) in enumerate((("done", ""), ("failed", "Recording ended early"), ("failed", "Video is damaged: 9000 decode errors"))):
            self.case.conn.execute("INSERT OR REPLACE INTO recordings(rec_id, title, channel, channel_id, start_ts, stop_ts, status, failure_reason) "
                                   "VALUES (?,?,?,?,?,?,?,?)", (f"h{n}", "T", "aw-poor", "aw-poor", time.time() - 3600, time.time(), status, reason))
        self.case.conn.commit()
        self._queue("old", "Swap Film", "Showtime", "aw-poor", 4 * 86400)
        self._airing("Swap Film", "2004", "Starz", "aw-starz", 2 * 86400)
        self._go()
        self.assertEqual(self._status("old"), "cancelled")
        self.assertEqual(len(server._recs), 2)

    def test_if_the_better_airing_cannot_be_queued_the_old_recording_stays(self):
        self._queue("old", "Swap Film", "AMC", "aw-amc", 6 * 86400)
        self._airing("Swap Film", "2004", "Starz", "aw-starz", 2 * 86400)
        with mock.patch.object(server, "_recording_slot_conflicts", return_value=[{"title": "Busy"}] * 6):
            self._go()
        self.assertEqual(self._status("old"), "queued")

    def test_a_sky_style_listing_with_the_year_in_its_title_is_matched(self):
        self._airing("Swap Film (2004)", "", "SKY CINEMA DRAMA | VIP", "aw-sky", 2 * 86400)
        summary = self._go()
        self.assertEqual(len(summary["scheduled"]), 1)
        self.assertIn("Swap Film (2004)", [r["title"] for r in server._recs.values()])

    def test_a_scheduled_summary_says_whether_the_channel_has_ads(self):
        recs = [{"title": "Ad Film", "channel": "AMC", "start_ts": self.now + 100, "status": "queued"}]
        self.assertFalse(server._wanted_scheduled_summary(recs, "Ad Film")["commercial_free"])
        recs[0]["channel"] = "HBO"
        self.assertTrue(server._wanted_scheduled_summary(recs, "Ad Film")["commercial_free"])

    def test_the_wanted_list_lists_sky_style_airings_and_warns_about_ads(self):
        wanted = [{"id": 1, "title": "Sky Film", "normalized_title": "sky film", "year": "2018", "type": "movie",
                   "status": "wanted", "notes": "", "source": "imdb", "imdb_id": "", "updated_at": ""}]
        index = {"movies": set(), "movie_versions": set(), "unyearred_movies": set(), "shows": set(),
                 "movie_counts": {}, "show_versions": set(), "unyearred_shows": set()}
        far = time.time() + 86400
        progs = [dict(title="Sky Film (2018)", channel_id="c", channel="SKY CINEMA DRAMA | VIP", stop_ts=far + 7000, start_ts=far, start_fmt="")]
        with mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
             mock.patch.dict(server._epg, {"programmes": progs}):
            recs = server.app.test_client().get("/epg-web/api/recommendations").get_json()["recommendations"]
        self.assertEqual(recs[0]["next_airing"]["channel"], "SKY CINEMA DRAMA | VIP")
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("⚠ ads", html)
        self.assertIn("r.scheduled.commercial_free === false", html)


class AutoRecordReasonsTests(unittest.TestCase):
    """The Wanted list says why a film with an airing has not been scheduled."""

    def setUp(self):
        self.case = AutoRecordWantedTests("test_it_can_be_turned_off")
        self.case.setUp()
        server._AUTO_WANTED_REASONS.clear()
        self.key = server.fold_title("Reason Film")

    def tearDown(self):
        server._AUTO_WANTED_REASONS.clear()
        with server._rec_lock:
            server._recs.clear()
        self.case.tearDown()

    def _go(self, config=None):
        return self.case._run([self.case._wanted("Reason Film", "2004")], config=config)

    def _reason(self):
        return (server._AUTO_WANTED_REASONS.get(self.key) or {}).get("reason")

    def _history(self, rec_id, channel_id, status, reason="", title="T", start_ago=3600):
        self.case.conn.execute("INSERT OR REPLACE INTO recordings(rec_id, title, channel, channel_id, start_ts, stop_ts, status, failure_reason) "
                               "VALUES (?,?,?,?,?,?,?,?)", (rec_id, title, channel_id, channel_id, time.time() - start_ago, time.time(), status, reason))
        self.case.conn.commit()

    def test_a_poor_channel_is_named_with_its_counts(self):
        self.case._airing("Reason Film", "2004", "Starz", channel_id="aw-poor")
        for n, status in enumerate(("done", "failed", "failed")):
            self._history(f"p{n}", "aw-poor", status, "Recording ended early")
        self._go()
        self.assertEqual(len(server._recs), 0)
        self.assertIn("Starz has a poor recording record (2 of 3 recent recordings went wrong)", self._reason())

    def test_the_bad_channel_list_is_named(self):
        self.case._airing("Reason Film", "2004", "Flix", channel_id="aw-flix")
        self._go(config={"bad_channels": ["Flix"]})
        self.assertEqual(self._reason(), "Flix is on your bad-channels list")

    def test_a_recent_failure_of_this_film_on_this_channel_is_named(self):
        self.case._airing("Reason Film", "2004", "HBO", channel_id="aw-hbo")
        self._history("f1", "aw-hbo", "failed", "Recording ended early", title="Reason Film", start_ago=86400)
        self._go()
        self.assertEqual(self._reason(), "a recording of this film from HBO failed in the last 14 days")

    def _cancel_by_hand(self, rec_id, channel_id, shift=0):
        self.case.conn.execute("INSERT INTO recordings(rec_id, title, channel, channel_id, start_ts, stop_ts, status, failure_reason) "
                               "VALUES (?,?,?,?,?,?,?,?)",
                               (rec_id, "Reason Film", "HBO", channel_id, self.case.base + shift,
                                self.case.base + shift + 6000, "cancelled", "Cancelled by user"))
        self.case.conn.commit()

    def test_an_airing_the_owner_cancelled_is_not_booked_again(self):
        self.case._airing("Reason Film", "2004", "HBO", channel_id="aw-hbo")
        self._cancel_by_hand("c1", "aw-hbo")
        self._go()
        self.assertEqual(len(server._recs), 0)
        self.assertEqual(self._reason(), "you cancelled the recording of this airing on HBO")

    def test_another_airing_is_still_booked_after_a_cancel(self):
        self.case._airing("Reason Film", "2004", "HBO", channel_id="aw-hbo")
        self.case._airing("Reason Film", "2004", "HBO", channel_id="aw-hbo", shift=86400)
        self._cancel_by_hand("c1", "aw-hbo")
        self._go()
        self.assertEqual([int(r["start_ts"]) for r in server._recs.values()], [int(self.case.base + 86400)])

    def test_cancelled_by_user_airings_are_listed_but_other_cancels_are_not(self):
        self._cancel_by_hand("c1", "aw-hbo")
        self.case.conn.execute("UPDATE recordings SET failure_reason='Replaced' WHERE rec_id='c1'")
        self.case.conn.commit()
        self.assertEqual(server._user_cancelled_airings(), set())
        self._cancel_by_hand("c2", "aw-hbo")
        self.assertEqual(server._user_cancelled_airings(), {("aw-hbo", int(self.case.base))})

    def test_full_recording_slots_are_named(self):
        self.case._airing("Reason Film", "2004", "HBO", channel_id="aw-hbo")
        with mock.patch.object(server, "_recording_slot_conflicts", return_value=[{"title": "Busy"}] * 6):
            self._go()
        self.assertIn("all recording slots are busy when it airs on HBO", self._reason())

    def test_a_refusal_is_named(self):
        self.case._airing("Reason Film", "2004", "HBO", channel_id="aw-hbo")
        wanted = [self.case._wanted("Reason Film", "2004")]
        with mock.patch.object(server, "load_config", return_value={"plex_path": self.case.movies}), \
             mock.patch.object(server, "_auto_wanted_lock", threading.Lock()), \
             mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index",
                               return_value={"movies": set(), "movie_versions": set(), "unyearred_movies": set()}), \
             mock.patch.object(server, "_recording_backend", return_value="agent"), \
             mock.patch.object(server, "_resolve_recording_source", return_value=(None, "no stream", {})):
            server._auto_record_wanted_movies()
        self.assertIn("HBO cannot be recorded then: no stream", self._reason())

    def test_a_scheduled_film_has_no_reason_and_an_old_reason_is_not_shown(self):
        self.case._airing("Reason Film", "2004", "HBO", channel_id="aw-hbo")
        self._go()
        self.assertEqual(len(server._recs), 1)
        self.assertIsNone(self._reason())
        server._AUTO_WANTED_REASONS[self.key] = {"reason": "was blocked", "at": time.time()}
        self.assertEqual(server._wanted_auto_reason("Reason Film"), "was blocked")
        server._AUTO_WANTED_REASONS[self.key] = {"reason": "was blocked", "at": time.time() - 4 * 3600}
        self.assertIsNone(server._wanted_auto_reason("Reason Film"))

    def test_the_next_airing_is_tried_when_the_first_has_no_mapped_stream(self):
        self.case._airing("Reason Film", "2004", "STARZ 1", channel_id="aw-unmapped", shift=0)
        self.case._airing("Reason Film", "2004", "Showtime", channel_id="aw-mapped", shift=3600)
        wanted = [self.case._wanted("Reason Film", "2004")]
        def resolve(channel_id, start_ts, stop_ts, exclude_rec_id="", preferred_provider=None):
            if channel_id == "aw-unmapped":
                return None, "no stream", {}
            return "http://x/1.ts", None, {"stream_id": "1", "provider": "primestreams"}
        with mock.patch.object(server, "load_config", return_value={"plex_path": self.case.movies}), \
             mock.patch.object(server, "_auto_wanted_lock", threading.Lock()), \
             mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index",
                               return_value={"movies": set(), "movie_versions": set(), "unyearred_movies": set()}), \
             mock.patch.object(server, "_recording_backend", return_value="agent"), \
             mock.patch.object(server, "_resolve_recording_source", resolve):
            server._auto_record_wanted_movies()
        self.assertEqual([r["channel"] for r in server._recs.values()], ["Showtime"])
        self.assertIsNone(self._reason())                                       # it did get scheduled

    def test_when_every_airing_is_unmapped_the_reason_says_so_and_the_next_pass_retries(self):
        self.case._airing("Reason Film", "2004", "STARZ 1", channel_id="aw-un1", shift=0)
        self.case._airing("Reason Film", "2004", "STARZ 2", channel_id="aw-un2", shift=3600)
        wanted = [self.case._wanted("Reason Film", "2004")]
        with mock.patch.object(server, "load_config", return_value={"plex_path": self.case.movies}), \
             mock.patch.object(server, "_auto_wanted_lock", threading.Lock()), \
             mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index",
                               return_value={"movies": set(), "movie_versions": set(), "unyearred_movies": set()}), \
             mock.patch.object(server, "_recording_backend", return_value="agent"), \
             mock.patch.object(server, "_resolve_recording_source", return_value=(None, "no stream", {})):
            server._auto_record_wanted_movies()
        self.assertEqual(len(server._recs), 0)
        self.assertIn("STARZ 1 cannot be recorded then: no stream", self._reason())
        self.assertIn("the next pass tries again", self._reason())

    def test_the_list_carries_the_reason_and_the_page_shows_it(self):
        server._AUTO_WANTED_REASONS[self.key] = {"reason": "Starz has a poor recording record", "at": time.time()}
        wanted = [self.case._wanted("Reason Film", "2004")]
        index = {"movies": set(), "movie_versions": set(), "unyearred_movies": set(), "shows": set(),
                 "movie_counts": {}, "show_versions": set(), "unyearred_shows": set()}
        with mock.patch.object(server, "db_rows", return_value=wanted), \
             mock.patch.object(server, "_plex_wanted_title_index", return_value=index), \
             mock.patch.dict(server._epg, {"programmes": []}):
            recs = server.app.test_client().get("/epg-web/api/recommendations").get_json()["recommendations"]
        self.assertEqual(recs[0]["auto_reason"], "Starz has a poor recording record")
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("if (a && r.auto_reason)", html)
        self.assertIn("Waiting: ' + esc(r.auto_reason)", html)


class StarzAndHboHitsStreamMapTests(unittest.TestCase):
    def _guide(self, temp, rows):
        guide_db = os.path.join(temp, "guide.db")
        server.ensure_guide_db(guide_db)
        conn = sqlite3.connect(guide_db)
        conn.executemany("INSERT INTO guide_channels(channel_id,channel_name) VALUES (?,?)", rows)
        conn.commit()
        conn.close()
        return guide_db

    def test_starz_channels_use_the_eaglecast_streams(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = self._guide(temp, [("14947", "STARZ 1"), ("24804", "STARZ 2")])
            self.assertEqual(server._eaglecast_verified_override("14947", guide_db)[0], "176929")
            self.assertEqual(server._eaglecast_verified_override("24804", guide_db)[0], "176930")

    def test_hbo_hits_uses_the_primestreams_hbo_hits_feed(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = self._guide(temp, [("10241", "HBO Hits"), ("59355", "HBO Hits (West)")])
            with mock.patch.object(server, "_guide_db_path", return_value=guide_db):
                self.assertEqual(server._primestream_verified_override("10241")[0], "95926")
                self.assertEqual(server._primestream_verified_override("59355")[0], "110774")
                self.assertIsNone(server._primestream_verified_override("99999"))


class ShowtimeWestRebrandMapTests(unittest.TestCase):
    def test_paramount_showtime_pacific_uses_the_showtime_west_eaglecast_stream(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            server.ensure_guide_db(guide_db)
            conn = sqlite3.connect(guide_db)
            conn.executemany(
                "INSERT INTO provider_streams(provider,channel_id,stream_id,provider_channel_name,"
                "stream_extension,guide_channel_name,mapped_at) VALUES ('eaglecast',?,?,?,?,?,0)",
                [("22532", "92020", "US| SHOWTIME WEST HD", "ts", "Paramount+ with Showtime HD (Pacific)"),
                 ("11115", "45503", "US| SHOWTIME HD", "ts", "Paramount+ with Showtime HD (Eastern)")])
            conn.commit(); conn.close()
            with mock.patch.object(server, "_guide_db_path", return_value=guide_db):
                self.assertEqual(server._eaglecast_stream_for_channel("22532")[0], "92020")
                self.assertIn("22532", server.get_eaglecast_channel_ids(guide_db))


class PlayBlockedNoteTests(unittest.TestCase):
    def test_overlap_error_names_the_recording_in_plain_words(self):
        err = ('Eaglecast is already scheduled for overlapping "Rawhide", '
               'and PrimeStreams has no usable stream for this channel.')
        self.assertEqual(server._play_blocked_note(err),
                         'Eaglecast has one connection and it is busy with "Rawhide"')

    def test_no_error_gives_no_note_and_addresses_are_hidden(self):
        self.assertEqual(server._play_blocked_note(''), '')
        self.assertNotIn("http", server._play_blocked_note("failed http://host/live/user/pw/1.ts"))

    def test_page_shows_the_play_note(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("'▶ Play unavailable: ' + selectedAiring.play_note", html)


class EaglecastDependencyReportTests(unittest.TestCase):
    def test_report_says_which_recordings_primestreams_could_not_cover(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            server.ensure_guide_db(guide_db)
            with mock.patch.object(server, "_guide_db_path", return_value=guide_db):
                server._init_recordings_table()
            conn = sqlite3.connect(guide_db)
            now = time.time()
            conn.executemany(
                "INSERT INTO recordings(rec_id,title,channel,channel_id,start_ts,stop_ts,status,stream_provider) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [("a", "Film A", "HBO", "hbo", now + 3600, now + 7200, "queued", "eaglecast"),
                 ("b", "Film B", "STARZ 1", "starz1", now + 3600, now + 7200, "queued", "eaglecast"),
                 ("c", "Film C", "HBO", "hbo", now - 86400, now - 80000, "done", "eaglecast")])
            conn.commit(); conn.close()

            def fake_stream(channel_id, preferred=None):
                self.assertEqual(preferred, "primestreams")
                return ("url-with-secret", None, {}) if channel_id == "hbo" else (None, "No stream_id found for channel", {})
            with mock.patch.object(server, "_guide_db_path", return_value=guide_db), \
                    mock.patch.object(server, "_stream_url", fake_stream):
                data = server.app.test_client().get("/epg-web/api/eaglecast-dependency").get_json()
        self.assertEqual(data["upcoming_would_break"], 1)
        self.assertEqual({r["title"]: r["primestreams_has_it"] for r in data["upcoming"]},
                         {"Film A": True, "Film B": False})
        self.assertEqual(data["recent_channels"][0]["recordings"], 1)
        self.assertNotIn("secret", str(data))


class EaglecastSwitchAndMoveTests(unittest.TestCase):
    def _db(self, temp):
        guide_db = os.path.join(temp, "guide.db")
        server.ensure_guide_db(guide_db)
        with mock.patch.object(server, "_guide_db_path", return_value=guide_db):
            server._init_recordings_table()
        conn = sqlite3.connect(guide_db)
        now = time.time()
        rows = [("covered", "Film A", "HBO", "hbo", now + 7200, "queued", "eaglecast", "111"),
                ("uncovered", "Film B", "STARZ 1", "starz1", now + 7200, "queued", "eaglecast", "176929"),
                ("too_soon", "Film C", "HBO", "hbo", now + 60, "queued", "eaglecast", "111"),
                ("on_ps", "Film D", "HBO", "hbo", now + 7200, "queued", "primestreams", "222")]
        conn.executemany(
            "INSERT INTO recordings(rec_id,title,channel,channel_id,start_ts,stop_ts,status,stream_provider,stream_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)", [(r[0], r[1], r[2], r[3], r[4], r[4] + 3600, r[5], r[6], r[7]) for r in rows])
        conn.commit(); conn.close()
        return guide_db

    @staticmethod
    def _fake_stream(channel_id, preferred=None):
        assert preferred == "primestreams"
        if channel_id == "hbo":
            return "url-with-secret", None, {"stream_id": "95926", "stream_extension": "ts"}
        return None, "No stream_id found for channel", {}

    def _status(self, guide_db, rec_id):
        conn = sqlite3.connect(guide_db)
        row = conn.execute("SELECT stream_provider, stream_id FROM recordings WHERE rec_id=?", (rec_id,)).fetchone()
        conn.close()
        return row

    def test_without_apply_it_only_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = self._db(temp)
            with mock.patch.object(server, "_guide_db_path", return_value=guide_db), \
                    mock.patch.object(server, "_stream_url", self._fake_stream):
                data = server.app.test_client().post("/epg-web/api/eaglecast/move-to-primestreams", json={}).get_json()
            self.assertFalse(data["applied"])
            self.assertEqual(data["moved"], 0)
            self.assertEqual([e["id"] for e in data["can_move"]], ["covered"])
            self.assertEqual([e["id"] for e in data["cannot_move"]], ["uncovered"])
            self.assertEqual(self._status(guide_db, "covered"), ("eaglecast", "111"))
            self.assertNotIn("secret", str(data))

    def test_apply_moves_only_what_primestreams_can_carry(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = self._db(temp)
            with server._rec_lock:
                server._recs["covered"] = {"stream_provider": "eaglecast", "stream_id": "111"}
            try:
                with mock.patch.object(server, "_guide_db_path", return_value=guide_db), \
                        mock.patch.object(server, "_stream_url", self._fake_stream):
                    data = server.app.test_client().post("/epg-web/api/eaglecast/move-to-primestreams",
                                                         json={"apply": True}).get_json()
                self.assertEqual(data["moved"], 1)
                self.assertEqual(self._status(guide_db, "covered"), ("primestreams", "95926"))
                self.assertEqual(self._status(guide_db, "uncovered"), ("eaglecast", "176929"))   # left alone
                self.assertEqual(self._status(guide_db, "too_soon"), ("eaglecast", "111"))       # starts too soon
                self.assertEqual(server._recs["covered"]["stream_provider"], "primestreams")
            finally:
                with server._rec_lock:
                    server._recs.pop("covered", None)

    def test_switching_eaglecast_off_removes_it_from_source_choice_and_back_on_restores_it(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            server.ensure_guide_db(guide_db)
            conn = sqlite3.connect(guide_db)
            conn.execute("INSERT INTO provider_streams(provider,channel_id,stream_id,provider_channel_name,stream_extension,"
                         "guide_channel_name,mapped_at) VALUES ('eaglecast','x1','5','US| HBO','ts','HBO',0)")
            conn.commit(); conn.close()
            cfg = {}
            with mock.patch.object(server, "load_config", lambda: cfg), \
                    mock.patch.object(server, "save_config", lambda c: cfg.update(c)), \
                    mock.patch.object(server, "_guide_db_path", return_value=guide_db):
                self.assertIn("x1", server.get_eaglecast_channel_ids(guide_db))
                self.assertIsNotNone(server._eaglecast_stream_for_channel("x1"))
                reply = server.app.test_client().post("/epg-web/api/eaglecast", json={"enabled": False})
                self.assertFalse(reply.get_json()["enabled"])
                self.assertEqual(server.get_eaglecast_channel_ids(guide_db), set())
                self.assertIsNone(server._eaglecast_stream_for_channel("x1"))
                server.app.test_client().post("/epg-web/api/eaglecast", json={"enabled": True})
                self.assertIn("x1", server.get_eaglecast_channel_ids(guide_db))
                bad = server.app.test_client().post("/epg-web/api/eaglecast", json={"enabled": "no"})
                self.assertEqual(bad.status_code, 400)


class EaglecastExpiryTests(unittest.TestCase):

    def test_a_typed_end_date_is_used_when_the_panel_gives_none(self):
        cfg = {"eaglecast_expires": "2026-11-22", "timezone": "America/New_York"}
        with mock.patch.object(server, "load_config", lambda: cfg), \
                mock.patch.object(server, "_eaglecast_panel_expiry", return_value=None):
            ts, source = server._eaglecast_expiry()
        from datetime import datetime as _dt, timezone as _tz
        self.assertEqual(_dt.fromtimestamp(ts, _tz.utc).strftime("%Y-%m-%d %H"), "2026-11-23 04")  # 23:59 Eastern
        self.assertEqual(source, "the date you entered")

    def test_the_provider_account_date_wins_over_a_typed_one(self):
        cfg = {"eaglecast_expires": "2026-11-22"}
        with mock.patch.object(server, "load_config", lambda: cfg), \
                mock.patch.object(server, "_eaglecast_panel_expiry", return_value=1_800_000_000.0):
            self.assertEqual(server._eaglecast_expiry(), (1_800_000_000.0, "the provider account"))

    def test_the_switch_reports_and_saves_the_end_date_and_refuses_a_bad_one(self):
        cfg = {"timezone": "America/New_York"}
        with mock.patch.object(server, "load_config", lambda: cfg), \
                mock.patch.object(server, "save_config", lambda c: cfg.update(c)), \
                mock.patch.object(server, "_eaglecast_panel_expiry", return_value=None):
            client = server.app.test_client()
            data = client.post("/epg-web/api/eaglecast", json={"expires": "2999-01-01"}).get_json()
            self.assertEqual(data["expires"], "2999-01-01")
            self.assertTrue(data["enabled"])
            self.assertEqual(client.post("/epg-web/api/eaglecast", json={"expires": "22/11"}).status_code, 400)
            self.assertEqual(client.post("/epg-web/api/eaglecast", json={"expires": ""}).get_json()["expires"], "")

    def test_a_subscription_that_has_run_out_switches_eaglecast_off(self):
        cfg = {"eaglecast_expires": "2000-01-01"}
        with mock.patch.object(server, "load_config", lambda: cfg), \
                mock.patch.object(server, "_eaglecast_panel_expiry", return_value=None):
            self.assertFalse(server._eaglecast_enabled())

    def test_an_airing_past_the_end_date_uses_primestreams_or_is_refused(self):
        expiry = time.time() + 86400
        covered = ("ps-url", None, {"provider": "primestreams", "stream_id": "9"})
        with mock.patch.object(server, "_eaglecast_stream_for_channel", return_value=("5", "US| HBO", "ts")), \
                mock.patch.object(server, "_eaglecast_expiry", return_value=(expiry, "the date you entered")), \
                mock.patch.object(server, "_eaglecast_overlap", return_value=None), \
                mock.patch.object(server, "_is_quarantined_eaglecast_stream", return_value=False):
            with mock.patch.object(server, "_stream_url", lambda cid, pref=None: covered if pref == "primestreams"
                                   else ("ec-url", None, {"provider": "eaglecast"})):
                before = server._resolve_recording_source("x", time.time() + 100, time.time() + 3000)
                after = server._resolve_recording_source("x", expiry + 100, expiry + 3000)
            self.assertEqual(before[2]["provider"], "eaglecast")
            self.assertEqual(after[2]["provider"], "primestreams")
            with mock.patch.object(server, "_stream_url", lambda cid, pref=None: (None, "No stream_id found for channel", {})
                                   if pref == "primestreams" else ("ec-url", None, {"provider": "eaglecast"})):
                url, error, _debug = server._resolve_recording_source("x", expiry + 100, expiry + 3000)
        self.assertIsNone(url)
        self.assertIn("subscription ends", error)
        self.assertEqual(server._play_blocked_note(error).startswith("The Eaglecast subscription ends"), True)

    def test_only_bookings_past_the_end_date_are_moved_automatically(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = os.path.join(temp, "guide.db")
            server.ensure_guide_db(guide_db)
            with mock.patch.object(server, "_guide_db_path", return_value=guide_db):
                server._init_recordings_table()
            now = time.time()
            expiry = now + 86400
            conn = sqlite3.connect(guide_db)
            conn.executemany(
                "INSERT INTO recordings(rec_id,title,channel,channel_id,start_ts,stop_ts,status,stream_provider,stream_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [("before", "Film A", "HBO", "hbo", now + 7200, now + 10800, "queued", "eaglecast", "111"),
                 ("after", "Film B", "HBO", "hbo", expiry + 7200, expiry + 10800, "queued", "eaglecast", "111"),
                 ("after_stuck", "Film C", "STARZ 1", "starz1", expiry + 9000, expiry + 12000, "queued", "eaglecast", "7")])
            conn.commit(); conn.close()

            def fake(cid, pref=None):
                return (("u", None, {"stream_id": "95926", "stream_extension": "ts"}) if cid == "hbo"
                        else (None, "No stream_id found for channel", {}))
            with mock.patch.object(server, "_guide_db_path", return_value=guide_db), \
                    mock.patch.object(server, "_stream_url", fake), \
                    mock.patch.object(server, "_eaglecast_expiry", return_value=(expiry, "the date you entered")):
                result = server._move_eaglecast_bookings(True, after_expiry_only=True)
            conn = sqlite3.connect(guide_db)
            state = dict(conn.execute("SELECT rec_id, stream_provider FROM recordings").fetchall())
            conn.close()
        self.assertEqual(result["moved"], 1)
        self.assertEqual(state, {"before": "eaglecast", "after": "primestreams", "after_stuck": "eaglecast"})
        self.assertEqual([e["id"] for e in result["cannot_move"]], ["after_stuck"])

    def test_without_an_end_date_the_after_expiry_move_refuses(self):
        with mock.patch.object(server, "_eaglecast_expiry", return_value=(None, "")):
            self.assertIn("error", server._move_eaglecast_bookings(True, after_expiry_only=True))


class AccountRemindersTests(unittest.TestCase):
    def _cfg(self, **extra):
        cfg = {"timezone": "America/New_York"}
        cfg.update(extra)
        return cfg

    @staticmethod
    def _in_days(days):
        from datetime import datetime as _dt, timedelta as _td
        return (server._local_dt(time.time()) + _td(days=days)).strftime("%Y-%m-%d")

    def test_a_date_within_the_window_is_due_and_a_far_one_is_not(self):
        cfg = self._cfg(sd_expires=self._in_days(20))
        with mock.patch.object(server, "load_config", lambda: cfg), \
                mock.patch.object(server, "_eaglecast_expiry", return_value=(None, "")):
            items = {i["key"]: i for i in server._reminder_items()}
            self.assertTrue(items["schedules_direct"]["due"])          # inside 30 days
            self.assertIn("runs out in 20 days", items["schedules_direct"]["message"])
            cfg["sd_expires"] = self._in_days(200)
            self.assertFalse({i["key"]: i for i in server._reminder_items()}["schedules_direct"]["due"])
            self.assertNotIn("eaglecast", {i["key"] for i in server._reminder_items()})   # no date known

    def test_eaglecast_reminds_inside_a_week_and_an_expired_account_says_so(self):
        cfg = self._cfg(sd_expires=self._in_days(-2))
        with mock.patch.object(server, "load_config", lambda: cfg), \
                mock.patch.object(server, "_eaglecast_expiry", return_value=(time.time() + 5 * 86400, "x")):
            items = {i["key"]: i for i in server._reminder_items()}
        self.assertTrue(items["eaglecast"]["due"])
        self.assertTrue(items["schedules_direct"]["due"])
        self.assertTrue(items["schedules_direct"]["message"].startswith("Schedules Direct ran out on"))

    def test_primestreams_is_never_tracked(self):
        self.assertNotIn("primestreams", server.REMINDER_WINDOWS)

    def test_the_endpoint_saves_a_date_and_refuses_bad_input(self):
        cfg = self._cfg()
        with mock.patch.object(server, "load_config", lambda: cfg), \
                mock.patch.object(server, "save_config", lambda c: cfg.update(c)), \
                mock.patch.object(server, "_eaglecast_expiry", return_value=(None, "")):
            client = server.app.test_client()
            reply = client.post("/epg-web/api/reminders", json={"key": "schedules_direct", "expires": "2027-05-05"})
            self.assertEqual(cfg["sd_expires"], "2027-05-05")
            self.assertEqual(reply.get_json()["reminders"][0]["expires"], "2027-05-05")
            self.assertEqual(client.post("/epg-web/api/reminders", json={"key": "nope", "expires": "2027-05-05"}).status_code, 400)
            self.assertEqual(client.post("/epg-web/api/reminders", json={"key": "schedules_direct", "expires": "05/05/27"}).status_code, 400)

    def test_the_page_has_the_reminder_bar(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn('id="reminder-bar"', html)
        self.assertIn("async function loadReminders()", html)


class SameAiringElsewhereTests(unittest.TestCase):
    def _setup(self, temp):
        guide_db = os.path.join(temp, "guide.db")
        server.ensure_guide_db(guide_db)
        with mock.patch.object(server, "_guide_db_path", return_value=guide_db):
            server._init_recordings_table()
        self.now = time.time()
        self.start = int(self.now) + 7200
        conn = sqlite3.connect(guide_db)
        for column, kind in (("episode_title", "TEXT"), ("season_num", "INTEGER"), ("episode_num", "INTEGER")):
            try:
                conn.execute(f"ALTER TABLE guide ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError:
                pass                                     # already there
        conn.execute("INSERT INTO recordings(rec_id,title,channel,channel_id,start_ts,stop_ts,guide_stop_ts,status,stream_provider,stream_id) "
                     "VALUES ('mob','MobLand','Paramount+ with Showtime HD (Pacific)','hd22532',?,?,?,'queued','eaglecast','92020')",
                     (self.start, self.start + 3700, self.start + 3600))
        self.alt_start = self.start + 120                  # the other channel's row starts two minutes later
        stamp = lambda ts: time.strftime("%Y%m%d%H%M%S", time.gmtime(ts))
        conn.execute("INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,category) VALUES (?,?,?,?,?,?,?)",
                     ("MobLand", "plain", "Paramount+ with Showtime (Pacific)", stamp(self.alt_start), stamp(self.alt_start + 3600), "", ""))
        conn.commit(); conn.close()
        return guide_db

    @staticmethod
    def _stream(channel_id, preferred=None):
        if channel_id == "plain":
            return "secret-url", None, {"stream_id": "39223", "stream_extension": "ts"}
        return None, "No stream_id found for channel", {}

    def _row(self, guide_db):
        conn = sqlite3.connect(guide_db)
        row = conn.execute("SELECT channel_id, channel, start_ts, stop_ts, guide_stop_ts, stream_provider, stream_id "
                           "FROM recordings WHERE rec_id='mob'").fetchone()
        conn.close()
        return row

    def _run(self, guide_db, apply_it, cfg=None):
        with mock.patch.object(server, "_guide_db_path", return_value=guide_db), \
                mock.patch.object(server, "_stream_url", self._stream), \
                mock.patch.object(server, "load_config", lambda: cfg or {}), \
                mock.patch.object(server, "_eaglecast_expiry", return_value=(None, "")):
            return server._move_eaglecast_bookings(apply_it)

    def test_a_dry_run_names_the_other_channel_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = self._setup(temp)
            data = self._run(guide_db, False)
            self.assertEqual(data["cannot_move"], [])
            self.assertEqual(data["can_move"][0]["via"], "Paramount+ with Showtime (Pacific)")
            self.assertEqual(self._row(guide_db)[0], "hd22532")
            self.assertNotIn("secret", str(data))

    def test_apply_switches_channel_source_and_times_together(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = self._setup(temp)
            data = self._run(guide_db, True)
            self.assertEqual(data["moved"], 1)
            channel_id, channel, start, stop, guide_stop, provider, stream_id = self._row(guide_db)
            self.assertEqual((channel_id, channel, provider, stream_id),
                             ("plain", "Paramount+ with Showtime (Pacific)", "primestreams", "39223"))
            self.assertEqual(int(start), self.alt_start)
            self.assertEqual(int(stop), self.start + 3700 + 120)            # padding kept, shifted with the start
            self.assertEqual(int(guide_stop), self.start + 3600 + 120)

    def test_an_airing_already_booked_elsewhere_is_not_booked_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = self._setup(temp)
            conn = sqlite3.connect(guide_db)
            conn.execute("INSERT INTO recordings(rec_id,title,channel,channel_id,start_ts,stop_ts,status,stream_provider) "
                         "VALUES ('twin','MobLand','Paramount+ with Showtime (Pacific)','plain',?,?,'queued','primestreams')",
                         (self.alt_start, self.alt_start + 3600))
            conn.commit(); conn.close()
            data = self._run(guide_db, True)
            self.assertEqual(data["moved"], 0)
            self.assertEqual([e["id"] for e in data["cannot_move"]], ["mob"])
            self.assertEqual(self._row(guide_db)[0], "hd22532")

    def test_a_bad_channel_is_never_chosen(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db = self._setup(temp)
            data = self._run(guide_db, True, cfg={"bad_channels": ["Paramount+ with Showtime (Pacific)"]})
            self.assertEqual(data["moved"], 0)
            self.assertEqual([e["id"] for e in data["cannot_move"]], ["mob"])


    def _episode_setup(self, temp, east_gap_hours=3):
        """The booked west row, and the same episode on an east row some hours earlier."""
        guide_db = self._setup(temp)
        stamp = lambda ts: time.strftime("%Y%m%d%H%M%S", time.gmtime(ts))
        conn = sqlite3.connect(guide_db)
        conn.execute("DELETE FROM guide")
        self.start = int(self.now) + 8 * 3600                   # the booked west airing is eight hours out
        conn.execute("UPDATE recordings SET start_ts=?, stop_ts=?, guide_stop_ts=? WHERE rec_id='mob'",
                     (self.start, self.start + 3700, self.start + 3600))
        east = self.start - east_gap_hours * 3600
        conn.execute("INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,category,episode_title,season_num,episode_num) "
                     "VALUES ('MobLand','hd22532','Paramount+ with Showtime HD (Pacific)',?,?,'','','The Queen Is Dead',2,10)",
                     (stamp(self.start), stamp(self.start + 3600)))
        conn.execute("INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,category,episode_title,season_num,episode_num) "
                     "VALUES ('MobLand','plain','Paramount+ with Showtime HD',?,?,'','','The Queen Is Dead',2,10)",
                     (stamp(east), stamp(east + 3600)))
        conn.execute("INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,category,episode_title,season_num,episode_num) "
                     "VALUES ('MobLand','plain','Paramount+ with Showtime HD',?,?,'','','Nervous Breakdown',2,9)",
                     (stamp(east + 7 * 86400), stamp(east + 7 * 86400 + 3600)))
        conn.commit(); conn.close()
        return guide_db, east

    def test_the_same_episode_on_the_east_feed_is_used_when_nothing_airs_at_the_same_time(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db, east = self._episode_setup(temp)
            data = self._run(guide_db, True)
            self.assertEqual(data["moved"], 1)
            row = self._row(guide_db)
            self.assertEqual((row[0], row[5]), ("plain", "primestreams"))
            self.assertEqual(int(row[2]), east)                                # now at the east airing's time
            self.assertEqual(int(row[3]), east + 3700)                         # the recording keeps its padding

    def test_a_different_episode_a_week_later_is_not_used(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db, _east = self._episode_setup(temp)
            conn = sqlite3.connect(guide_db)
            conn.execute("DELETE FROM guide WHERE channel_id='plain' AND episode_num=10")     # only the other episode is left
            conn.commit(); conn.close()
            data = self._run(guide_db, True)
            self.assertEqual(data["moved"], 0)
            self.assertEqual(self._row(guide_db)[0], "hd22532")

    def test_an_episode_already_booked_is_not_booked_again_at_another_time(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db, east = self._episode_setup(temp)
            conn = sqlite3.connect(guide_db)
            conn.execute("INSERT INTO recordings(rec_id,title,channel,channel_id,start_ts,stop_ts,status,stream_provider,season_num,episode_num) "
                         "VALUES ('already','MobLand','SHOWTIME','showtime.us',?,?,'queued','primestreams',2,10)",
                         (east + 86400, east + 86400 + 3600))
            conn.commit(); conn.close()
            data = self._run(guide_db, True)
            self.assertEqual(data["moved"], 0)
            self.assertEqual(self._row(guide_db)[0], "hd22532")

    def test_an_east_airing_that_is_too_close_to_start_is_not_used(self):
        with tempfile.TemporaryDirectory() as temp:
            guide_db, _east = self._episode_setup(temp, east_gap_hours=2)      # east airing starts at about now
            conn = sqlite3.connect(guide_db)
            conn.execute("UPDATE recordings SET start_ts=?, stop_ts=?, guide_stop_ts=? WHERE rec_id='mob'",
                         (self.now + 5400, self.now + 9000, self.now + 8900))   # booked 90 minutes out
            stamp = lambda ts: time.strftime("%Y%m%d%H%M%S", time.gmtime(ts))
            conn.execute("UPDATE guide SET start_utc=?, end_utc=? WHERE channel_id='plain' AND episode_num=10",
                         (stamp(self.now + 120), stamp(self.now + 3720)))        # two minutes from now
            conn.commit(); conn.close()
            self.assertEqual(self._run(guide_db, True)["moved"], 0)


class SeriesNoDuplicateBookingTests(unittest.TestCase):
    """One airing, or one episode, is booked once even when several channels carry it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.guide_db = os.path.join(self.temp.name, "guide.db")
        server.ensure_guide_db(self.guide_db)
        with mock.patch.object(server, "_guide_db_path", return_value=self.guide_db):
            server._init_recordings_table()
        conn = sqlite3.connect(self.guide_db)
        for column, kind in (("episode_title", "TEXT"), ("season_num", "INTEGER"), ("episode_num", "INTEGER")):
            try:
                conn.execute(f"ALTER TABLE guide ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError:
                pass
        conn.commit(); conn.close()
        self.base = int(time.time()) + 3 * 86400
        with server._rec_lock:
            server._recs.clear()

    def tearDown(self):
        with server._rec_lock:
            server._recs.clear()
        self.temp.cleanup()

    def _guide(self, rows):
        stamp = lambda ts: time.strftime("%Y%m%d%H%M%S", time.gmtime(ts))
        conn = sqlite3.connect(self.guide_db)
        conn.executemany(
            "INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,category,episode_title,season_num,episode_num) "
            "VALUES ('Show',?,?,?,?,'','',?,?,?)",
            [(cid, name, stamp(start), stamp(start + 3600), ep, sn, en) for cid, name, start, ep, sn, en in rows])
        conn.commit(); conn.close()

    def _run(self):
        channels = {"hd", "west", "plain", "other"}
        with mock.patch.object(server, "_guide_db_path", return_value=self.guide_db), \
                mock.patch.object(server, "load_config", return_value={}), \
                mock.patch.object(server, "get_recordable_channel_ids", return_value=channels), \
                mock.patch.object(server, "_plex_episode_keys", side_effect=lambda *a, **k: set(getattr(self, "plex_keys", set()))), \
                mock.patch.object(server, "_plex_tv_path", return_value=self.temp.name), \
                mock.patch.object(server, "_recording_backend", return_value="agent"), \
                mock.patch.object(server, "_db_upsert_rec", return_value=True), \
                mock.patch.object(server, "_recent_same_source_failure", return_value=False), \
                mock.patch.object(server, "_resolve_recording_source",
                                  return_value=("u", None, {"stream_id": "1", "provider": "primestreams"})):
            return server._schedule_series_airings("Show", self.guide_db, "movies.db")

    def _starts(self):
        with server._rec_lock:
            return sorted(round(r["start_ts"]) for r in server._recs.values())

    def test_a_row_with_no_episode_number_at_the_same_time_is_not_booked_again(self):
        self._guide([("hd", "Show HD", self.base, "Pilot", 1, 1),
                     ("plain", "Show", self.base, None, None, None)])
        self.assertEqual(self._run(), 1)
        self.assertEqual(self._starts(), [self.base])

    def test_the_same_episode_on_an_east_and_a_west_feed_is_booked_once(self):
        self._guide([("hd", "Show HD", self.base, "Pilot", 1, 1),
                     ("west", "Show West", self.base + 3 * 3600, "Pilot", 1, 1)])
        self.assertEqual(self._run(), 1)

    def test_an_episode_already_booked_is_not_booked_again_on_another_channel(self):
        self._guide([("west", "Show West", self.base + 3 * 3600, "Pilot", 1, 1)])
        with server._rec_lock:
            server._recs["existing"] = {"title": "Show", "channel_id": "hd", "channel": "Show HD", "start_ts": float(self.base),
                                        "stop_ts": float(self.base + 3600), "status": "queued", "season_num": 1, "episode_num": 1}
        self.assertEqual(self._run(), 0)

    def test_an_airing_with_no_episode_number_at_a_booked_time_is_not_booked_again(self):
        self._guide([("plain", "Show", self.base + 60, None, None, None)])
        with server._rec_lock:
            server._recs["existing"] = {"title": "Show", "channel_id": "hd", "channel": "Show HD", "start_ts": float(self.base),
                                        "stop_ts": float(self.base + 3600), "status": "queued", "season_num": 1, "episode_num": 1}
        self.assertEqual(self._run(), 0)

    def test_different_episodes_and_different_times_are_still_booked(self):
        self._guide([("hd", "Show HD", self.base, "Pilot", 1, 1),
                     ("hd", "Show HD", self.base + 7 * 86400, "Second", 1, 2),
                     ("plain", "Show", self.base + 3 * 86400, None, None, None)])
        self.assertEqual(self._run(), 3)


class HideNonEnglishTests(unittest.TestCase):
    def test_the_classifier(self):
        classify = server._is_non_english_channel
        for name in ("Telemundo", "UNIVISION", "ABS-CBN News Channel", "TV5 French", "HBO Latino HD", "CINE ESPAÑOL",
                     "Россия 24", "NHK 世界", "ZEE TV"):
            self.assertTrue(classify(name), name)
        for name in ("ESPN", "HBO", "Paramount+ with Showtime HD", "AMC (WEST)", "Hallmark Channel", "AFRO"):
            self.assertFalse(classify(name), name)
        self.assertTrue(classify("Some Channel", "es"))                 # Schedules Direct says Spanish
        self.assertTrue(classify("Some Channel", "fr,es"))
        self.assertFalse(classify("Some Channel", "en"))
        self.assertFalse(classify("Some Channel", "en,es"))             # bilingual feeds stay
        self.assertFalse(classify("Some Channel", "en-CA"))             # an English variant is still English
        self.assertFalse(classify("Some Channel", "en,fr-CA"))
        self.assertTrue(classify("Some Channel", "fr-CA"))
        self.assertTrue(classify("Some Channel", "tl,zh"))
        self.assertFalse(classify("Some Channel", ""))                  # language unknown: judged by name only

    def _guide(self, hide):
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc).replace(minute=0, second=0, microsecond=0)
        start = now.timestamp()
        channels = [{"id": cid, "name": name, "icon": "", "language": lang}
                    for cid, name, lang in (("c1", "ESPN", "en"), ("c2", "Telemundo", ""), ("c3", "Plain Name", "es"))]
        progs = [{"title": f"Show {c['id']}", "channel_id": c["id"], "channel": c["name"], "start_ts": start,
                  "stop_ts": start + 3600, "start_iso": "", "stop_iso": "", "start_fmt": "", "stop_fmt": "",
                  "desc": "", "category": ""} for c in channels]
        epg = {"channels": channels, "channel_map": {c["id"]: c["name"] for c in channels}, "programmes": progs,
               "programme_starts": [start] * len(progs)}
        query = "/epg-web/api/guide?hours=2" + ("&hide_foreign=1" if hide else "")
        deadline = time.time() + 10              # the startup guide load must not overwrite the sample guide
        while time.time() < deadline and (server._startup_status.get("loading") or server._startup_status.get("background_loading")):
            time.sleep(0.05)
        with mock.patch.dict(server._epg, epg), \
                mock.patch.object(server, "load_config", lambda: {"timezone": "UTC"}):
            data = server.app.test_client().get(query).get_json()
        return data

    def test_the_guide_hides_non_english_channels_and_their_programmes_only_when_asked(self):
        shown = self._guide(False)
        self.assertEqual(sorted(c["name"] for c in shown["channels"]), ["ESPN", "Plain Name", "Telemundo"])
        hidden = self._guide(True)
        self.assertEqual([c["name"] for c in hidden["channels"]], ["ESPN"])
        self.assertEqual([p["channel_id"] for p in hidden["programmes"]], ["c1"])

    def test_the_page_has_the_switch_and_sends_it(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn('id="hide-foreign"', html)
        self.assertIn("params.set('hide_foreign', '1')", html)
        self.assertIn("epg_hide_foreign", html)
        self.assertRegex(html, r'<input id="hide-foreign" type="checkbox" checked')            # on by default
        self.assertIn("getItem('epg_hide_foreign') !== '0'", html)                              # off only if you switched it off


class AdultLockTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"timezone": "UTC"}
        self.patches = [mock.patch.object(server, "load_config", lambda: self.cfg),
                        mock.patch.object(server, "save_config", lambda c: self.cfg.update(c))]
        for p in self.patches:
            p.start()
        with server._adult_lock:
            server._adult_tokens.clear()
            server._adult_failures.update({"count": 0, "first": 0.0, "locked_until": 0.0})
        self.client = server.app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_what_counts_as_adult(self):
        for name in ("BraZZers TV Plus", "Playboy TV", "PENTHOUSE", "Hustler TV", "Private Spice", "Dorcel TV", "XXX Hits"):
            self.assertTrue(server._is_adult_channel(name), name)
        for name in ("Adult Swim", "ESPN", "Hallmark Channel", "HBO", "Private Practice Reruns", "Starz Encore"):
            self.assertFalse(server._is_adult_channel(name), name)
        self.assertTrue(server._is_adult_programme("Adults only"))
        self.assertFalse(server._is_adult_programme("Drama"))
        self.assertTrue(server._is_automatic_recording_quarantined_channel("Playboy TV"))     # never booked by automation

    def test_only_a_salted_hash_is_stored_and_it_checks_out(self):
        stored = server._hash_password("open sesame")
        self.assertNotIn("open sesame", stored)
        self.assertTrue(server._password_matches("open sesame", stored))
        self.assertFalse(server._password_matches("open sesame ", stored))
        self.assertNotEqual(server._hash_password("open sesame"), stored)                     # fresh salt each time

    def test_set_unlock_and_lock(self):
        post = lambda path, body, headers=None: self.client.post("/epg-web/api/adult" + path, json=body, headers=headers or {})
        self.assertEqual(self.client.get("/epg-web/api/adult").get_json(), {"password_set": False, "unlocked": False})
        self.assertEqual(post("/password", {"new": "abc"}).status_code, 400)                    # too short
        reply = post("/password", {"new": "secret1"}).get_json()
        self.assertTrue(reply["ok"])
        self.assertNotIn("secret1", str(self.cfg))                                               # never stored in the clear
        self.assertEqual(post("/unlock", {"password": "nope"}).status_code, 403)
        token = post("/unlock", {"password": "secret1"}).get_json()["token"]
        status = self.client.get("/epg-web/api/adult", headers={"X-Adult-Token": token}).get_json()
        self.assertEqual(status, {"password_set": True, "unlocked": True})
        post("/lock", {}, {"X-Adult-Token": token})
        self.assertFalse(self.client.get("/epg-web/api/adult", headers={"X-Adult-Token": token}).get_json()["unlocked"])

    def test_changing_or_removing_needs_the_current_password(self):
        post = lambda body: self.client.post("/epg-web/api/adult/password", json=body)
        post({"new": "secret1"})
        self.assertEqual(post({"new": "other22", "current": "wrong"}).status_code, 403)
        self.assertEqual(post({"new": "", "current": "wrong"}).status_code, 403)
        self.assertTrue(post({"new": "other22", "current": "secret1"}).get_json()["ok"])
        self.assertTrue(server._password_matches("other22", self.cfg["adult_password_hash"]))
        self.assertTrue(post({"new": "", "current": "other22"}).get_json()["ok"])
        self.assertNotIn("adult_password_hash", self.cfg)                                        # protection removed

    def test_too_many_wrong_guesses_pause_trying(self):
        self.client.post("/epg-web/api/adult/password", json={"new": "secret1"})
        for _ in range(server.ADULT_MAX_FAILURES):
            self.client.post("/epg-web/api/adult/unlock", json={"password": "bad"})
        reply = self.client.post("/epg-web/api/adult/unlock", json={"password": "secret1"})      # even the right one is refused now
        self.assertEqual(reply.status_code, 429)

    def test_a_token_expires(self):
        self.client.post("/epg-web/api/adult/password", json={"new": "secret1"})
        token = self.client.post("/epg-web/api/adult/unlock", json={"password": "secret1"}).get_json()["token"]
        with server._adult_lock:
            server._adult_tokens[token] = time.time() - 1
        self.assertFalse(self.client.get("/epg-web/api/adult", headers={"X-Adult-Token": token}).get_json()["unlocked"])

    def _guide(self, query, headers=None):
        from datetime import datetime as _dt, timezone as _tz
        start = _dt.now(_tz.utc).replace(minute=0, second=0, microsecond=0).timestamp()
        channels = [{"id": cid, "name": name, "icon": "", "language": ""}
                    for cid, name in (("c1", "ESPN"), ("c2", "Brazzers TV Plus"))]
        progs = [{"title": t, "channel_id": cid, "channel": name, "start_ts": start, "stop_ts": start + 3600, "start_iso": "",
                  "stop_iso": "", "start_fmt": "", "stop_fmt": "", "desc": "", "category": cat}
                 for t, cid, name, cat in (("Game", "c1", "ESPN", "Sports"), ("Adult film", "c1", "ESPN", "Adults only"),
                                           ("Feature", "c2", "Brazzers TV Plus", ""))]
        epg = {"channels": channels, "channel_map": {c["id"]: c["name"] for c in channels}, "programmes": progs,
               "programme_starts": [start] * len(progs)}
        deadline = time.time() + 10
        while time.time() < deadline and (server._startup_status.get("loading") or server._startup_status.get("background_loading")):
            time.sleep(0.05)
        with mock.patch.dict(server._epg, epg):
            data = self.client.get("/epg-web/api/guide?hours=2" + query, headers=headers or {}).get_json()
        return [c["name"] for c in data["channels"]], sorted(p["title"] for p in data["programmes"])

    def test_the_guide_hides_adult_channels_and_tagged_programmes_by_default(self):
        self.assertEqual(self._guide(""), (["ESPN"], ["Game"]))

    def test_with_no_password_the_page_can_ask_to_show_adult(self):
        channels, titles = self._guide("&hide_adult=0")
        self.assertEqual(channels, ["ESPN", "Brazzers TV Plus"])
        self.assertEqual(titles, ["Adult film", "Feature", "Game"])

    def test_with_a_password_asking_to_show_adult_is_ignored_without_the_token(self):
        self.client.post("/epg-web/api/adult/password", json={"new": "secret1"})
        self.assertEqual(self._guide("&hide_adult=0"), (["ESPN"], ["Game"]))                     # a changed page gets nothing
        token = self.client.post("/epg-web/api/adult/unlock", json={"password": "secret1"}).get_json()["token"]
        channels, _titles = self._guide("&hide_adult=0", {"X-Adult-Token": token})
        self.assertEqual(channels, ["ESPN", "Brazzers TV Plus"])

    def _view(self, query, headers=None, language_of=None):
        """The guide with a mixed set of channels, returning the raw JSON."""
        from datetime import datetime as _dt, timezone as _tz
        start = _dt.now(_tz.utc).replace(minute=0, second=0, microsecond=0).timestamp()
        rows = (("c1", "ESPN", "en"), ("c2", "Brazzers TV Plus", "en"), ("c3", "Telemundo", "es"), ("c4", "Plain Name", ""))
        channels = [{"id": cid, "name": name, "icon": "", "language": lang} for cid, name, lang in rows]
        progs = [{"title": f"Show {cid}", "channel_id": cid, "channel": name, "start_ts": start, "stop_ts": start + 3600,
                  "start_iso": "", "stop_iso": "", "start_fmt": "", "stop_fmt": "", "desc": "", "category": ""}
                 for cid, name, _lang in rows]
        epg = {"channels": channels, "channel_map": {c["id"]: c["name"] for c in channels}, "programmes": progs,
               "programme_starts": [start] * len(progs)}
        deadline = time.time() + 10
        while time.time() < deadline and (server._startup_status.get("loading") or server._startup_status.get("background_loading")):
            time.sleep(0.05)
        with mock.patch.dict(server._epg, epg):
            return self.client.get("/epg-web/api/guide?hours=2" + query, headers=headers or {}).get_json()

    def test_the_counts_say_how_many_channels_each_switch_hides(self):
        data = self._view("&hide_foreign=1")
        self.assertEqual(data["hidden_counts"], {"non_english": 1, "adult": 1})
        self.assertEqual(sorted(c["name"] for c in data["channels"]), ["ESPN", "Plain Name"])

    def test_the_non_english_view_shows_what_the_switch_hides(self):
        data = self._view("&hide_foreign=1&content=non_english")                  # the switch is on, the view still shows them
        self.assertEqual([c["name"] for c in data["channels"]], ["Telemundo"])
        self.assertEqual([p["channel_id"] for p in data["programmes"]], ["c3"])

    def test_the_adult_view_with_no_password_shows_only_adult_channels(self):
        data = self._view("&content=adult")
        self.assertEqual([c["name"] for c in data["channels"]], ["Brazzers TV Plus"])

    def test_the_adult_view_is_locked_by_the_password_on_the_server(self):
        self.client.post("/epg-web/api/adult/password", json={"new": "secret1"})
        locked = self._view("&content=adult&hide_adult=0")
        self.assertTrue(locked.get("adult_locked"))
        self.assertNotIn("channels", locked)                                      # nothing is sent at all
        token = self.client.post("/epg-web/api/adult/unlock", json={"password": "secret1"}).get_json()["token"]
        data = self._view("&content=adult", {"X-Adult-Token": token})
        self.assertEqual([c["name"] for c in data["channels"]], ["Brazzers TV Plus"])

    def test_the_page_renders_and_has_no_backslash_escapes_in_its_script(self):
        reply = server.app.test_client().get("/epg-web")
        self.assertEqual(reply.status_code, 200)                                  # a stray \\ud83d-style escape once made this a 500
        self.assertNotIn("\\ud83d", reply.get_data(as_text=True))

    def test_the_adult_controls_are_tucked_behind_the_admin_button(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn('<option value="non_english">', html)
        self.assertNotIn('<option value="adult">', html)                          # not in the page at all until an admin session adds it
        self.assertIn("option.value = 'adult'", html)
        self.assertIn('id="admin-btn"', html)
        self.assertIn("function setAdminMode", html)
        hidden_span = html.index('id="adult-controls" style="display:none;"')
        self.assertLess(hidden_span, html.index('id="hide-adult"'))               # the checkbox sits inside the hidden block
        self.assertLess(hidden_span, html.index('id="hidden-count-adult"'))
        self.assertIn('id="hidden-count-foreign"', html)
        self.assertIn("savedContent !== 'adult'", html)                           # never reopened on its own
        self.assertIn("setAdminMode(!!st.unlocked)", html)                        # on only for an unlocked tab

    def test_the_page_has_the_adult_controls_and_checkbox_spacing(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertRegex(html, r'<input id="hide-adult" type="checkbox" checked')
        self.assertIn("function onAdultToggle", html)
        self.assertIn("type=\"password\"", html)
        self.assertIn(".guide-toolbar input[type=checkbox]{width:auto", html)                    # label sits next to its box


class ProviderEpgProbeTests(unittest.TestCase):
    def test_probe_decodes_every_field_and_never_exposes_the_login(self):
        import base64, json as _json
        guide_db_dir = tempfile.TemporaryDirectory()
        guide_db = os.path.join(guide_db_dir.name, "guide.db")
        server.ensure_guide_db(guide_db)
        conn = sqlite3.connect(guide_db)
        conn.execute("INSERT INTO provider_stream_authority(provider,stream_id,epg_channel_id,provider_name,checked_at) "
                     "VALUES ('primestreams','777','antenna.us','ANTENNA TV',0)")
        conn.commit(); conn.close()
        enc = lambda text: base64.b64encode(text.encode()).decode()
        item = {"id": "1", "title": enc("Sanford and Son"), "description": enc("Fred fakes a heart attack."),
                "start_timestamp": 1800000000, "stop_timestamp": 1800001800, "season": "2", "episode_num": "16"}

        class Reply:
            def read(self):
                return _json.dumps({"epg_listings": [item]}).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        seen = []

        def fake_urlopen(req, timeout=0):
            seen.append(req.full_url)
            return Reply()
        cfg = {"epg_url": "http://provider.example", "epg_user": "USERNAME1", "epg_pass": "PASSWORD1"}
        with mock.patch.object(server, "load_config", lambda: cfg), \
                mock.patch.object(server, "_guide_db_path", return_value=guide_db), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            data = server.app.test_client().get("/epg-web/api/provider-epg-probe?name=antenna").get_json()
            bad = server.app.test_client().get("/epg-web/api/provider-epg-probe?name=a")
        guide_db_dir.cleanup()
        row = data["results"][0]
        self.assertEqual((row["stream_id"], row["provider_name"]), ("777", "ANTENNA TV"))
        self.assertEqual(row["items"][0]["title"], "Sanford and Son")
        self.assertEqual(row["items"][0]["episode_num"], "16")                 # unknown extra fields are passed through
        self.assertIn("episode_num", row["keys"])
        self.assertIn("get_short_epg", seen[0])
        text = str(data)
        self.assertNotIn("USERNAME1", text)
        self.assertNotIn("PASSWORD1", text)
        self.assertNotIn("provider.example", text)
        self.assertEqual(bad.status_code, 400)

    def test_probe_errors_do_not_leak_the_address(self):
        guide_db_dir = tempfile.TemporaryDirectory()
        cfg = {"epg_url": "http://provider.example", "epg_user": "USERNAME1", "epg_pass": "PASSWORD1"}

        def boom(req, timeout=0):
            raise OSError("failed for " + req.full_url)
        with mock.patch.object(server, "load_config", lambda: cfg), \
                mock.patch("urllib.request.urlopen", boom):
            data = server.app.test_client().get("/epg-web/api/provider-epg-probe?stream_id=777").get_json()
        guide_db_dir.cleanup()
        self.assertEqual(data["results"][0]["error"], "OSError")
        self.assertNotIn("PASSWORD1", str(data))


class OmdbEpisodesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(server, "BASE_DIR", self.temp.name),
                        mock.patch.object(server, "load_config", lambda: {"omdb_key": "SECRETKEY"}),
                        mock.patch.object(server, "_OMDB_BACKGROUND", False)]
        for p in self.patches:
            p.start()
        server._omdb_jobs.clear()
        self.calls = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        server._omdb_jobs.clear()
        self.temp.cleanup()

    def _fake(self, limit_after=None):
        def fake(params, key):
            self.calls.append(dict(params))
            assert key == "SECRETKEY"
            if limit_after is not None and len(self.calls) > limit_after:
                return {"Response": "False", "Error": "Request limit reached!"}
            if params.get("t"):
                return {"Response": "True", "imdbID": "tt1", "Title": "Test Show", "Year": "1965–1970", "totalSeasons": "2"}
            if params.get("Season"):
                n = int(params["Season"])
                return {"Response": "True", "Episodes": [
                    {"Title": f"Ep {n}-{e}", "Episode": str(e), "imdbID": f"tt{n}{e}"} for e in (1, 2)]}
            return {"Response": "True", "Plot": f"Plot of {params['i']}"}
        return fake

    def test_fetches_every_episode_plot_once_and_then_serves_from_the_file(self):
        with mock.patch.object(server, "_omdb_get", self._fake()):
            data = server.app.test_client().get("/epg-web/api/omdb-episodes?title=Test+Show").get_json()
            self.assertEqual(data["status"], "done")
            self.assertEqual([(e["season"], e["number"], e["summary"]) for e in data["episodes"]],
                             [(1, 1, "Plot of tt11"), (1, 2, "Plot of tt12"), (2, 1, "Plot of tt21"), (2, 2, "Plot of tt22")])
            self.assertEqual(data["premiered"], "1965")
            made = len(self.calls)
            again = server.app.test_client().get("/epg-web/api/omdb-episodes?title=Test+Show").get_json()
            self.assertEqual(again["status"], "done")
            self.assertEqual(len(self.calls), made)                                      # nothing refetched
        self.assertNotIn("SECRETKEY", str(data))
        self.assertNotIn("SECRETKEY", open(server._omdb_cache_path(), encoding="utf-8").read())

    def test_a_daily_limit_keeps_what_was_fetched_and_says_so(self):
        with mock.patch.object(server, "_omdb_get", self._fake(limit_after=5)):          # search, 2 lists, then 2 plots
            data = server.app.test_client().get("/epg-web/api/omdb-episodes?title=Test+Show").get_json()
        self.assertEqual(data["status"], "limit")
        summaries = [e["summary"] for e in data["episodes"]]
        self.assertEqual(sum(1 for text in summaries if text), 2)                       # the two it got are kept
        self.assertEqual(len(summaries), 4)

    def test_no_key_and_no_title_are_refused_and_errors_do_not_leak_the_key(self):
        client = server.app.test_client()
        self.assertEqual(client.get("/epg-web/api/omdb-episodes").status_code, 400)

        def boom(params, key):
            raise OSError("failed for key=" + key)
        with mock.patch.object(server, "_omdb_get", boom):
            data = client.get("/epg-web/api/omdb-episodes?title=Other+Show").get_json()
        self.assertEqual(data["status"], "error")
        self.assertNotIn("SECRETKEY", str(data))
        with mock.patch.object(server, "load_config", lambda: {}):
            self.assertEqual(client.get("/epg-web/api/omdb-episodes?title=X+Show").status_code, 503)


class GuideEpisodeGuessTests(unittest.TestCase):
    """The programme window shows the probable episode for an airing the guide gave no episode number."""
    SHOW = "Zzz Test Sitcom"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.guide_db = os.path.join(self.temp.name, "guide.db")
        server.ensure_guide_db(self.guide_db)
        conn = sqlite3.connect(self.guide_db)
        for column, typedef in (("episode_title", "TEXT"), ("season_num", "INTEGER"), ("episode_num", "INTEGER"), ("prog_type", "TEXT")):
            conn.execute(f"ALTER TABLE guide ADD COLUMN {column} {typedef}")
        conn.commit(); conn.close()
        self.key = server._norm_plex_show(self.SHOW)
        server._tmdb_episode_cache.pop(self.key, None)
        server._series_unknown.pop(self.key, None)
        server._series_warming.discard(self.key)
        self.cfg = {"guide_db_path": self.guide_db, "timezone": "UTC", "tmdb_key": "KEY"}
        self.patches = [mock.patch.object(server, "load_config", lambda: self.cfg),
                        mock.patch.object(server, "BASE_DIR", self.temp.name),
                        mock.patch.object(server, "_stream_url", return_value=("private-url", None, {})),
                        mock.patch.object(server, "_SERIES_WARM_BACKGROUND", False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        server._tmdb_episode_cache.pop(self.key, None)
        self.temp.cleanup()

    def _airings(self, rows, title=None):
        conn = sqlite3.connect(self.guide_db)
        conn.executemany("INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,prog_type) VALUES (?,?,?,?,?,?,?)",
                         [(self.SHOW, "ant", "ANTENNA TV", f"2099081{i}120000", f"2099081{i}123000", desc, "")
                          for i, desc in enumerate(rows, 1)])
        conn.commit(); conn.close()
        return server.app.test_client().get("/epg-web/api/airings?title=" + (title or self.SHOW).replace(" ", "+")).get_json()

    def _seed(self, episodes):
        server._tmdb_episode_cache[self.key] = {"at": time.time(), "data": {"title": self.SHOW, "premiered": "1965", "episodes": episodes}}

    EPISODES = [
        {"season": 2, "number": 15, "name": "Jeannie Breaks the Bank", "summary": "Tony draws attention when Jeannie puts three million dollars into his bank account."},
        {"season": 2, "number": 16, "name": "My Master, the Author", "summary": "Jeannie writes a book about child care and sends it to a publisher."},
        {"season": 2, "number": 17, "name": "The Greatest Invention", "summary": "Roger convinces Tony to let Jeannie grant him a wish."},
    ]

    def test_a_matching_description_shows_the_episode_and_makes_it_a_series(self):
        self._seed(self.EPISODES)
        data = self._airings(["Jeannie puts 3 million dollars in Tony's bank account.", "Nothing at all like any episode."])
        first, second = data["airings"][0], data["airings"][1]
        self.assertEqual((first["episode_guess"]["season"], first["episode_guess"]["number"]), (2, 15))
        self.assertTrue(first["episode_guess"]["confident"])
        self.assertNotIn("episode_guess", second)                 # a poor match is not shown
        self.assertTrue(data["is_series"])                        # so Record series is offered
        self.assertFalse(data["guess_pending"])
        self.assertNotIn("desc", first)

    def _numbered(self, desc, season, number, name, channel="FETV"):
        conn = sqlite3.connect(self.guide_db)
        conn.execute("INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,episode_title,season_num,episode_num,prog_type) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (self.SHOW, channel.lower(), channel, "20990701100000", "20990701103000", desc, name, season, number, "EP"))
        conn.commit(); conn.close()

    def test_the_same_description_on_a_numbered_airing_elsewhere_names_the_episode_exactly(self):
        text = "Darrin discovers that bedwarmers can be worse than mothers-in-law."
        self._numbered(text, 7, 15, "Mary, the Good Fairy")
        with mock.patch.object(server, "_warm_series") as warm:
            data = self._airings([text])                      # no TMDb data at all: the guide itself is enough
        ant = next(a for a in data["airings"] if a["channel_name"] == "ANTENNA TV")
        self.assertEqual((ant["episode_guess"]["season"], ant["episode_guess"]["number"]), (7, 15))
        self.assertEqual((ant["episode_guess"]["name"], ant["episode_guess"]["score"], ant["episode_guess"]["source"]),
                         ("Mary, the Good Fairy", 1.0, "guide"))
        self.assertTrue(data["is_series"])
        warm.assert_not_called()                              # nothing was left to guess

    def test_a_description_on_two_different_episodes_is_not_trusted(self):
        text = "A story that two different episodes happen to share in words."
        self._numbered(text, 1, 1, "One")
        self._numbered(text, 2, 2, "Two", channel="DTV323")
        with mock.patch.object(server, "_warm_series"):
            data = self._airings([text])
        self.assertNotIn("episode_guess", next(a for a in data["airings"] if a["channel_name"] == "ANTENNA TV"))

    def test_a_short_description_is_not_trusted(self):
        self._numbered("A short one.", 1, 1, "One")
        with mock.patch.object(server, "_warm_series"):
            data = self._airings(["A short one."])
        self.assertNotIn("episode_guess", next(a for a in data["airings"] if a["channel_name"] == "ANTENNA TV"))

    def test_the_naming_tool_is_told_about_exact_matches(self):
        text = "Darrin discovers that bedwarmers can be worse than mothers-in-law."
        self._numbered(text, 7, 15, "Mary, the Good Fairy")
        self._airings([text])
        data = server.app.test_client().get("/epg-web/api/guide-descriptions?title=" + self.SHOW.replace(" ", "+")).get_json()
        ant = next(a for a in data["airings"] if a["channel"] == "ANTENNA TV")
        self.assertEqual(ant["episode"], {"season": 7, "number": 15, "name": "Mary, the Good Fairy"})

    def test_a_different_description_still_falls_back_to_the_wording_match(self):
        self._numbered("Some other story entirely, about a boat.", 3, 3, "Three")
        self._seed(self.EPISODES)
        data = self._airings(["Jeannie puts 3 million dollars in Tony's bank account."])
        guess = next(a for a in data["airings"] if a["channel_name"] == "ANTENNA TV")["episode_guess"]
        self.assertEqual((guess["season"], guess["number"]), (2, 15))
        self.assertNotEqual(guess.get("source"), "guide")

    def test_debug_mode_shows_the_description_and_the_nearest_episodes_with_scores(self):
        self._seed(self.EPISODES)
        conn = sqlite3.connect(self.guide_db)
        conn.execute("INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,prog_type) VALUES (?,?,?,?,?,?,?)",
                     (self.SHOW, "ant", "ANTENNA TV", "20990830120000", "20990830123000", "A completely unrelated story about a tuba.", ""))
        conn.commit(); conn.close()
        plain = server.app.test_client().get("/epg-web/api/airings?title=" + self.SHOW.replace(" ", "+")).get_json()
        self.assertNotIn("guess_debug", plain["airings"][0])                          # off unless asked for
        data = server.app.test_client().get("/epg-web/api/airings?debug_guess=1&title=" + self.SHOW.replace(" ", "+")).get_json()
        debug = data["airings"][0]["guess_debug"]
        self.assertEqual(debug["desc"], "A completely unrelated story about a tuba.")
        self.assertEqual(len(debug["nearest"]), 3)
        self.assertEqual(set(debug["nearest"][0]), {"season", "number", "name", "score"})
        self.assertNotIn("episode_guess", data["airings"][0])                         # too poor to show, yet visible here

    def test_while_the_episode_list_is_fetched_the_window_is_told_to_ask_again(self):
        with mock.patch.object(server, "_warm_series") as warm:
            data = self._airings(["Jeannie writes a book."])
        warm.assert_called_once_with(self.SHOW)
        self.assertTrue(data["guess_pending"])
        self.assertNotIn("episode_guess", data["airings"][0])
        self.assertFalse(data["is_series"])

    def test_a_year_in_the_title_means_a_film_and_is_not_matched(self):
        self._seed(self.EPISODES)
        conn = sqlite3.connect(self.guide_db)
        conn.execute("INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,prog_type) VALUES (?,?,?,?,?,?,?)",
                     (self.SHOW + " (2005)", "ant", "ANTENNA TV", "20990820120000", "20990820140000", "Jeannie writes a book.", ""))
        conn.commit(); conn.close()
        with mock.patch.object(server, "_warm_series") as warm:
            data = server.app.test_client().get("/epg-web/api/airings?title=" + (self.SHOW + " (2005)").replace(" ", "+")).get_json()
        warm.assert_not_called()
        self.assertNotIn("episode_guess", data["airings"][0])

    def test_an_omdb_plot_can_make_the_match_the_tmdb_wording_could_not(self):
        episodes = [dict(e) for e in self.EPISODES]
        self._seed(episodes)
        with open(server._omdb_cache_path(), "w", encoding="utf-8") as handle:
            json.dump({self.key: {"episodes": [
                {"season": 2, "number": 17, "name": "The Greatest Invention", "summary": "Tony cannot change history when Jeannie takes him to Napoleon's court."}]}}, handle)
        data = self._airings(["Tony cannot change history when Jeannie takes him to Napoleon's court."])
        guess = data["airings"][0]["episode_guess"]
        self.assertEqual((guess["season"], guess["number"]), (2, 17))

    def test_a_series_tmdb_does_not_know_is_not_asked_about_again(self):
        calls = []

        def unknown(title, key):
            calls.append(title)
            return None, "TMDb has no series named", 404
        with mock.patch.object(server, "_fetch_tmdb_series", unknown):
            server._warm_series(self.SHOW)
            server._warm_series(self.SHOW)
        self.assertEqual(len(calls), 1)

    def test_the_omdb_daily_budget_stops_requests_without_calling_omdb(self):
        def never(*args, **kwargs):
            raise AssertionError("OMDb must not be called once the day's budget is spent")
        server._omdb_budget.update({"day": time.strftime("%Y-%m-%d"), "used": server.OMDB_DAILY_BUDGET})
        try:
            with mock.patch("urllib.request.urlopen", never):
                reply = server._omdb_get({"t": "x"}, "KEY")
        finally:
            server._omdb_budget.update({"day": "", "used": 0})
        self.assertEqual(reply["Response"], "False")
        self.assertIn("limit", reply["Error"].lower())

    def test_the_page_shows_the_guess_and_asks_again_while_it_is_fetched(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("a.episode_guess", html)
        self.assertIn("'maybe '", html)
        self.assertIn("ar.guess_pending", html)
        self.assertIsNotNone(server._episode_match)               # the shared matcher is importable


class ExactEpisodeUsedWhenRecordingTests(unittest.TestCase):
    """An exact description match names the episode when it is booked, not just on screen."""
    SHOW = "Zzz Exact Sitcom"
    TEXT = "Darrin discovers that bedwarmers can be worse than mothers-in-law."

    def setUp(self):
        with server._rec_lock:
            server._recs.clear()
            server._rec_cancel_events.clear()
        server._init_recordings_table()
        self._clean()
        self.start = int(time.time()) + 7200
        self.stamp = lambda ts: time.strftime("%Y%m%d%H%M%S", time.gmtime(ts))

    def tearDown(self):
        self._clean()

    def _clean(self):
        conn = sqlite3.connect(server._guide_db_path())
        conn.execute("DELETE FROM guide WHERE title=?", (self.SHOW,))
        conn.execute("DELETE FROM recordings WHERE title=?", (self.SHOW,))
        conn.commit(); conn.close()

    def _row(self, channel, desc, season=None, number=None, name="", prog_type="", shift=0):
        conn = sqlite3.connect(server._guide_db_path())
        conn.execute("INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,category,prog_type,episode_title,season_num,episode_num) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (self.SHOW, channel, channel.upper(), self.stamp(self.start + shift), self.stamp(self.start + shift + 1800),
                      desc, "", prog_type, name, season, number))
        conn.commit(); conn.close()

    def _record(self, channel="zz-antenna"):
        body = {"title": self.SHOW, "channel_id": channel, "channel_name": channel.upper(),
                "start_ts": self.start, "stop_ts": self.start + 1800}
        with mock.patch.object(server, "_recording_backend", return_value="agent"), \
                mock.patch.object(server, "_resolve_recording_source",
                                  return_value=("http://x/1.ts", None, {"stream_id": "1", "provider": "primestreams"})):
            return server.app.test_client().post("/epg-web/api/record", json=body)

    def _saved(self):
        conn = sqlite3.connect(server._guide_db_path())
        row = conn.execute("SELECT season_num, episode_num, episode_title, is_series FROM recordings WHERE title=?", (self.SHOW,)).fetchone()
        conn.close()
        return row

    def test_an_unnumbered_airing_is_booked_as_the_episode_its_exact_description_names(self):
        self._row("zz-antenna", self.TEXT)
        self._row("zz-fetv", self.TEXT, 7, 4, "Samantha's Hot Bedwarmer", "EP", shift=86400)
        self.assertTrue(self._record().get_json()["ok"])
        self.assertEqual(self._saved(), (7, 4, "Samantha's Hot Bedwarmer", 1))

    def test_an_airing_with_no_exact_match_stays_unnumbered(self):
        self._row("zz-antenna", self.TEXT)
        self._row("zz-fetv", "A different story altogether about a boat race.", 7, 4, "Other", "EP", shift=86400)
        self.assertTrue(self._record().get_json()["ok"])
        self.assertEqual(self._saved()[:3], (None, None, ""))

    def test_a_numbered_row_that_is_not_an_episode_listing_is_not_trusted(self):
        self._row("zz-antenna", self.TEXT)
        self._row("zz-fetv", self.TEXT, 7, 4, "Samantha's Hot Bedwarmer", "SH", shift=86400)      # a show-level listing
        self.assertTrue(self._record().get_json()["ok"])
        self.assertEqual(self._saved()[:2], (None, None))

    def test_an_airing_the_guide_already_numbers_is_left_alone(self):
        self._row("zz-antenna", self.TEXT, 1, 1, "Given", "EP")
        self._row("zz-fetv", self.TEXT, 7, 4, "Samantha's Hot Bedwarmer", "EP", shift=86400)
        self.assertTrue(self._record().get_json()["ok"])
        self.assertNotEqual(self._saved()[:2], (7, 4))                 # the exact match is only for airings the guide did not number


class SeriesPassUsesExactEpisodesTests(SeriesNoDuplicateBookingTests):
    TEXT_A = "Darrin discovers that bedwarmers can be worse than mothers-in-law."
    TEXT_B = "Samantha's cousin Serena turns up and everything goes wrong for Darrin."

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.guide_db)
        conn.execute("ALTER TABLE guide ADD COLUMN prog_type TEXT")
        conn.commit(); conn.close()

    def _rows(self, rows):
        stamp = lambda ts: time.strftime("%Y%m%d%H%M%S", time.gmtime(ts))
        conn = sqlite3.connect(self.guide_db)
        conn.executemany(
            "INSERT INTO guide(title,channel_id,channel_name,start_utc,end_utc,desc,category,prog_type,episode_title,season_num,episode_num) "
            "VALUES ('Show',?,?,?,?,?,'',?,?,?,?)",
            [(cid, name, stamp(start), stamp(start + 1800), desc, prog, ep, sn, en)
             for cid, name, start, desc, prog, ep, sn, en in rows])
        conn.commit(); conn.close()

    def _booked(self):
        with server._rec_lock:
            return sorted((r["season_num"], r["episode_num"], r["channel_id"]) for r in server._recs.values())

    def test_unnumbered_airings_are_booked_with_the_episode_their_exact_description_names(self):
        self._rows([("hd", "Show HD", self.base - 10 * 86400, self.TEXT_A, "EP", "Bedwarmer", 7, 4),     # an earlier numbered airing
                    ("plain", "Show", self.base, self.TEXT_A, "", "", None, None)])
        self.assertEqual(self._run(), 1)
        self.assertEqual(self._booked(), [(7, 4, "plain")])

    def test_an_unnumbered_row_whose_text_matches_nothing_is_still_booked_unnumbered(self):
        self._rows([("plain", "Show", self.base, self.TEXT_B, "", "", None, None)])
        self.assertEqual(self._run(), 1)
        self.assertEqual(self._booked(), [(None, None, "plain")])

    def test_an_episode_already_in_plex_is_not_booked_again_even_when_the_guide_did_not_number_it(self):
        self._rows([("hd", "Show HD", self.base - 10 * 86400, self.TEXT_A, "EP", "Bedwarmer", 7, 4),
                    ("plain", "Show", self.base, self.TEXT_A, "", "", None, None),
                    ("plain", "Show", self.base + 86400, self.TEXT_B, "", "", None, None)])
        self.plex_keys = {"show|7|4"}
        self.assertEqual(self._run(), 1)
        self.assertEqual(self._booked(), [(None, None, "plain")])                         # only the unidentified one is booked


class FavoriteEmptyRowsTests(unittest.TestCase):
    CHANNELS = [{"id": "c1", "name": "Cinemax"}, {"id": "c2", "name": "Cinemax Action"}, {"id": "c3", "name": "HBO"},
                {"id": "c4", "name": "Brazzers TV Plus"}, {"id": "c5", "name": "Telemundo", "language": "es"}]

    def _rows(self, present=(), ch_filter="", ch_id_filter="", hide_foreign=False, hide_adult=False):
        return [c["id"] for c in server._favorite_rows_without_data(
            self.CHANNELS, set(present), {"c1", "c2", "c3", "c4", "c5"}, ch_filter, ch_id_filter, hide_foreign, hide_adult)]

    def test_with_no_focus_every_starred_channel_without_data_gets_an_empty_row(self):
        self.assertEqual(self._rows(present={"c3"}), ["c1", "c2", "c4", "c5"])

    def test_focusing_one_channel_does_not_bring_back_the_others_as_empty_rows(self):
        self.assertEqual(self._rows(present={"c1"}, ch_id_filter="c1"), [])               # the clicked channel has data
        self.assertEqual(self._rows(present=(), ch_id_filter="c1"), ["c1"])               # it has none: only it is shown empty
        self.assertEqual(self._rows(present=(), ch_id_filter="c2"), ["c2"])

    def test_a_name_search_keeps_only_matching_empty_rows(self):
        self.assertEqual(self._rows(ch_filter="cinemax"), ["c1", "c2"])

    def test_the_hide_switches_still_apply_to_the_empty_rows(self):
        self.assertEqual(self._rows(hide_foreign=True, hide_adult=True), ["c1", "c2", "c3"])


class LocalChannelTests(unittest.TestCase):
    def test_cincinnati_stations_are_local_and_look_alikes_are_not(self):
        for name in ('WCPO', 'WCPO-DT', 'WLWT HD', 'US| ABC 9 CINCINNATI OH (WCPO)', 'WKRC-DT2', 'WXIX', 'WCET', 'WPTO', 'WSTR HD', 'FOX 19 Cincinnati'):
            self.assertTrue(server._is_local_channel(name, server.LOCAL_CALL_SIGNS, 'cincinnati'), name)
        for name in ('ESPN', 'HBO', 'WCPOX Shopping', 'KABC', 'WLWTV Foo', 'Cinemax', ''):
            self.assertFalse(server._is_local_channel(name, server.LOCAL_CALL_SIGNS, 'cincinnati'), name)


class LocalByZipTests(unittest.TestCase):
    def test_a_zip_codes_call_signs_decide_what_is_local(self):
        self.assertTrue(server._is_local_channel('WKYT-DT', ['wkyt', 'wlex']))
        self.assertFalse(server._is_local_channel('WCPO', ['wkyt', 'wlex']))
        self.assertTrue(server._is_local_channel('WLEX HD2', ['wkyt', 'wlex']))
        self.assertFalse(server._is_local_channel('WLEXTRA', ['wlex']))
        self.assertFalse(server._is_local_channel('WCPO', []))

    def test_the_built_in_zip_needs_no_lookup_and_a_bad_zip_gives_none(self):
        self.assertIn('wcpo', server._local_call_signs_for_zip(server.DEFAULT_LOCAL_ZIP))
        self.assertIsNone(server._local_call_signs_for_zip('12'))


class OtaLookupTokenTests(unittest.TestCase):
    def test_an_expired_token_is_dropped_and_the_lookup_retried_once(self):
        import sd_guide
        calls, tokens = [], iter(['old', 'new'])
        def fake_req(method, path, body=None, token=None):
            calls.append(token)
            if token == 'old':
                raise RuntimeError('SD HTTP 403: {"response":"TOKEN_EXPIRED","code":4006}')
            return [{'callsign': 'WCPO'}, {'callsign': 'WLWT'}]
        with mock.patch.object(sd_guide, '_req', fake_req), \
             mock.patch.object(sd_guide, 'get_token', lambda u, p: next(tokens)), \
             mock.patch.object(sd_guide.os, 'remove') as removed:
            self.assertEqual(sd_guide.ota_callsigns('u', 'p', '41042'), ['wcpo', 'wlwt'])
        self.assertEqual(calls, ['old', 'new'])
        removed.assert_called_once()

    def test_other_errors_are_not_retried(self):
        import sd_guide
        with mock.patch.object(sd_guide, '_req', side_effect=RuntimeError('SD HTTP 404: no such lineup')) as req, \
             mock.patch.object(sd_guide, 'get_token', lambda u, p: 't'):
            with self.assertRaises(RuntimeError):
                sd_guide.ota_callsigns('u', 'p', '00000')
        self.assertEqual(req.call_count, 1)


class LocalCallSignFormTests(unittest.TestCase):
    def test_call_signs_with_a_digital_suffix_still_match_plain_channel_names(self):
        for sign in ('WCPO-DT', 'WCPODT', 'WCPO-DT2', 'WCPO', 'wcpo-hd'):
            self.assertEqual(server._base_call_sign(sign), 'wcpo', sign)
        self.assertEqual(server._base_call_sign('WGTV'), 'wgtv')
        self.assertTrue(server._is_local_channel('WCPO', ['WCPO-DT', 'WLWT-DT']))
        self.assertTrue(server._is_local_channel('WLWT HD', ['WCPODT', 'WLWTDT']))
        self.assertFalse(server._is_local_channel('ESPN', ['WCPO-DT']))


class NoFeedMessageTests(unittest.TestCase):
    CHANNELS = [{"id": "a", "name": "WXIX"}, {"id": "b", "name": "FOX (WXIX) Cincinnati OH"},
                {"id": "c", "name": "WXIX-DT"}, {"id": "d", "name": "WCPO"}]

    def _message(self, playable, channel_id="a", name="WXIX"):
        def fake_stream(cid, provider=None):
            return ("<url>", None, {}) if cid in playable else (None, "No stream_id found for channel", {})
        with mock.patch.dict(server._epg, {"channels": self.CHANNELS}), mock.patch.object(server, "_stream_url", fake_stream):
            return server._no_feed_message(channel_id, name)

    def test_it_names_another_listing_of_the_same_station_that_has_a_feed(self):
        msg = self._message({"b"})
        self.assertIn('"FOX (WXIX) Cincinnati OH"', msg)
        self.assertNotIn("WCPO", msg)
        self.assertNotIn("No stream_id", msg)

    def test_it_says_so_when_no_listing_of_the_station_has_a_feed(self):
        msg = self._message(set())
        self.assertIn("no other listing of this station has one", msg)


class LiveStreamFallbackTests(unittest.TestCase):
    def _pick(self, busy_titles, ps_ok=True):
        def fake(cid, provider=None):
            if provider == 'primestreams':
                return (('ps-url', None, {'provider': 'primestreams'}) if ps_ok
                        else (None, 'No stream_id found for channel', {}))
            return 'eagle-url', None, {'provider': 'eaglecast'}
        with mock.patch.object(server, '_stream_url', fake), \
             mock.patch.object(server, '_busy_eaglecast_titles', lambda recs: busy_titles):
            return server._live_stream_url('retroplex.us')

    def test_a_free_eaglecast_connection_is_still_used_first(self):
        self.assertEqual(self._pick([])[0], 'eagle-url')

    def test_a_busy_eaglecast_connection_hands_live_play_to_primestreams(self):
        url, err, dbg = self._pick(['Before I Fall'])
        self.assertEqual((url, err, dbg['provider']), ('ps-url', None, 'primestreams'))

    def test_a_busy_eaglecast_with_no_primestreams_feed_keeps_the_eaglecast_answer(self):
        self.assertEqual(self._pick(['Before I Fall'], ps_ok=False)[0], 'eagle-url')


class DailyRecordingSummaryTests(unittest.TestCase):
    NOW = 1_800_000_000

    def _row(self, hours_ago, status, title='T', reason=''):
        return {'title': title, 'channel': 'Ch', 'start_ts': self.NOW - hours_ago * 3600, 'status': status,
                'failure_reason': reason, 'stream_provider': 'eaglecast'}

    def test_it_counts_the_last_24_hours_and_lists_the_failures(self):
        rows = [self._row(1, 'done'), self._row(5, 'done'), self._row(6, 'failed', 'Bad', 'Video is damaged: 90 decode errors'),
                self._row(10, 'skipped_existing_better'), self._row(30, 'failed', 'Old'), self._row(2, 'cancelled'),
                self._row(-3, 'queued')]            # a booking still in the future
        out = server._daily_recording_summary(rows, self.NOW)
        self.assertEqual((out['done'], out['failed'], out['skipped']), (2, 1, 1))
        self.assertEqual([p['title'] for p in out['problems']], ['Bad'])
        self.assertIn('decode errors', out['problems'][0]['reason'])

    def test_an_address_in_a_failure_reason_is_hidden(self):
        out = server._daily_recording_summary([self._row(1, 'failed', 'X', 'could not open http://host:80/live/u/p/1.ts now')], self.NOW)
        self.assertNotIn('http', out['problems'][0]['reason'])
        self.assertIn('<address>', out['problems'][0]['reason'])

    def test_a_quiet_day_has_no_problems(self):
        out = server._daily_recording_summary([self._row(1, 'done')], self.NOW)
        self.assertEqual((out['failed'], out['problems']), (0, []))


class ChannelReliabilityTests(unittest.TestCase):
    NOW = 1_800_000_000

    def _rows(self, channel_id, done, failed, reason='Video is damaged: 90 decode errors', days_ago=2):
        base = {'channel_id': channel_id, 'channel': channel_id.upper(), 'start_ts': self.NOW - days_ago * 86400}
        return ([dict(base, status='done', failure_reason='') for _ in range(done)] +
                [dict(base, status='failed', failure_reason=reason) for _ in range(failed)])

    def test_levels_follow_the_share_of_failures(self):
        rows = (self._rows('good', 9, 1) + self._rows('warn', 6, 3) + self._rows('bad', 2, 4)
                + self._rows('new', 1, 1))
        out = server._channel_reliability(rows, self.NOW)
        self.assertEqual({k: v['level'] for k, v in out.items()},
                         {'good': 'good', 'warn': 'warn', 'bad': 'bad', 'new': 'unknown'})
        self.assertEqual((out['bad']['done'], out['bad']['failed']), (2, 4))

    def test_the_main_failure_kind_is_named(self):
        rows = self._rows('c', 1, 3, 'Recording ended early: 42 min captured of 58 min scheduled') + \
               self._rows('c', 0, 1, 'Video is damaged: 5 decode errors')
        self.assertEqual(server._channel_reliability(rows, self.NOW)['c']['top_reason'], 'streams that end early')

    def test_old_cancelled_queued_and_channelless_rows_do_not_count(self):
        rows = (self._rows('c', 3, 0, days_ago=40) + self._rows('c', 3, 0)
                + [{'channel_id': 'c', 'channel': 'C', 'start_ts': self.NOW - 3600, 'status': 'cancelled', 'failure_reason': 'x'},
                   {'channel_id': 'c', 'channel': 'C', 'start_ts': self.NOW - 3600, 'status': 'queued', 'failure_reason': ''},
                   {'channel_id': '', 'channel': '?', 'start_ts': self.NOW - 3600, 'status': 'failed', 'failure_reason': 'x'}])
        out = server._channel_reliability(rows, self.NOW)
        self.assertEqual(list(out), ['c'])
        self.assertEqual((out['c']['done'], out['c']['failed']), (3, 0))


class GuideNowTitlesTests(unittest.TestCase):
    def test_it_lists_the_programme_on_now_then_the_next_ones_for_one_channel(self):
        now = 1_800_000_000
        progs = [{'channel_id': 'a', 'title': 'Past', 'start_ts': now - 7200, 'stop_ts': now - 3600},
                 {'channel_id': 'a', 'title': 'Now', 'start_ts': now - 600, 'stop_ts': now + 3000},
                 {'channel_id': 'a', 'title': 'Next', 'start_ts': now + 3000, 'stop_ts': now + 6600},
                 {'channel_id': 'b', 'title': 'Other', 'start_ts': now - 600, 'stop_ts': now + 3000}]
        with mock.patch.dict(server._epg, {'programmes': progs}):
            titles = [row['title'] for row in server._guide_now_titles('a', now)]
        self.assertEqual(titles, ['Now', 'Next'])


class ChannelReportTests(unittest.TestCase):
    NOW = 1_800_000_000

    def _row(self, cid, name, status, days_ago=1, reason='', decision='', provider='primestreams', title='T'):
        return {'channel_id': cid, 'channel': name, 'title': title, 'start_ts': self.NOW - days_ago * 86400,
                'status': status, 'failure_reason': reason, 'quality_decision': decision, 'stream_provider': provider}

    def test_it_lists_only_channels_with_failures_worst_first_with_reasons_and_the_last_failure(self):
        rows = ([self._row('a', 'Sky A', 'done')] * 2
                + [self._row('a', 'Sky A', 'failed', 3, 'Video is damaged: 90 decode errors', title='Old'),
                   self._row('a', 'Sky A', 'failed', 1, 'Recording started late: 120 s after the scheduled start', title='New')]
                + [self._row('b', 'Good B', 'done')] * 5
                + [self._row('c', 'Never C', 'done')] * 3
                + [self._row('b', 'Good B', 'failed', 2, 'Recording ended early: 9 min missing')])
        out = server._channel_report(rows, self.NOW)
        self.assertEqual([c['channel'] for c in out], ['Sky A', 'Good B'])
        self.assertEqual((out[0]['done'], out[0]['failed'], out[0]['failure_pct']), (2, 2, 50))
        self.assertEqual(out[0]['reasons'], {'damaged video': 1, 'late starts': 1})
        self.assertEqual(out[0]['last_failure']['title'], 'New')

    def test_minor_blips_are_counted_and_the_text_filter_narrows_the_report(self):
        rows = [self._row('a', 'Sky A', 'done', decision='recording because Plex has no copy; 4 minor video errors'),
                self._row('a', 'Sky A', 'failed', 1, 'Video is damaged: 90 decode errors'),
                self._row('b', 'Starz B', 'failed', 1, 'Video is damaged: 90 decode errors')]
        out = server._channel_report(rows, self.NOW, text='sky')
        self.assertEqual([(c['channel'], c['minor']) for c in out], [('Sky A', 1)])

    def test_failures_outside_the_period_or_still_running_do_not_count(self):
        rows = [self._row('a', 'A', 'failed', 40, 'Video is damaged: 90 decode errors'), self._row('a', 'A', 'recording')]
        self.assertEqual(server._channel_report(rows, self.NOW, days=30), [])


class DailySummaryMinorTests(unittest.TestCase):
    def test_finished_recordings_with_a_few_video_errors_are_counted_as_minor(self):
        now = 1_800_000_000
        rows = [{'title': 'A', 'channel': 'C', 'start_ts': now - 3600, 'status': 'done', 'failure_reason': '',
                 'stream_provider': 'primestreams', 'quality_decision': 'recording because Plex has no copy; 4 minor video errors'},
                {'title': 'B', 'channel': 'C', 'start_ts': now - 3600, 'status': 'done', 'failure_reason': '',
                 'stream_provider': 'primestreams', 'quality_decision': 'recording because Plex has no copy'}]
        out = server._daily_recording_summary(rows, now)
        self.assertEqual((out['done'], out['minor'], out['failed']), (2, 1, 0))


class ChannelRecordingsTests(unittest.TestCase):
    def test_every_past_recording_is_listed_newest_first_without_cancelled_ones(self):
        base = 1_800_000_000
        rows = [{'title': 'Old', 'start_ts': base, 'status': 'done', 'failure_reason': '',
                 'quality_decision': 'recording because Plex has no copy; 3 minor video errors', 'stream_provider': 'eaglecast'},
                {'title': 'New', 'start_ts': base + 86400, 'status': 'failed',
                 'failure_reason': 'Video is damaged: 90 decode errors; opened http://h:80/live/u/p/1.ts', 'quality_decision': '',
                 'stream_provider': 'primestreams'},
                {'title': 'Gone', 'start_ts': base + 100, 'status': 'cancelled', 'failure_reason': 'Cancelled by user',
                 'quality_decision': '', 'stream_provider': 'primestreams'},
                {'title': 'Skip', 'start_ts': base + 50, 'status': 'skipped_existing_better', 'failure_reason': '',
                 'quality_decision': 'Plex is better', 'stream_provider': 'primestreams'}]
        out = server._channel_recordings(rows)
        self.assertEqual([r['title'] for r in out], ['New', 'Skip', 'Old'])
        self.assertEqual((out[0]['severity'], out[0]['result']), ('critical', 'failed'))
        self.assertNotIn('http', out[0]['detail'])
        self.assertEqual(out[2]['severity'], 'minor')
        self.assertEqual(out[1]['severity'], '')


class ChannelResetTests(unittest.TestCase):
    NOW = 1_800_000_000

    def _row(self, status, days_ago, reason='', cid='a', name='Chan A'):
        return {'channel_id': cid, 'channel': name, 'title': 'T', 'start_ts': self.NOW - days_ago * 86400,
                'status': status, 'failure_reason': reason, 'quality_decision': '', 'stream_provider': 'eaglecast'}

    def test_a_reset_stops_older_failures_counting_and_shows_the_latest_result(self):
        rows = [self._row('failed', 20, 'Recording ended early: 9 min missing'), self._row('failed', 10, 'Video is damaged: 90 decode errors'),
                self._row('done', 3), self._row('done', 1)]
        resets = {'a': self.NOW - 5 * 86400}
        self.assertEqual(server._channel_report(rows, self.NOW, resets=resets), [])        # nothing left to complain about
        out = server._channel_report(rows, self.NOW, resets=resets, include_reset=True)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]['failed'], out[0]['done'], out[0]['before_reset']), (0, 2, 2))
        self.assertEqual((out[0]['last_result'], out[0]['reset_at'] != ''), ('recorded', True))

    def test_failures_after_the_reset_still_count_and_without_a_reset_the_latest_result_is_shown(self):
        rows = [self._row('done', 8), self._row('failed', 2, 'Video is damaged: 90 decode errors')]
        out = server._channel_report(rows, self.NOW, resets={'a': self.NOW - 5 * 86400})
        self.assertEqual((out[0]['failed'], out[0]['done']), (1, 0))
        plain = server._channel_report(rows, self.NOW)
        self.assertEqual((plain[0]['last_result'], plain[0]['last_success']), ('failed', server._local_dt(self.NOW - 8 * 86400).strftime('%Y-%m-%d')))

    def test_the_reliability_warning_ignores_recordings_before_a_reset(self):
        rows = [self._row('failed', 20, 'Video is damaged: 9 decode errors')] * 4 + [self._row('done', 1)] * 3
        before = server._channel_reliability(rows, self.NOW)['a']
        after = server._channel_reliability(rows, self.NOW, resets={'a': self.NOW - 5 * 86400})['a']
        self.assertEqual(before['level'], 'bad')
        self.assertEqual((after['level'], after['failed']), ('good', 0))

    def test_old_recordings_are_marked_before_reset_in_the_channel_list(self):
        rows = [{'title': 'Old', 'start_ts': self.NOW - 86400 * 9, 'status': 'failed', 'failure_reason': 'x', 'quality_decision': '', 'stream_provider': 'p'},
                {'title': 'New', 'start_ts': self.NOW - 86400, 'status': 'done', 'failure_reason': '', 'quality_decision': '', 'stream_provider': 'p'}]
        out = server._channel_recordings(rows, reset_ts=self.NOW - 5 * 86400)
        self.assertEqual([(r['title'], r['before_reset']) for r in out], [('New', False), ('Old', True)])


class ChannelResetStorageTests(unittest.TestCase):
    def test_a_reset_is_saved_read_back_and_cleared(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(server, '_guide_db_path', lambda: temp + '/g.db'):
            server._set_channel_reset('c1', 123.0)
            self.assertEqual(server._channel_resets(), {'c1': 123.0})
            server._set_channel_reset('c1', 456.0)
            self.assertEqual(server._channel_resets(), {'c1': 456.0})
            server._set_channel_reset('c1', None)
            self.assertEqual(server._channel_resets(), {})


class ResetAllChannelsTests(unittest.TestCase):
    def test_reset_all_covers_every_channel_with_a_recording_and_can_be_undone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(server, '_guide_db_path', lambda: temp + '/g.db'):
            conn = sqlite3.connect(temp + '/g.db')
            conn.execute('CREATE TABLE recordings (rec_id TEXT, channel_id TEXT)')
            conn.executemany('INSERT INTO recordings VALUES(?,?)', [('1', 'a'), ('2', 'a'), ('3', 'b'), ('4', ''), ('5', None)])
            conn.commit(); conn.close()
            self.assertEqual(server._reset_all_channels(1000.0), 2)
            self.assertEqual(server._channel_resets(), {'a': 1000.0, 'b': 1000.0})
            self.assertEqual(server._reset_all_channels(None), 2)
            self.assertEqual(server._channel_resets(), {})


class SeriesFailureGuardTests(unittest.TestCase):
    NOW = None

    def _db(self, temp, rows):
        conn = sqlite3.connect(temp + '/g.db')
        conn.execute('CREATE TABLE recordings (title TEXT, channel_id TEXT, start_ts REAL, status TEXT, '
                     'failure_reason TEXT, season_num INTEGER, episode_num INTEGER)')
        conn.executemany('INSERT INTO recordings VALUES(?,?,?,?,?,?,?)', rows)
        conn.commit(); conn.close()

    def test_one_failed_episode_does_not_block_the_rest_of_the_series_on_that_channel(self):
        import tempfile
        now = time.time()
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(server, '_guide_db_path', lambda: temp + '/g.db'):
            self._db(temp, [('The Sopranos', 'hbo', now - 3600, 'failed', 'ended early', 4, 9)])
            guard = server._recent_same_source_failure
            self.assertTrue(guard('The Sopranos', 'hbo', season=4, episode=9))        # the same episode: still blocked
            self.assertFalse(guard('The Sopranos', 'hbo', season=5, episode=7))       # another episode: free to book
            self.assertTrue(guard('The Sopranos', 'hbo'))                              # unnumbered or a film: title level
            self.assertFalse(guard('The Sopranos', 'other', season=4, episode=9))      # another channel

    def test_a_reset_on_the_channel_lifts_the_block(self):
        import tempfile
        now = time.time()
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(server, '_guide_db_path', lambda: temp + '/g.db'):
            self._db(temp, [('The Sopranos', 'hbo', now - 3600, 'failed', 'ended early', 4, 9)])
            self.assertTrue(server._recent_same_source_failure('The Sopranos', 'hbo', season=4, episode=9))
            server._set_channel_reset('hbo', now - 60)
            self.assertFalse(server._recent_same_source_failure('The Sopranos', 'hbo', season=4, episode=9))
            self.assertFalse(server._recent_same_source_failure('The Sopranos', 'hbo'))

    def test_a_reset_also_clears_a_suspect_channel_for_automatic_booking(self):
        import tempfile
        now = time.time()
        rows = [('Show %d' % i, 'bad', now - 7200 + i, 'failed', 'Video is damaged: 90 decode errors', None, None) for i in range(3)]
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(server, '_guide_db_path', lambda: temp + '/g.db'):
            self._db(temp, rows)
            self.assertEqual(server._channel_recording_reliability()['bad']['level'], 'suspect')
            server._set_channel_reset('bad', now - 60)
            self.assertNotIn('bad', server._channel_recording_reliability())


class SeriesWhyTests(SeriesNoDuplicateBookingTests):
    """The dry run names the reason each airing is or is not booked, and books nothing."""

    def _why(self, recordable=("hd",), failure=False, plex=None, stream_error=None):
        explain = []
        with mock.patch.object(server, "_guide_db_path", return_value=self.guide_db), \
                mock.patch.object(server, "load_config", return_value={}), \
                mock.patch.object(server, "get_recordable_channel_ids", return_value=set(recordable)), \
                mock.patch.object(server, "_plex_episode_keys", side_effect=lambda *a, **k: set(plex or ())), \
                mock.patch.object(server, "_plex_tv_path", return_value=self.temp.name), \
                mock.patch.object(server, "_recording_backend", return_value="agent"), \
                mock.patch.object(server, "_db_upsert_rec", return_value=True) as upsert, \
                mock.patch.object(server, "_recent_same_source_failure", return_value=failure), \
                mock.patch.object(server, "_resolve_recording_source",
                                  return_value=(None if stream_error else "u", stream_error, {"stream_id": "1", "provider": "primestreams"})):
            count = server._schedule_series_airings("Show", self.guide_db, "movies.db", explain=explain, dry_run=True)
        return explain, count, upsert

    def test_a_normal_airing_would_be_booked_and_nothing_is_booked(self):
        self._guide([("hd", "Show HD", self.base, "Pilot", 1, 1)])
        explain, count, upsert = self._why()
        self.assertEqual([e["why"] for e in explain], ["would be booked"])
        self.assertEqual((count, self._starts()), (0, []))
        upsert.assert_not_called()

    def test_each_reason_is_named(self):
        self._guide([("hd", "Show HD", self.base, "Pilot", 1, 1), ("hd", "Show HD", self.base + 86400, "Two", 1, 2),
                     ("gone", "No Stream Channel", self.base, "Pilot", 1, 1)])
        why = lambda **kw: " | ".join(e["why"] for e in self._why(**kw)[0])
        self.assertIn("no recording stream is set up", why())                                   # channel 'gone' is not recordable
        self.assertIn("this episode failed on this channel", why(failure=True))
        self.assertIn("already in Plex", why(plex={"show|1|2"}))
        self.assertIn("no usable stream: boom", why(stream_error="boom"))
        self.assertEqual([e["episode"] for e in self._why()[0]].count("S1E1"), 2)             # both S1E1 rows are explained


class SeriesCommercialFreeFirstTests(SeriesNoDuplicateBookingTests):
    """A series is recorded from any channel that carries the episode; a commercial-free one wins."""

    def _book(self, channels):
        with mock.patch.object(server, "_guide_db_path", return_value=self.guide_db), \
                mock.patch.object(server, "load_config", return_value={}), \
                mock.patch.object(server, "get_recordable_channel_ids", return_value=set(channels)), \
                mock.patch.object(server, "_plex_episode_keys", return_value=set()), \
                mock.patch.object(server, "_plex_tv_path", return_value=self.temp.name), \
                mock.patch.object(server, "_recording_backend", return_value="agent"), \
                mock.patch.object(server, "_db_upsert_rec", return_value=True), \
                mock.patch.object(server, "_recent_same_source_failure", return_value=False), \
                mock.patch.object(server, "_resolve_recording_source",
                                  return_value=("u", None, {"stream_id": "1", "provider": "primestreams"})):
            server._schedule_series_airings("Show", self.guide_db, "movies.db")
        with server._rec_lock:
            return sorted((r["channel_id"], r["season_num"], r["episode_num"]) for r in server._recs.values())

    def test_a_commercial_free_airing_beats_an_ad_supported_hd_one_even_when_it_is_later(self):
        self._guide([("adhd", "Antenna TV HD", self.base, "Pilot", 1, 1), ("hbo", "HBO Drama", self.base + 7200, "Pilot", 1, 1)])
        self.assertEqual(self._book({"adhd", "hbo"}), [("hbo", 1, 1)])

    def test_an_episode_only_on_an_ad_supported_channel_is_still_recorded(self):
        self._guide([("adhd", "Antenna TV HD", self.base, "Pilot", 1, 1), ("hbo", "HBO Drama", self.base + 7200, "Two", 1, 2)])
        self.assertEqual(self._book({"adhd", "hbo"}), [("adhd", 1, 1), ("hbo", 1, 2)])

    def test_picture_quality_still_breaks_a_tie_between_ad_supported_channels(self):
        self._guide([("plain", "Antenna TV", self.base, "Pilot", 1, 1), ("adhd", "Antenna TV HD", self.base + 7200, "Pilot", 1, 1)])
        self.assertEqual(self._book({"plain", "adhd"}), [("adhd", 1, 1)])

    def test_the_page_main_series_button_records_all_episodes_and_weekly_is_for_sports(self):
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("function recordMainSeriesButton()", html)
        self.assertIn("weeklyBtn.textContent = sportsEvent ? '📅 Record weekly' : '📺 Record series';", html)
        self.assertIn("recordSeries('pm-weekly-btn')", html)


class SeriesReplaceAdCopyTests(SeriesCommercialFreeFirstTests):
    """An episode in Plex that was recorded with commercials is recorded again from a commercial-free airing."""

    def _recorded(self, channel, season=1, episode=1, status="done"):
        conn = sqlite3.connect(self.guide_db)
        conn.execute("INSERT INTO recordings(rec_id,title,channel,channel_id,start_ts,stop_ts,status,season_num,episode_num) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"r{channel}{season}{episode}", "Show", channel, "x", 1.0, 2.0, status, season, episode))
        conn.commit(); conn.close()

    def _book_in_plex(self, channels):
        with mock.patch.object(server, "_plex_episode_keys", return_value={"show|1|1"}):
            return self._book_with_plex(channels)

    def _book_with_plex(self, channels):
        with mock.patch.object(server, "_guide_db_path", return_value=self.guide_db), \
                mock.patch.object(server, "load_config", return_value={}), \
                mock.patch.object(server, "get_recordable_channel_ids", return_value=set(channels)), \
                mock.patch.object(server, "_plex_episode_keys", return_value={"show|1|1"}), \
                mock.patch.object(server, "_plex_tv_path", return_value=self.temp.name), \
                mock.patch.object(server, "_recording_backend", return_value="agent"), \
                mock.patch.object(server, "_db_upsert_rec", return_value=True), \
                mock.patch.object(server, "_recent_same_source_failure", return_value=False), \
                mock.patch.object(server, "_resolve_recording_source",
                                  return_value=("u", None, {"stream_id": "1", "provider": "primestreams"})):
            server._schedule_series_airings("Show", self.guide_db, "movies.db")
        with server._rec_lock:
            return sorted((r["channel_id"], r["season_num"], r["episode_num"], bool(r.get("auto_upgrade")))
                          for r in server._recs.values())

    def test_a_commercial_free_airing_replaces_a_copy_recorded_with_commercials(self):
        self._recorded("Antenna TV HD")
        self._guide([("hbo", "HBO Drama", self.base, "Pilot", 1, 1)])
        self.assertEqual(self._book_with_plex({"hbo"}), [("hbo", 1, 1, True)])

    def test_nothing_is_booked_when_the_copy_is_already_commercial_free_or_of_unknown_origin(self):
        self._guide([("hbo", "HBO Drama", self.base, "Pilot", 1, 1)])
        self.assertEqual(self._book_with_plex({"hbo"}), [])                     # no recording of ours: unknown, leave it
        self._recorded("HBO Drama")
        self.assertEqual(self._book_with_plex({"hbo"}), [])                     # already commercial-free

    def test_an_ad_supported_airing_never_replaces_an_ad_supported_copy(self):
        self._recorded("Antenna TV HD")
        self._guide([("adhd", "Antenna TV HD", self.base, "Pilot", 1, 1)])
        self.assertEqual(self._book_with_plex({"adhd"}), [])

    def test_only_a_few_replacements_are_booked_at_once(self):
        for episode in range(1, 6):
            self._recorded("Antenna TV HD", 1, episode)
        self._guide([("hbo", "HBO Drama", self.base + n * 3600, "Ep", 1, n) for n in range(1, 6)])
        with mock.patch.object(server, "_plex_episode_keys", return_value={f"show|1|{n}" for n in range(1, 6)}):
            with mock.patch.object(server, "_guide_db_path", return_value=self.guide_db), \
                    mock.patch.object(server, "load_config", return_value={}), \
                    mock.patch.object(server, "get_recordable_channel_ids", return_value={"hbo"}), \
                    mock.patch.object(server, "_plex_tv_path", return_value=self.temp.name), \
                    mock.patch.object(server, "_recording_backend", return_value="agent"), \
                    mock.patch.object(server, "_db_upsert_rec", return_value=True), \
                    mock.patch.object(server, "_recent_same_source_failure", return_value=False), \
                    mock.patch.object(server, "_resolve_recording_source",
                                      return_value=("u", None, {"stream_id": "1", "provider": "primestreams"})):
                booked = server._schedule_series_airings("Show", self.guide_db, "movies.db")
        self.assertEqual(booked, server.MAX_SERIES_UPGRADES_AT_ONCE)


class SyncStreamsLoginRefusedTests(unittest.TestCase):
    def test_a_panel_that_answers_with_an_object_instead_of_a_channel_list_gets_a_plain_message(self):
        import io
        from urllib import request as urlreq

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        deadline = time.time() + 10          # the startup guide load must not see this test's patched urlopen
        while time.time() < deadline and (server._startup_status.get("loading") or server._startup_status.get("background_loading")):
            time.sleep(0.05)
        cfg = {"epg_url": "http://panel.invalid:80", "epg_user": "u", "epg_pass": "p", "db_path": "x.db"}
        reply = Response(json.dumps({"user_info": {"auth": 0}}).encode())
        with mock.patch.object(server, "load_config", return_value=cfg), \
             mock.patch.object(urlreq, "urlopen", return_value=reply):
            response = server.app.test_client().post("/epg-web/api/sync-streams")
        self.assertEqual(response.status_code, 502)
        message = response.get_json()["error"]
        self.assertIn("did not accept the saved login", message)
        self.assertIn("auth=0", message)
        self.assertNotIn("panel.invalid", message)


class PrimeStreamsSwitchTests(unittest.TestCase):
    def test_switched_off_blocks_primestreams_but_not_eaglecast(self):
        cfg = {"primestreams_enabled": False}
        with mock.patch.object(server, "load_config", return_value=cfg), \
             mock.patch.object(server, "_stream_url_unchecked",
                               return_value=("http://x/1.ts", None, {"provider": "primestreams"})):
            url, error, _ = server._stream_url("c1")
            self.assertIsNone(url)
            self.assertIn("switched off", error)
            self.assertIn("switched off", server._stream_url("c1", "primestreams")[1])
        with mock.patch.object(server, "load_config", return_value=cfg), \
             mock.patch.object(server, "_stream_url_unchecked",
                               return_value=("http://x/1.ts", None, {"provider": "eaglecast"})):
            self.assertIsNotNone(server._stream_url("c1", "eaglecast")[0])

    def test_on_by_default(self):
        with mock.patch.object(server, "load_config", return_value={}), \
             mock.patch.object(server, "_stream_url_unchecked",
                               return_value=("http://x/1.ts", None, {"provider": "primestreams"})):
            self.assertIsNotNone(server._stream_url("c1")[0])

    def test_move_to_eaglecast_moves_one_of_two_overlapping_and_lists_the_rest(self):
        import sqlite3 as sq, tempfile
        db = os.path.join(tempfile.mkdtemp(), "g.db")
        conn = sq.connect(db)
        conn.execute("CREATE TABLE recordings (rec_id, title, channel, channel_id, start_ts, stop_ts, status, "
                     "stream_provider, stream_id, stream_extension)")
        t = time.time() + 86400
        for rid, ch, a, b in (("a", "c1", t, t + 3600), ("b", "c2", t + 600, t + 4200), ("c", "c3", t, t + 3600)):
            conn.execute("INSERT INTO recordings VALUES (?,?,?,?,?,?,'queued','primestreams','9','ts')",
                         (rid, rid, ch, ch, a, b))
        conn.commit()
        conn.close()
        carried = {"c1": ("11", "n", "ts"), "c2": ("12", "n", "ts")}
        with mock.patch.object(server, "_guide_db_path", return_value=db), \
             mock.patch.object(server, "_eaglecast_enabled", return_value=True), \
             mock.patch.object(server, "_eaglecast_expiry", return_value=(None, "")), \
             mock.patch.object(server, "_is_quarantined_eaglecast_stream", return_value=False), \
             mock.patch.object(server, "_eaglecast_stream_for_channel", side_effect=lambda c: carried.get(c)):
            report = server._move_primestreams_bookings(False)
            self.assertEqual([e["id"] for e in report["can_move"]], ["a"])
            self.assertEqual({e["id"] for e in report["cannot_move"]}, {"b", "c"})
            self.assertEqual(report["moved"], 0)
            done = server._move_primestreams_bookings(True)
        self.assertEqual(done["moved"], 1)
        row = sq.connect(db).execute("SELECT stream_provider, stream_id FROM recordings WHERE rec_id='a'").fetchone()
        self.assertEqual(row, ("eaglecast", "11"))


class PrimeStreamsHealthTests(unittest.TestCase):
    CFG = {"epg_url": "http://panel.invalid:826", "epg_user": "someone", "epg_pass": "secret"}

    def _check(self, payload=None, error=None, cfg=None):
        import io
        from urllib import request as urlreq

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        patch = mock.patch.object(urlreq, "urlopen", side_effect=error) if error else \
            mock.patch.object(urlreq, "urlopen", return_value=Response(json.dumps(payload).encode()))
        with mock.patch.object(server, "load_config", return_value=self.CFG if cfg is None else cfg), patch:
            return server._check_primestreams()

    def test_working_account_is_ok(self):
        self.assertEqual(self._check({"user_info": {"auth": 1, "status": "Active"}})["state"], "ok")

    def test_known_login_but_account_not_active(self):
        result = self._check({"user_info": {"auth": 1, "status": "Expired"}})
        self.assertEqual(result["state"], "inactive")
        self.assertIn("not active", result["message"])

    def test_login_not_accepted(self):
        self.assertEqual(self._check({"user_info": {"auth": 0}})["state"], "refused")
        self.assertEqual(self._check({"something": "else"})["state"], "refused")

    def test_no_answer_is_unreachable_and_leaks_nothing(self):
        result = self._check(error=OSError("could not reach panel.invalid with secret"))
        self.assertEqual(result["state"], "unreachable")
        self.assertEqual(result["detail"], "OSError")
        for text in ("panel.invalid", "someone", "secret"):
            self.assertNotIn(text, json.dumps(result))

    def test_an_http_401_from_the_panel_means_the_login_was_refused(self):
        from urllib import error as urlerr
        result = self._check(error=urlerr.HTTPError("http://panel.invalid/x", 401, "Unauthorized", {}, None))
        self.assertEqual(result["state"], "refused")
        self.assertNotIn("panel.invalid", json.dumps(result))

    def test_nothing_saved(self):
        self.assertEqual(self._check({}, cfg={})["state"], "not_set_up")

    def test_the_answer_is_reused_for_a_few_minutes_and_fresh_repeats_it(self):
        server._ps_health.update({"at": 0.0, "result": None})
        with mock.patch.object(server, "_check_primestreams",
                               return_value={"state": "ok", "status": "Active", "message": "x"}) as check:
            server._primestreams_health()
            server._primestreams_health()
            self.assertEqual(check.call_count, 1)
            server._primestreams_health(0)
            self.assertEqual(check.call_count, 2)
        server._ps_health.update({"at": 0.0, "result": None})

    def test_the_status_endpoint_reports_health_and_the_page_has_the_bar(self):
        server._ps_health.update({"at": time.time(), "result": {"state": "inactive", "status": "", "message": "m"}})
        data = server.app.test_client().get("/epg-web/api/primestreams").get_json()
        self.assertEqual(data["health"]["state"], "inactive")
        self.assertIn("enabled", data)
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn('id="ps-bar"', html)
        server._ps_health.update({"at": 0.0, "result": None})


class PrimeStreamsBookingRulesTests(unittest.TestCase):
    def tearDown(self):
        server._ps_health.update({"at": 0.0, "result": None})

    def _health(self, state, **extra):
        server._ps_health.update({"at": time.time(), "result": dict({"state": state, "status": "", "message": "m"}, **extra)})

    def test_an_offline_account_is_not_booked(self):
        for state in ("inactive", "refused"):
            self._health(state)
            url, error, _ = server._primestreams_source("c1", 1000.0, 2000.0)
            self.assertIsNone(url)
            self.assertIn("not working right now", error)

    def test_no_answer_from_the_nas_does_not_block_a_booking(self):
        self._health("unreachable")
        with mock.patch.object(server, "_primestreams_conflict", return_value=None), \
             mock.patch.object(server, "_stream_url", return_value=("http://x/1.ts", None, {})):
            self.assertIsNone(server._primestreams_source("c1", 1000.0, 2000.0)[1])

    def test_a_working_account_is_booked_unless_it_is_already_full(self):
        self._health("ok", max_connections=1)
        with mock.patch.object(server, "_primestreams_conflict", return_value=None), \
             mock.patch.object(server, "_stream_url", return_value=("http://x/1.ts", None, {})):
            self.assertEqual(server._primestreams_source("c1", 1000.0, 2000.0)[1], None)
        with mock.patch.object(server, "_primestreams_conflict", return_value=("Other Film", "ch", 1.0, 2.0)):
            url, error, _ = server._primestreams_source("c1", 1000.0, 2000.0)
        self.assertIsNone(url)
        self.assertIn('overlapping "Other Film"', error)
        self.assertEqual(server._play_blocked_note(error), 'PrimeStreams has one connection and it is busy with "Other Film"')

    def test_overlap_counts_only_primestreams_bookings_up_to_the_limit(self):
        import sqlite3 as sq, tempfile
        db = os.path.join(tempfile.mkdtemp(), "g.db")
        conn = sq.connect(db)
        conn.execute("CREATE TABLE recordings (rec_id, title, channel, start_ts, stop_ts, status, stream_provider)")
        conn.execute("INSERT INTO recordings VALUES ('a','Held','c',1000,3000,'queued','primestreams')")
        conn.execute("INSERT INTO recordings VALUES ('b','Eagle','c',1000,3000,'queued','eaglecast')")
        conn.commit()
        conn.close()
        with mock.patch.object(server, "_guide_db_path", return_value=db):
            self._health("ok", max_connections=1)
            self.assertEqual(server._primestreams_conflict(2000.0, 4000.0)[0], "Held")
            self.assertIsNone(server._primestreams_conflict(3000.0, 4000.0))
            self.assertIsNone(server._primestreams_conflict(2000.0, 4000.0, exclude_rec_id="a"))
            self._health("ok", max_connections=2)
            self.assertIsNone(server._primestreams_conflict(2000.0, 4000.0))


class RefreshBookingStreamsTests(unittest.TestCase):
    def _db(self):
        import sqlite3 as sq, tempfile
        db = os.path.join(tempfile.mkdtemp(), "g.db")
        conn = sq.connect(db)
        conn.execute("CREATE TABLE recordings (rec_id, title, channel, channel_id, stream_id, stream_extension, "
                     "stop_ts, status, stream_provider)")
        t = time.time() + 3600
        for rid, ch, sid, prov in (("a", "c1", "100", "primestreams"), ("b", "c2", "200", "primestreams"),
                                    ("c", "c3", "300", "primestreams"), ("d", "c4", "400", "eaglecast")):
            conn.execute("INSERT INTO recordings VALUES (?,?,?,?,?, 'ts', ?, 'queued', ?)", (rid, rid, ch, ch, sid, t, prov))
        conn.commit()
        conn.close()
        return db

    def test_stale_numbers_are_replaced_and_the_rest_left_alone(self):
        import sqlite3 as sq
        db = self._db()
        current = {"c1": ("http://x", None, {"stream_id": "111", "stream_extension": "ts"}),
                   "c2": ("http://x", None, {"stream_id": "200", "stream_extension": "ts"}),
                   "c3": (None, "no channel", {})}
        with mock.patch.object(server, "_guide_db_path", return_value=db), \
             mock.patch.object(server, "_stream_url", side_effect=lambda cid, prov=None: current[cid]):
            report = server._refresh_queued_stream_ids(False)
            self.assertEqual([e["id"] for e in report["would_change"]], ["a"])
            self.assertEqual([e["id"] for e in report["unresolved"]], ["c"])
            self.assertEqual(report["changed"], 0)
            done = server._refresh_queued_stream_ids(True)
        self.assertEqual(done["changed"], 1)
        rows = dict(sq.connect(db).execute("SELECT rec_id, stream_id FROM recordings"))
        self.assertEqual(rows, {"a": "111", "b": "200", "c": "300", "d": "400"})


class FilmFirstSlotsTests(unittest.TestCase):
    CARRIED = {"eaglecast": {"c1", "c2", "c3"}, "primestreams": {"c1", "c2", "c3"}}

    def _db(self, rows):
        import sqlite3 as sq, tempfile
        db = os.path.join(tempfile.mkdtemp(), "g.db")
        conn = sq.connect(db)
        conn.execute("CREATE TABLE recordings (rec_id, title, channel, channel_id, start_ts, stop_ts, status, "
                     "stream_provider, stream_id, stream_extension, season_num, episode_num, is_series, failure_reason)")
        for r in rows:
            conn.execute("INSERT INTO recordings VALUES (?,?,?,?,?,?,'queued',?,'9','ts',?,?,?,NULL)", r)
        conn.commit()
        conn.close()
        return db

    def _run(self, db, carried=None, apply_it=True, primestreams_slots=1):
        carried = self.CARRIED if carried is None else carried
        source = lambda provider, cid, stop, expiry: ("77", "ts") if cid in carried[provider] else None
        with mock.patch.object(server, "_guide_db_path", return_value=db), \
             mock.patch.object(server, "_eaglecast_enabled", return_value=True), \
             mock.patch.object(server, "_primestreams_enabled", return_value=True), \
             mock.patch.object(server, "_eaglecast_expiry", return_value=(None, "")), \
             mock.patch.object(server, "_primestreams_health", return_value={"state": "ok"}), \
             mock.patch.object(server, "_primestreams_max_connections", return_value=primestreams_slots), \
             mock.patch.object(server, "_slot_source", side_effect=source):
            return server._rebalance_slots(apply_it)

    def _state(self, db):
        import sqlite3 as sq
        return {r[0]: (r[1], r[2]) for r in sq.connect(db).execute("SELECT rec_id, stream_provider, status FROM recordings")}

    def test_a_film_takes_eaglecast_and_the_episode_moves_to_primestreams(self):
        t = time.time() + 86400
        db = self._db([("ep", "A Rerun", "c1", "c1", t, t + 3600, "eaglecast", 2, 5, 1),
                       ("film", "A Film", "c2", "c2", t + 600, t + 4200, "primestreams", None, None, 0)])
        result = self._run(db)
        self.assertEqual({m["id"]: m["to"] for m in result["moves"]}, {"film": "eaglecast", "ep": "primestreams"})
        self.assertEqual(self._state(db), {"ep": ("primestreams", "queued"), "film": ("eaglecast", "queued")})

    def test_an_episode_that_only_the_film_s_connection_could_carry_is_not_recorded(self):
        t = time.time() + 86400
        carried = {"eaglecast": {"c1", "c2"}, "primestreams": set()}
        db = self._db([("ep", "A Rerun", "c1", "c1", t, t + 3600, "eaglecast", 2, 5, 1),
                       ("film", "A Film", "c2", "c2", t + 600, t + 4200, "eaglecast", None, None, 0)])
        result = self._run(db, carried=carried)
        self.assertEqual([e["id"] for e in result["not_recorded"]], ["ep"])
        self.assertEqual(self._state(db)["ep"][1], "failed")
        self.assertEqual(self._state(db)["film"], ("eaglecast", "queued"))

    def test_report_only_changes_nothing_and_non_overlapping_bookings_are_left_alone(self):
        t = time.time() + 86400
        db = self._db([("ep", "A Rerun", "c1", "c1", t, t + 3600, "eaglecast", 2, 5, 1),
                       ("film", "A Film", "c2", "c2", t + 600, t + 4200, "primestreams", None, None, 0),
                       ("late", "Later", "c3", "c3", t + 9000, t + 12000, "primestreams", 2, 6, 1)])
        before = self._state(db)
        result = self._run(db, apply_it=False)
        self.assertTrue(result["moves"])
        self.assertEqual(self._state(db), before)
        self.assertNotIn("late", [m["id"] for m in result["moves"]] + [e["id"] for e in result["not_recorded"]])

    def test_two_episodes_that_clash_are_left_alone_and_a_channel_nobody_carries_is_never_dropped(self):
        t = time.time() + 86400
        carried = {"eaglecast": {"c1"}, "primestreams": {"c1"}}
        db = self._db([("e1", "Show", "c1", "c1", t, t + 3600, "eaglecast", 1, 1, 1),
                       ("e2", "Show", "c1", "c1", t + 60, t + 3700, "primestreams", 1, 2, 1),
                       ("e3", "Show", "c1", "c1", t + 120, t + 3800, "primestreams", 1, 3, 1),
                       ("x", "Odd", "c9", "c9", t, t + 3600, "primestreams", 1, 4, 1)])
        result = self._run(db, carried=carried)
        self.assertEqual(result["not_recorded"], [])
        self.assertTrue({"e3", "x"} <= {e["id"] for e in result["left_alone"]})
        self.assertEqual({v[1] for v in self._state(db).values()}, {"queued"})


class RecordingPlannerTests(unittest.TestCase):
    def _db(self, rows):
        import sqlite3 as sq, tempfile
        db = os.path.join(tempfile.mkdtemp(), "g.db")
        conn = sq.connect(db)
        conn.execute("CREATE TABLE recordings (rec_id, title, channel, start_ts, stop_ts, status, stream_provider, "
                     "season_num, episode_num, is_series, failure_reason)")
        for r in rows:
            conn.execute("INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?,?,?,NULL)", r)
        conn.commit()
        conn.close()
        return db

    def _get(self, db, date):
        with mock.patch.object(server, "_guide_db_path", return_value=db), \
             mock.patch.object(server, "load_config", return_value={"timezone": "UTC"}), \
             mock.patch.object(server, "_primestreams_max_connections", return_value=1):
            return server.app.test_client().get("/epg-web/api/recording-plan?date=" + date)

    def test_the_day_lists_its_recordings_marks_films_and_flags_clashes(self):
        from datetime import datetime, timezone
        noon = datetime(2027, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp()
        db = self._db([("a", "A Film", "ch", noon, noon + 7200, "queued", "eaglecast", None, None, 0),
                       ("b", "A Rerun", "ch", noon + 3600, noon + 7200, "queued", "eaglecast", 2, 5, 1),
                       ("c", "Other Day", "ch", noon + 86400, noon + 90000, "queued", "eaglecast", None, None, 0),
                       ("d", "Gone", "ch", noon, noon + 100, "cancelled", "eaglecast", None, None, 0)])
        data = self._get(db, "2027-01-05").get_json()
        self.assertEqual({i["id"] for i in data["items"]}, {"a", "b"})
        by = {i["id"]: i for i in data["items"]}
        self.assertTrue(by["a"]["film"])
        self.assertFalse(by["b"]["film"])
        self.assertTrue(by["a"]["conflict"] and by["b"]["conflict"])
        self.assertEqual(data["capacity"], {"eaglecast": 1, "primestreams": 1})

    def test_a_bad_date_is_refused_and_the_page_has_the_planner(self):
        self.assertEqual(self._get(self._db([]), "nope").status_code, 400)
        html = server.app.test_client().get("/epg-web").get_data(as_text=True)
        self.assertIn("switchTab('planner')", html)
        self.assertIn("'planner'", html)


class PlannerActionsTests(unittest.TestCase):
    def _db(self, rows):
        import sqlite3 as sq, tempfile
        db = os.path.join(tempfile.mkdtemp(), "g.db")
        conn = sq.connect(db)
        conn.execute("CREATE TABLE recordings (rec_id, title, channel, channel_id, start_ts, stop_ts, status, "
                     "stream_provider, stream_id, stream_extension, season_num, episode_num, is_series, failure_reason)")
        for r in rows:
            conn.execute("INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?,'9','ts',?,?,?,NULL)", r)
        conn.commit()
        conn.close()
        return db

    def test_the_planner_shows_only_what_is_planned_unless_asked(self):
        from datetime import datetime, timezone
        noon = datetime(2027, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp()
        db = self._db([("q", "Planned", "c", "c", noon, noon + 3600, "queued", "eaglecast", None, None, 0),
                       ("f", "Failed", "c", "c", noon + 4000, noon + 5000, "failed", "primestreams", None, None, 0),
                       ("d", "Done", "c", "c", noon + 6000, noon + 7000, "done", "primestreams", None, None, 0)])
        with mock.patch.object(server, "_guide_db_path", return_value=db), \
             mock.patch.object(server, "load_config", return_value={"timezone": "UTC"}):
            client = server.app.test_client()
            planned = client.get("/epg-web/api/recording-plan?date=2027-01-05").get_json()
            everything = client.get("/epg-web/api/recording-plan?date=2027-01-05&all=1").get_json()
        self.assertEqual([i["id"] for i in planned["items"]], ["q"])
        self.assertEqual({i["id"] for i in everything["items"]}, {"q", "f", "d"})

    def test_switching_source_checks_the_channel_and_the_connection(self):
        t = time.time() + 86400
        db = self._db([("a", "Film", "c1", "c1", t, t + 3600, "queued", "primestreams", None, None, 0),
                       ("b", "Other", "c2", "c2", t, t + 3600, "queued", "eaglecast", 2, 1, 1)])
        with mock.patch.object(server, "_guide_db_path", return_value=db), \
             mock.patch.object(server, "_eaglecast_enabled", return_value=True), \
             mock.patch.object(server, "_eaglecast_expiry", return_value=(None, "")), \
             mock.patch.object(server, "_slot_source", return_value=("77", "ts")):
            client = server.app.test_client()
            busy = client.post("/epg-web/api/recording-plan/switch-source", json={"id": "a", "provider": "eaglecast"})
            self.assertEqual(busy.status_code, 409)
            self.assertIn("already booked", busy.get_json()["error"])
            with mock.patch.object(server, "_eaglecast_overlap", return_value=None):
                moved = client.post("/epg-web/api/recording-plan/switch-source", json={"id": "a", "provider": "eaglecast"})
            self.assertEqual(moved.status_code, 200)
            bad = client.post("/epg-web/api/recording-plan/switch-source", json={"id": "a", "provider": "x"})
            self.assertEqual(bad.status_code, 400)
        import sqlite3 as sq
        self.assertEqual(sq.connect(db).execute("SELECT stream_provider, stream_id FROM recordings WHERE rec_id='a'").fetchone(),
                         ("eaglecast", "77"))

    def test_cancelling_other_copies_lists_them_first_and_keeps_the_chosen_one(self):
        t = time.time() + 86400
        db = self._db([("keep", "The Film", "c1", "c1", t, t + 3600, "queued", "eaglecast", None, None, 0),
                       ("dup1", "The Film", "c2", "c2", t + 9000, t + 12000, "queued", "primestreams", None, None, 0),
                       ("dup2", "The Film", "c3", "c3", t + 20000, t + 23000, "scheduled", "primestreams", None, None, 0),
                       ("ep", "The Film", "c4", "c4", t + 30000, t + 33000, "queued", "primestreams", 1, 2, 1),
                       ("other", "Different", "c5", "c5", t, t + 3600, "queued", "primestreams", None, None, 0)])
        cancelled = []

        def fake_cancel():
            cancelled.append(server.request.json["id"])
            return server.jsonify({"ok": True})
        with mock.patch.object(server, "_guide_db_path", return_value=db), \
             mock.patch.object(server, "api_rec_cancel", side_effect=fake_cancel):
            client = server.app.test_client()
            report = client.post("/epg-web/api/recording-plan/cancel-copies", json={"id": "keep"}).get_json()
            self.assertEqual({c["id"] for c in report["copies"]}, {"dup1", "dup2"})
            self.assertEqual(cancelled, [])
            done = client.post("/epg-web/api/recording-plan/cancel-copies", json={"id": "keep", "apply": True}).get_json()
        self.assertEqual(done["cancelled"], 2)
        self.assertEqual(set(cancelled), {"dup1", "dup2"})


class PlannerMissedTests(unittest.TestCase):
    def test_a_queued_booking_whose_time_has_passed_is_missed_and_hidden_not_a_clash(self):
        import sqlite3 as sq, tempfile
        db = os.path.join(tempfile.mkdtemp(), "g.db")
        conn = sq.connect(db)
        conn.execute("CREATE TABLE recordings (rec_id, title, channel, start_ts, stop_ts, status, stream_provider, "
                     "season_num, episode_num, is_series, failure_reason)")
        now = time.time()
        for rid, a, b in (("old1", now - 7200, now - 600), ("old2", now - 5000, now - 300), ("next", now + 3600, now + 7200)):
            conn.execute("INSERT INTO recordings VALUES (?,?,?,?,?,'queued','primestreams',NULL,NULL,0,NULL)", (rid, rid, "ch", a, b))
        conn.commit()
        conn.close()
        with mock.patch.object(server, "_guide_db_path", return_value=db), \
             mock.patch.object(server, "load_config", return_value={"timezone": "UTC"}), \
             mock.patch.object(server, "_primestreams_max_connections", return_value=1):
            client = server.app.test_client()
            from datetime import datetime, timezone
            day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
            default = client.get("/epg-web/api/recording-plan?date=" + day).get_json()
            everything = client.get("/epg-web/api/recording-plan?date=" + day + "&all=1").get_json()
        ids = {i["id"] for i in default["items"]}
        self.assertNotIn("old1", ids)
        self.assertNotIn("old2", ids)
        self.assertFalse(any(i["conflict"] for i in default["items"]))
        by = {i["id"]: i for i in everything["items"]}
        for rid in ("old1", "old2"):
            if rid in by:
                self.assertEqual(by[rid]["status"], "missed")
                self.assertFalse(by[rid]["conflict"])


class CancelOutsideMemoryTests(unittest.TestCase):
    def test_a_booking_not_held_in_memory_is_cancelled_in_the_database(self):
        import sqlite3 as sq, tempfile
        db = os.path.join(tempfile.mkdtemp(), "g.db")
        conn = sq.connect(db)
        conn.execute("CREATE TABLE recordings (rec_id, title, channel, channel_id, start_ts, stop_ts, status, "
                     "stream_provider, season_num, episode_num, failure_reason)")
        conn.execute("INSERT INTO recordings VALUES ('old','Old','c','c',1,2,'queued','primestreams',NULL,NULL,NULL)")
        conn.execute("INSERT INTO recordings VALUES ('busy','Busy','c','c',1,2,'recording','primestreams',NULL,NULL,NULL)")
        conn.commit()
        conn.close()
        server._recs.pop("old", None)
        calls = []
        with mock.patch.object(server, "_guide_db_path", return_value=db), \
             mock.patch.object(server, "_db_update_rec_status", side_effect=lambda *a, **k: calls.append(a)):
            client = server.app.test_client()
            ok = client.post("/epg-web/api/record/cancel", json={"id": "old"})
            refused = client.post("/epg-web/api/record/cancel", json={"id": "busy"})
            missing = client.post("/epg-web/api/record/cancel", json={"id": "nope"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(calls, [("old", "cancelled", "Cancelled by user")])
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(missing.status_code, 404)


class AiringPlayStateTests(unittest.TestCase):
    def test_a_live_airing_is_judged_as_live_viewing_not_by_its_whole_recording_window(self):
        busy_later = 'Eaglecast is already scheduled for overlapping "The Hunger Games", and PrimeStreams has no usable stream for this channel.'
        with mock.patch.object(server, "_live_stream_url", return_value=("http://x/1.ts", None, {"provider": "eaglecast"})), \
             mock.patch.object(server, "_is_foreign_recording_feed", return_value=False):
            ok, note = server._airing_play_state("c1", "NFL RedZone HD", True, busy_later)
            self.assertTrue(ok)
            self.assertEqual(note, "")
            # a later airing is not live, so it keeps the recording check
            later_ok, later_note = server._airing_play_state("c1", "NFL RedZone HD", False, busy_later)
            self.assertFalse(later_ok)
            self.assertIn("busy with", later_note)

    def test_a_live_airing_with_no_live_source_says_why(self):
        with mock.patch.object(server, "_live_stream_url", return_value=(None, "PrimeStreams is switched off, no.", {})), \
             mock.patch.object(server, "_is_foreign_recording_feed", return_value=False):
            ok, note = server._airing_play_state("c1", "X", True, None)
        self.assertFalse(ok)
        self.assertTrue(note)


class RecordingPriorityOverViewingTests(unittest.TestCase):
    def _db(self, provider, start_in, length=3600, status="queued"):
        import sqlite3 as sq, tempfile
        db = os.path.join(tempfile.mkdtemp(), "g.db")
        conn = sq.connect(db)
        conn.execute("CREATE TABLE recordings (rec_id, title, start_ts, stop_ts, status, stream_provider)")
        now = time.time()
        conn.execute("INSERT INTO recordings VALUES ('a','Hunger Games',?,?,?,?)", (now + start_in, now + start_in + length, status, provider))
        conn.commit()
        conn.close()
        return db

    def test_watching_stops_two_minutes_before_a_recording_on_the_same_connection(self):
        with mock.patch.object(server, "_guide_db_path", return_value=self._db("eaglecast", 60)):
            blocked = server._viewing_blocked_by_recording("eaglecast")
            self.assertEqual(blocked[0], "Hunger Games")
            self.assertIn("starts recording at", server._recording_priority_message(blocked))
            self.assertIsNone(server._viewing_blocked_by_recording("primestreams"))
        with mock.patch.object(server, "_guide_db_path", return_value=self._db("eaglecast", 900)):
            self.assertIsNone(server._viewing_blocked_by_recording("eaglecast"))
        with mock.patch.object(server, "_guide_db_path", return_value=self._db("eaglecast", -600, 3600, "recording")):
            self.assertIn("recording on this connection now",
                          server._recording_priority_message(server._viewing_blocked_by_recording("eaglecast")))

    def test_a_provider_with_spare_connections_is_not_blocked_by_one_recording(self):
        with mock.patch.object(server, "_guide_db_path", return_value=self._db("primestreams", 30)), \
             mock.patch.object(server, "_primestreams_max_connections", return_value=2):
            self.assertIsNone(server._viewing_blocked_by_recording("primestreams"))
        with mock.patch.object(server, "_guide_db_path", return_value=self._db("primestreams", 30)), \
             mock.patch.object(server, "_primestreams_max_connections", return_value=1):
            self.assertIsNotNone(server._viewing_blocked_by_recording("primestreams"))

    def test_the_relay_refuses_when_a_recording_is_due_and_stops_itself_when_one_becomes_due(self):
        import io

        class Upstream(io.BytesIO):
            closed_by_relay = False

            def close(self):
                Upstream.closed_by_relay = True
                super().close()
        with mock.patch.object(server, "_viewing_blocked_by_recording", return_value=("Film", time.time() + 30)):
            with server.app.test_request_context("/x"):
                refused = server._relay_live_stream("http://x/1.ts", "eaglecast")
        self.assertEqual(refused[1], 409)
        answers = iter([None, None, ("Film", time.time() + 30), ("Film", time.time() + 30)])
        data = Upstream(b"a" * 65536 * 10)
        original = server.PLAY_GUARD_CHECK_SECONDS
        server.PLAY_GUARD_CHECK_SECONDS = 0
        try:
            with mock.patch.object(server, "_viewing_blocked_by_recording", side_effect=lambda p: next(answers)), \
                 mock.patch("urllib.request.urlopen", return_value=data):
                with server.app.test_request_context("/x"):
                    response = server._relay_live_stream("http://x/1.ts", "eaglecast")
                    chunks = list(response.response)
        finally:
            server.PLAY_GUARD_CHECK_SECONDS = original
        self.assertLess(len(chunks), 10)
        self.assertTrue(Upstream.closed_by_relay)
