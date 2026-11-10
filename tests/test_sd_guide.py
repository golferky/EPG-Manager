import json
import os
import tempfile
import time
import unittest
from unittest import mock

import sd_guide


class TokenCacheTests(unittest.TestCase):
    def _run(self, cache_bytes):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "sd_token.json")
            if cache_bytes is not None:
                with open(path, "wb") as fh:
                    fh.write(cache_bytes)
            with mock.patch.object(sd_guide, "SD_TOKEN_FILE", path), \
                 mock.patch.object(sd_guide, "_req", return_value={"code": 0, "token": "fresh"}) as req:
                token = sd_guide.get_token("user", "pass")
            with open(path) as fh:
                saved = json.load(fh)
        return token, req.called, saved

    def test_a_valid_cached_token_is_used_without_logging_in(self):
        cache = json.dumps({"token": "cached", "expires": time.time() + 1000}).encode()
        token, logged_in, _ = self._run(cache)
        self.assertEqual(token, "cached")
        self.assertFalse(logged_in)

    def test_a_cache_file_of_blank_bytes_is_ignored_and_rewritten(self):
        token, logged_in, saved = self._run(b"\x00" * 64)
        self.assertEqual(token, "fresh")
        self.assertTrue(logged_in)
        self.assertEqual(saved["token"], "fresh")

    def test_an_expired_or_malformed_cache_logs_in_again(self):
        for cache in (json.dumps({"token": "old", "expires": 1}).encode(), b"[1, 2]", b"{not json"):
            token, logged_in, _ = self._run(cache)
            self.assertEqual(token, "fresh", cache)
            self.assertTrue(logged_in)

    def test_no_cache_file_logs_in(self):
        token, logged_in, _ = self._run(None)
        self.assertEqual((token, logged_in), ("fresh", True))


class DatabaseLockTests(unittest.TestCase):
    def test_waits_for_another_writer_instead_of_failing(self):
        import sqlite3
        import threading
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, "guide.db")
            sd_guide.ensure_guide_db(db)
            locked = threading.Event()

            def hold_the_lock():
                # SQLite connections belong to one thread: take and release the lock here.
                conn = sqlite3.connect(db, isolation_level=None)
                conn.execute("BEGIN EXCLUSIVE")
                locked.set()
                time.sleep(1.0)
                conn.execute("COMMIT")
                conn.close()

            holder = threading.Thread(target=hold_the_lock)
            holder.start()
            self.assertTrue(locked.wait(5))
            started = time.time()
            sd_guide.ensure_guide_db(db)            # must wait for the lock, not raise
            self.assertGreaterEqual(time.time() - started, 0.5)
            holder.join()

    def test_every_database_connection_asks_for_a_long_wait(self):
        import inspect
        import re as _re
        source = inspect.getsource(sd_guide)
        calls = _re.findall(r"sqlite3\.connect\([^)]*\)", source)
        self.assertGreaterEqual(len(calls), 2)
        for call in calls:
            match = _re.search(r"timeout\s*=\s*(\d+)", call)
            self.assertTrue(match and int(match.group(1)) >= 60, call)


class WriteListingsTests(unittest.TestCase):
    WS, WE = "20261110000000", "20261201000000"

    def _db(self, temp):
        import sqlite3
        db = os.path.join(temp, "guide.db")
        sd_guide.ensure_guide_db(db)
        return db, sqlite3.connect(db, timeout=0.2)

    @staticmethod
    def _airing(sid, start="2026-11-12T14:00:00Z"):
        return {"stationID": sid, "start": start, "duration": 3600}

    def _count(self, db, where="1=1"):
        import sqlite3
        c = sqlite3.connect(db)
        n = c.execute(f"select count(*) from guide where {where}").fetchone()[0]
        c.close()
        return n

    def _seed(self, conn):
        conn.execute("insert into guide(title,channel_id,channel_name,start_utc,end_utc,desc,category) "
                     "values ('Old','S1','One','20261112140000','20261112150000','','')")
        conn.execute("insert into guide(title,channel_id,channel_name,start_utc,end_utc,desc,category) "
                     "values ('Other','S9','Nine','20261112140000','20261112150000','','')")
        conn.execute("insert into guide(title,channel_id,channel_name,start_utc,end_utc,desc,category) "
                     "values ('Past','S1','One','20260101140000','20260101150000','','')")
        conn.commit()

    def test_replaces_the_window_for_the_given_stations_only(self):
        with tempfile.TemporaryDirectory() as temp:
            db, conn = self._db(temp)
            self._seed(conn)
            inserted, removed = sd_guide.write_listings(
                conn, ["S1"], {"P1": [self._airing("S1")]},
                {"P1": {"title": "New", "category": "", "desc": ""}}, {"S1": {"name": "One"}},
                self.WS, self.WE, log=lambda m: None)
            conn.close()
            self.assertEqual((inserted, removed), (1, 1))
            self.assertEqual(self._count(db, "title='Old'"), 0)       # superseded
            self.assertEqual(self._count(db, "title='New'"), 1)
            self.assertEqual(self._count(db, "title='Other'"), 1)     # other station untouched
            self.assertEqual(self._count(db, "title='Past'"), 1)      # outside the window untouched

    def test_a_group_that_fails_is_rolled_back_and_earlier_groups_are_kept(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            db, conn = self._db(temp)
            self._seed(conn)
            real_execute = conn.execute
            calls = {"n": 0}

            class Proxy:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, sql, *a):
                    if sql.startswith("DELETE"):
                        calls["n"] += 1
                        if calls["n"] == 2:
                            raise sqlite3.OperationalError("disk I/O error")
                    return conn.execute(sql, *a)

            with mock.patch.object(sd_guide, "STATION_GROUP", 1):
                with self.assertRaises(sqlite3.OperationalError):
                    sd_guide.write_listings(
                        Proxy(), ["S1", "S2"],
                        {"P1": [self._airing("S1")], "P2": [self._airing("S2")]},
                        {"P1": {"title": "New1", "category": "", "desc": ""},
                         "P2": {"title": "New2", "category": "", "desc": ""}},
                        {"S1": {"name": "One"}, "S2": {"name": "Two"}},
                        self.WS, self.WE, log=lambda m: None)
            conn.close()
            self.assertEqual(self._count(db, "title='New1'"), 1)      # first group committed
            self.assertEqual(self._count(db, "title='New2'"), 0)      # failed group not written

    def test_a_busy_database_is_retried(self):
        import sqlite3
        import threading
        with tempfile.TemporaryDirectory() as temp:
            db, conn = self._db(temp)
            conn.execute("PRAGMA busy_timeout=100")
            locked = threading.Event()

            def hold_the_lock():
                other = sqlite3.connect(db, isolation_level=None)
                other.execute("BEGIN EXCLUSIVE")
                locked.set()
                time.sleep(1.5)
                other.execute("COMMIT")
                other.close()

            holder = threading.Thread(target=hold_the_lock)
            holder.start()
            self.assertTrue(locked.wait(5))
            inserted, _removed = sd_guide.write_listings(
                conn, ["S1"], {"P1": [self._airing("S1")]},
                {"P1": {"title": "New", "category": "", "desc": ""}}, {"S1": {"name": "One"}},
                self.WS, self.WE, log=lambda m: None, pause=0.3)
            holder.join()
            conn.close()
            self.assertEqual(inserted, 1)
            self.assertEqual(self._count(db, "title='New'"), 1)

    def test_other_errors_are_not_retried(self):
        import sqlite3
        calls = []

        def action():
            calls.append(1)
            raise sqlite3.OperationalError("no such table: guide")
        with self.assertRaises(sqlite3.OperationalError):
            sd_guide._retry_locked(action, attempts=5, pause=0)
        self.assertEqual(len(calls), 1)
