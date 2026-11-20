#!/usr/bin/env python3
"""Record a 24/7 channel straight from the provider, the way the recorder does, into one .ts file.

    python3 ~/EPG-Manager/mac/record_247.py "everybody loves" --minutes 150
    python3 ~/EPG-Manager/mac/record_247.py "andy griffith" --minutes 60 --out ~/andy.ts

It finds the channel through the dashboard, builds the stream address from the recorder's own settings
(~/EPG-Manager/recording_agent.json) and never prints it.  If the provider drops the stream, a new segment is
started after a short pause, like the recorder's resume, and the segments are joined at the end.  This is for
testing and for capturing a 24/7 channel before splitting it with split_247.py; it does not touch the dashboard,
Plex or the recorder.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import recording_agent  # noqa: E402

PAUSE_SECONDS = 15
# How long a stall is waited out before the stream is given up on.  On a 24/7 channel a short stall usually
# recovers by itself, whereas giving up and reconnecting makes the provider restart the feed about a minute EARLIER
# (a repeated minute).  The recorder's 15 s is meant for live channels.
STALL_TIMEOUT_MICROSECONDS = str(120 * 1000000)
POLL_SECONDS = 5
MAX_EMPTY_IN_A_ROW = 5


def find_channel(server, query):
    url = server.rstrip('/') + '/epg-web/api/247channels?q=' + urllib.parse.quote(query)
    channels = json.load(urllib.request.urlopen(url, timeout=30)).get('channels', [])
    return channels[0] if channels else None


def record(cfg, stream_id, minutes, out, log=print):
    """Record for ``minutes`` into ``out``; returns (seconds recorded, number of drops)."""
    url = recording_agent.stream_url(cfg, stream_id, 'primestreams', 'ts')
    end_time = time.time() + minutes * 60
    segments, drops, empty, attempt = [], 0, 0, 0
    began = time.time()
    while end_time - time.time() > 30:
        segment = recording_agent.segment_path(out, attempt)
        # No ffmpeg reconnect options: on a 24/7 channel the provider restarts the feed a minute or so EARLIER when
        # ffmpeg re-opens a dropped connection, which repeats that minute in the file.  A dropped stream ends this
        # ffmpeg instead, and the next segment starts at the live point.  The hidden banner would print the address.
        cmd = [cfg.get('ffmpeg', 'ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'warning', '-y',
               '-rw_timeout', STALL_TIMEOUT_MICROSECONDS, '-i', url, '-t', str(int(end_time - time.time())),
               '-c', 'copy', '-f', 'mpegts', str(segment)]
        with open(str(out) + '.log', 'a', encoding='utf-8') as errors:
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errors, stdin=subprocess.DEVNULL)
            last_report = time.time()
            while process.poll() is None:
                time.sleep(POLL_SECONDS)
                if time.time() - last_report >= 60:
                    last_report = time.time()
                    size = sum(os.path.getsize(s) for s in segments + [segment] if os.path.exists(s)) / 1e6
                    log('  %d min recorded, %.0f MB, %d drop(s) so far' % ((time.time() - began) / 60, size, drops))
        segments.append(segment)
        size = os.path.getsize(segment) if os.path.exists(segment) else 0
        empty = empty + 1 if size == 0 else 0
        if end_time - time.time() <= 30 or empty >= MAX_EMPTY_IN_A_ROW:
            break
        drops += 1
        log('the stream stopped early (drop %d); starting again in %d s' % (drops, PAUSE_SECONDS))
        time.sleep(PAUSE_SECONDS)
        attempt += 1
    with open(out, 'ab') as joined:
        for extra in segments[1:]:
            if os.path.exists(extra):
                with open(extra, 'rb') as part:
                    joined.write(part.read())
                os.remove(extra)
    return time.time() - began, drops


def main(argv=None):
    parser = argparse.ArgumentParser(description='Record a 24/7 channel directly from the provider')
    parser.add_argument('channel', help='part of the channel name, for example "everybody loves"')
    parser.add_argument('--minutes', type=float, default=150.0)
    parser.add_argument('--out', default='')
    parser.add_argument('--config', default=os.path.expanduser('~/EPG-Manager/recording_agent.json'))
    args = parser.parse_args(argv)
    cfg = recording_agent.load_config(args.config)
    channel = find_channel(cfg['server_url'], args.channel)
    if not channel:
        print('no 24/7 channel matches', args.channel)
        return 2
    out = os.path.expanduser(args.out) if args.out else os.path.expanduser(
        '~/%s_%dm.ts' % (''.join(c for c in args.channel.lower() if c.isalnum()), int(args.minutes)))
    if os.path.exists(out):
        print('that file already exists, so nothing was recorded:', out)
        return 2
    print('recording "%s" for %g minutes into %s (stream address not shown)' % (channel['name'], args.minutes, out))
    seconds, drops = record(cfg, channel['stream_id'], args.minutes, out)
    size = os.path.getsize(out) / 1e6 if os.path.exists(out) else 0
    print('done: %.0f minutes, %.0f MB, %d drop(s).  Error details are in %s.log' % (seconds / 60, size, drops, out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
