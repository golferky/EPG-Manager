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

    def test_verbose_scheduled_status_is_active(self):
        rec = {"status": "scheduled (12m away)"}
        self.assertEqual(server._rec_status_base(rec["status"]), "scheduled")
        self.assertTrue(server._rec_is_active(rec))

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


if __name__ == "__main__":
    unittest.main()
