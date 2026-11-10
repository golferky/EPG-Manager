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
