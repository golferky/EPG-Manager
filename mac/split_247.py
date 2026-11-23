#!/usr/bin/env python3
"""Cut a recording of a 24/7 channel into its complete episodes.

A 24/7 channel has no guide, but the episodes are separated by a repeatable marker.  On the Andy Griffith channel
it is a black screen of four seconds or more that comes just before the whistling theme.  This finds those
long black moments with FFmpeg's blackdetect, treats each one as the start of an episode, keeps the episodes that
are complete, and throws away the partial one at each end of the recording.

    python3 ~/EPG-Manager/mac/split_247.py ~/andy_griffith_2h.ts                 show the plan, cut nothing
    python3 ~/EPG-Manager/mac/split_247.py ~/andy_griffith_2h.ts --cut           cut the episodes

Options:
    --name "The Andy Griffith Show"   the name given to each file
    --out FOLDER                      where the episodes go (default: a folder next to the recording)
    --min-black 4                     seconds of black that mark the start of an episode
    --min-minutes 15 --max-minutes 45 an episode outside this length is reported and skipped
    --skip-start 36.0                 seconds after each marker where the next episode really begins (Raymond)
    --chain-gap 22                    start each episode where the run of black screens after its marker ends (breaks of any layout)
    --keep-after 15.5                 seconds kept past the next marker so the closing credits are not cut off (Raymond)
    --start "2026-11-20 07:00"        when the recording began (default: worked out from the file's time)

The recording is only read.  Nothing is deleted or overwritten.  Cuts are made without re-encoding, starting inside
the black, so an episode may begin a moment before the black ends.
"""
import argparse
import os
import re
import subprocess
import sys
import time

BLACK_LINE = re.compile(r'black_start:([0-9.]+) black_end:([0-9.]+)')


TIME_LINE = re.compile(r'time=(\d+):(\d\d):(\d\d(?:\.\d+)?)')


def parse_total_seconds(log):
    """The length FFmpeg actually read, from its last progress line (0.0 if it printed none).

    ffprobe's length is wrong for a capture that was joined after a drop: the clock restarts at the join, so it
    reports only the part after it.  FFmpeg's own pass over the file corrects the join.
    """
    found = TIME_LINE.findall(log)
    if not found:
        return 0.0
    hours, minutes, seconds = found[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_blacks(log):
    """[(start, end)] of every black moment in FFmpeg's blackdetect output."""
    return [(float(a), float(b)) for a, b in BLACK_LINE.findall(log)]


def episodes_from_blacks(blacks, total, min_black=4.0, min_len=15 * 60, max_len=45 * 60):
    """The complete episodes: ``[(start, end, length)]``, plus a list of ``(start, end, why)`` that were skipped.

    Each black of ``min_black`` seconds or more starts an episode, which ends where the next one starts.  The
    stretch before the first marker and after the last is a partial episode and is dropped.  A stretch outside
    ``min_len``..``max_len`` is skipped too (a gap in the recording, or a marker that was missed).
    """
    starts = [start for start, end in blacks if end - start >= min_black]
    episodes, skipped = [], []
    for here, there in zip(starts, starts[1:]):
        length = there - here
        if length < min_len:
            skipped.append((here, there, 'too short (%.1f min)' % (length / 60)))
        elif length > max_len:
            skipped.append((here, there, 'too long (%.1f min): a marker may have been missed' % (length / 60)))
        else:
            episodes.append((here, there, length))
    return episodes, skipped


def chain_end(blacks, marker_start, gap):
    """Where the run of black screens that follows a marker ends.

    A break between episodes can be one black or several close together (credits, a logo, a bumper, then a short
    black).  Starting at the marker's black, each following black that begins within ``gap`` seconds of the last one's
    end belongs to the same break.  The episode starts where the run ends.
    """
    ordered = sorted(blacks)
    end = None
    for start, finish in ordered:
        if end is None:
            if abs(start - marker_start) < 0.5:
                end = finish
            continue
        if start - end <= gap:
            end = max(end, finish)
        elif start > end:
            break
    return end


def trim_episodes(episodes, skip_start=0.0, keep_after=0.0, blacks=None, chain_gap=0.0):
    """Move each episode's cut points off the marker: ``[(cut_start, cut_end)]``.

    The marker is where a long black begins.  With ``chain_gap`` and the list of black screens, an episode starts where
    the run of close-together blacks after its marker ends (so a break of any layout is handled); otherwise
    ``skip_start`` seconds are skipped after the marker.  Where the credits of an episode sit on the black that is the
    marker, ``keep_after`` seconds past the next marker are kept so they are not cut off.  With none of these set a
    cut starts one second into the black, as before.
    """
    cuts = []
    for start, end, _length in episodes:
        cut_start = None
        if chain_gap and blacks:
            found = chain_end(blacks, start, chain_gap)
            if found:
                cut_start = found - 0.5               # half a second inside the last black, so no frame is missed
        if cut_start is None:
            cut_start = start + (skip_start if skip_start else 1.0)
        cuts.append((cut_start, end + keep_after))
    return cuts


def clock(seconds):
    return '%d:%02d:%02d' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def episode_filename(name, recording_start, offset):
    stamp = time.strftime('%Y-%m-%d_%H%M', time.localtime(recording_start + offset))
    safe = re.sub(r'[\\/:*?"<>|]', '', name).strip()
    return '%s - %s.mp4' % (safe, stamp)


def unique_path(path):
    """``path``, or ``path`` with " (2)", " (3)" ... added before the extension if that name is taken."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    number = 2
    while os.path.exists('%s (%d)%s' % (base, number, ext)):
        number += 1
    return '%s (%d)%s' % (base, number, ext)


def probe_duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
    return float(out)


def find_blacks(path):
    """([(start, end)] of every black moment, the length FFmpeg read in seconds)."""
    print('looking for black moments (this takes a few minutes for a long recording) ...', flush=True)
    log = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-i', path, '-vf', 'blackdetect=d=0.2:pix_th=0.10',
                          '-an', '-f', 'null', '-'], capture_output=True, text=True, stdin=subprocess.DEVNULL).stderr
    return parse_blacks(log), parse_total_seconds(log)


def cut_is_good(target, expected_seconds):
    """True when the cut file exists and is about as long as it should be (an empty cut is a failure)."""
    try:
        if os.path.getsize(target) < 1000000:
            return False
        return abs(probe_duration(target) - expected_seconds) <= max(30.0, expected_seconds * 0.05)
    except (OSError, ValueError):
        return False


def recording_began(path, total):
    """When the recording began: the file's creation time where the system keeps one, else its end minus its length."""
    stat = os.stat(path)
    born = getattr(stat, 'st_birthtime', 0)
    return born if born else stat.st_mtime - total


def main(argv=None):
    parser = argparse.ArgumentParser(description='Cut a 24/7 channel recording into its complete episodes')
    parser.add_argument('recording')
    parser.add_argument('--name', default='Episode')
    parser.add_argument('--out', default='')
    parser.add_argument('--min-black', type=float, default=4.0)
    parser.add_argument('--min-minutes', type=float, default=15.0)
    parser.add_argument('--max-minutes', type=float, default=45.0)
    parser.add_argument('--skip-start', type=float, default=0.0)
    parser.add_argument('--keep-after', type=float, default=0.0)
    parser.add_argument('--chain-gap', type=float, default=0.0)
    parser.add_argument('--start', default='')
    parser.add_argument('--cut', action='store_true', help='actually cut the episodes (otherwise only show the plan)')
    args = parser.parse_args(argv)

    path = os.path.expanduser(args.recording)
    if not os.path.isfile(path):
        print('no such file:', path)
        return 2
    blacks, read_length = find_blacks(path)
    total = max(probe_duration(path), read_length)
    if args.start:
        recording_start = time.mktime(time.strptime(args.start, '%Y-%m-%d %H:%M'))
    else:
        recording_start = recording_began(path, total)
    episodes, skipped = episodes_from_blacks(blacks, total, args.min_black,
                                             args.min_minutes * 60, args.max_minutes * 60)
    long_blacks = [b for b in blacks if b[1] - b[0] >= args.min_black]
    print('recording: %s long, %d black moments, %d of them %.1f s or longer'
          % (clock(total), len(blacks), len(long_blacks), args.min_black))
    print('recording began about', time.strftime('%Y-%m-%d %H:%M', time.localtime(recording_start)))
    print('complete episodes: %d' % len(episodes))
    cuts = trim_episodes(episodes, args.skip_start, args.keep_after, blacks, args.chain_gap)
    for (start, end, length), (cut_start, cut_end) in zip(episodes, cuts):
        print('  %s to %s  (%.1f min)  ->  %s' % (clock(cut_start), clock(cut_end), (cut_end - cut_start) / 60,
                                                   episode_filename(args.name, recording_start, cut_start)))
    for start, end, why in skipped:
        print('  skipped %s to %s: %s' % (clock(start), clock(end), why))
    if long_blacks:
        print('dropped: %s before the first marker and %s after the last (partial episodes)'
              % (clock(long_blacks[0][0]), clock(max(0, total - long_blacks[-1][0]))))
    if not args.cut:
        print('nothing was cut.  Add --cut to cut these episodes.')
        return 0
    if not episodes:
        print('no complete episodes to cut')
        return 1
    out = os.path.expanduser(args.out) if args.out else os.path.splitext(path)[0] + ' episodes'
    os.makedirs(out, exist_ok=True)
    for (cut_start, cut_end) in cuts:
        length = cut_end - cut_start
        target = unique_path(os.path.join(out, episode_filename(args.name, recording_start, cut_start)))
        print('cutting', os.path.basename(target), flush=True)
        # The start is given AFTER the input (read through to it): a capture joined after a drop has a broken clock,
        # and jumping straight to a late position then lands past what FFmpeg believes is the end and writes an
        # empty file.
        result = subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-i', path,
                                 '-ss', '%.2f' % cut_start, '-t', '%.2f' % length,
                                 '-c:v', 'copy', '-c:a', 'aac', target], stdin=subprocess.DEVNULL)
        if result.returncode != 0 or not cut_is_good(target, length):
            print('  that cut did not come out right (empty or the wrong length); removing the unfinished file')
            try:
                os.remove(target)
            except OSError:
                pass
    print('done.  The episodes are in', out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
