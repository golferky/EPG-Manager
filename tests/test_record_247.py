import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mac"))

import record_247 as rec  # noqa: E402

FAKE_FFMPEG = """#!/bin/sh
for last; do :; done
echo "$@" >> "$(dirname "$0")/args"
n=0; [ -f "$(dirname "$0")/count" ] && n=$(cat "$(dirname "$0")/count"); n=$((n+1)); echo $n > "$(dirname "$0")/count"
if [ "$n" = 1 ]; then s=1; else s=2; fi
printf 'part%s;' "$n" > "$last"
echo "warning from the stand-in" >&2
sleep $s
"""


class Dashboard(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        body = {"channels": [{"id": "c1", "name": "EVERYBODY LOVES TEST", "stream_id": 4242}]} if "everybody" in self.path else {"channels": []}
        self.wfile.write(json.dumps(body).encode())


class Record247Tests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Dashboard)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()

    def test_the_channel_is_found_by_part_of_its_name(self):
        self.assertEqual(rec.find_channel(self.base, "everybody")["stream_id"], 4242)
        self.assertIsNone(rec.find_channel(self.base, "nothing like it"))

    def test_a_dropped_stream_is_resumed_the_segments_are_joined_and_the_address_stays_hidden(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = os.path.join(temp, "fake_ffmpeg")
            with open(fake, "w") as handle:
                handle.write(FAKE_FFMPEG)
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            cfg = {"epg_url": "http://provider.invalid:8080", "epg_user": "SECRETUSER", "epg_pass": "SECRETPASS",
                   "ffmpeg": fake, "recording_tail_seconds": 45}
            out = os.path.join(temp, "out.ts")
            messages = []
            old_pause, old_poll = rec.PAUSE_SECONDS, rec.POLL_SECONDS
            rec.PAUSE_SECONDS, rec.POLL_SECONDS = 0.1, 0.2
            try:
                seconds, drops = rec.record(cfg, 4242, 0.55, out, log=messages.append)     # 33 s: room for a second segment
            finally:
                rec.PAUSE_SECONDS, rec.POLL_SECONDS = old_pause, old_poll
            self.assertGreaterEqual(drops, 1)
            with open(out, "rb") as handle:
                data = handle.read()
            self.assertTrue(data.startswith(b"part1;"))
            self.assertIn(b"part2;", data)                                     # the second segment was joined on
            self.assertFalse(any(name.startswith("out.part") for name in os.listdir(temp)))   # no leftover pieces
            arguments = open(os.path.join(temp, "args"), encoding="utf-8").read()
            self.assertNotIn("reconnect", arguments)                           # no ffmpeg reconnect: it repeats a minute
            self.assertIn("-nostdin", arguments)
            self.assertIn("-rw_timeout 120000000", arguments)                  # a short stall is waited out, not reconnected
            logged = open(out + ".log", encoding="utf-8").read()
            for text in messages + [logged]:
                self.assertNotIn("SECRET", text)
                self.assertNotIn("provider.invalid", text)


if __name__ == "__main__":
    unittest.main()
