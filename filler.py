"""Fill an idle PrimeStreams connection with a 24/7 channel, then turn the capture into episodes.

The dashboard decides when there is an opening (no recording due soon, nobody watching) and which channel to capture;
the recorder runs it here.  A capture is always the lowest priority: it ends early the moment the dashboard says so
(a recording or a viewer needs the connection).  Afterwards the capture is repaired, cut into episodes, each episode
is identified from what is said in it, and the confident ones are filed in Plex.  An episode that cannot be
identified goes to the holding folder, never into Plex.  Nothing here ever deletes an episode.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import recording_agent

CAPTURE_DIRNAME = '247 captures'
STATUS_POLL_SECONDS = 5
STALL_TIMEOUT_MICROSECONDS = str(120 * 1000000)
DROP_PAUSE_SECONDS = 15
MAX_EMPTY_IN_A_ROW = 5
TERMINATE_WAIT_SECONDS = 10
MIN_CAPTURE_SECONDS = 20 * 60           # shorter than this is not worth cutting
VIDEO_TYPES = ('.mp4', '.mkv', '.m4v')


def captures_dir(cfg):
    path = Path(os.path.expanduser(cfg['local_recordings'])) / CAPTURE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tools_on_path():
    """The mac/ tools (split, identify) live beside this file; they are imported only when needed."""
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mac')
    if folder not in sys.path:
        sys.path.insert(0, folder)


def build_capture_command(cfg, stream_id, seconds, target):
    """FFmpeg for one segment.  No reconnect options: on a 24/7 feed a reconnect makes the provider restart a minute early."""
    url = recording_agent.stream_url(cfg, stream_id, 'primestreams', 'ts')
    return [cfg.get('ffmpeg', 'ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'warning', '-y',
            '-rw_timeout', STALL_TIMEOUT_MICROSECONDS, '-i', url, '-t', str(int(max(1, seconds))),
            '-c', 'copy', '-f', 'mpegts', str(target)]


def _stop_process(process):
    process.terminate()
    try:
        process.wait(timeout=TERMINATE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def capture(cfg, api, info, popen=subprocess.Popen, now=time.time, sleep=time.sleep, log=print):
    """Record the channel until ``info['until_ts']`` or until the dashboard says stop.

    Returns (path of the joined capture or None, seconds captured, number of drops).  A dropped stream is started
    again after a short pause, as a new segment; the segments are joined at the end.
    """
    folder = captures_dir(cfg)
    stem = folder / ('%s_%d' % (re.sub(r'[^A-Za-z0-9]+', '_', info.get('show') or info.get('channel') or 'channel').strip('_'),
                                int(info['start_ts'])))
    joined = Path(str(stem) + '.ts')
    until = float(info['until_ts'])
    segments, drops, empty, attempt, began, stopped = [], 0, 0, 0, now(), False
    while until - now() > 30 and not stopped:
        segment = recording_agent.segment_path(str(joined), attempt)
        command = build_capture_command(cfg, info['stream_id'], until - now(), segment)
        with open(str(joined) + '.log', 'a', encoding='utf-8') as errors:
            process = popen(command, stdout=subprocess.DEVNULL, stderr=errors, stdin=subprocess.DEVNULL)
            last_poll = now()
            while process.poll() is None:
                sleep(1)
                if now() >= until:
                    _stop_process(process)
                    stopped = True
                    break
                if now() - last_poll >= STATUS_POLL_SECONDS:
                    last_poll = now()
                    try:
                        status = api.filler_status(info['id'], sum(
                            os.path.getsize(part) for part in segments + [segment] if os.path.exists(part)))
                    except Exception:
                        status = {}
                    if status.get('until_ts'):
                        until = min(until, float(status['until_ts']))
                    if status.get('stop'):
                        log('[filler] the dashboard asked it to stop')
                        _stop_process(process)
                        stopped = True
                        break
        segments.append(segment)
        size = os.path.getsize(segment) if os.path.exists(segment) else 0
        empty = empty + 1 if size == 0 else 0
        if stopped or until - now() <= 30 or empty >= MAX_EMPTY_IN_A_ROW:
            break
        drops += 1
        log('[filler] the stream stopped early (drop %d); starting again in %d s' % (drops, DROP_PAUSE_SECONDS))
        sleep(DROP_PAUSE_SECONDS)
        attempt += 1
    seconds = now() - began
    existing = [Path(s) for s in segments if os.path.exists(s) and os.path.getsize(s) > 0]
    for empty_segment in segments:
        if Path(empty_segment) not in existing and os.path.exists(empty_segment):
            os.remove(empty_segment)
    if not existing:
        return None, seconds, drops
    # The first segment is the final file itself: later segments are appended to it.
    first, rest = existing[0], existing[1:]
    if first != joined:
        os.replace(first, joined)
    if rest:
        with open(joined, 'ab') as out:
            for part in rest:
                with open(part, 'rb') as source:
                    shutil.copyfileobj(source, out)
                os.remove(part)
    return joined, seconds, drops


def repair(cfg, path, run=subprocess.run):
    """Copy the capture into a file with a continuous clock (a joined capture has a break at each drop).  Returns the new path."""
    clean = Path(str(path)[:-3] + '.clean.ts')
    result = run([cfg.get('ffmpeg', 'ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path),
                  '-c', 'copy', str(clean)], stdin=subprocess.DEVNULL)
    if result.returncode != 0 or not clean.exists() or clean.stat().st_size == 0:
        return path
    return clean


def holding_folder(cfg, show):
    """Where an episode that could not be identified waits: the holding folder, in a folder for the show."""
    tv_root = cfg.get('plex_tv_path', '/Volumes/Plex/TV Shows')
    base = str(cfg.get('needs_episode_path', '') or '')
    if not base:
        base = os.path.join(os.path.dirname(tv_root.rstrip('/')), 'TV Needing Episode Info')
    return Path(base) / recording_agent.safe_filename(show)


def _unique(path):
    path = Path(path)
    if not path.exists():
        return path
    number = 2
    while True:
        candidate = path.with_name('%s (%d)%s' % (path.stem, number, path.suffix))
        if not candidate.exists():
            return candidate
        number += 1


def _move(source, destination):
    destination = _unique(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return destination


def cut_episodes(cfg, clean_path, info, log=print):
    """Cut the capture into its complete episodes.  Returns [(file, label)] of what was cut."""
    _tools_on_path()
    import split_247 as split
    profile = info.get('profile') or {}
    show = info.get('show') or 'Episode'
    blacks, read_length = split.find_blacks(str(clean_path))
    total = max(read_length, 0.0) or split.probe_duration(str(clean_path))
    episodes, skipped = split.episodes_from_blacks(
        blacks, total, float(profile.get('min_black', 4.0)),
        float(profile.get('min_minutes', 15.0)) * 60, float(profile.get('max_minutes', 45.0)) * 60)
    for start, end, why in skipped:
        log('[filler] skipped %s to %s: %s' % (split.clock(start), split.clock(end), why))
    cuts = split.trim_episodes(episodes, float(profile.get('skip_start', 0.0)), float(profile.get('keep_after', 0.0)))
    began = float(info['start_ts'])
    out_dir = Path(clean_path).parent / 'episodes' / recording_agent.safe_filename(show)
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for cut_start, cut_end in cuts:
        length = cut_end - cut_start
        target = _unique(out_dir / split.episode_filename(show, began, cut_start))
        result = subprocess.run([cfg.get('ffmpeg', 'ffmpeg'), '-nostdin', '-loglevel', 'error', '-i', str(clean_path),
                                 '-ss', '%.2f' % cut_start, '-t', '%.2f' % length, '-c:v', 'copy', '-c:a', 'aac',
                                 str(target)], stdin=subprocess.DEVNULL)
        if result.returncode == 0 and split.cut_is_good(str(target), length):
            made.append(target)
        else:
            log('[filler] a cut did not come out right; removing the unfinished file')
            try:
                target.unlink()
            except OSError:
                pass
    return made


def identify_and_file(cfg, files, info, log=print):
    """Listen to each episode, file the confident ones in Plex, hold the rest.  Returns (filed, held, notes)."""
    _tools_on_path()
    show = info.get('show') or ''
    filed, held, notes = 0, 0, []
    tv_root = cfg.get('plex_tv_path', '/Volumes/Plex/TV Shows')
    plex_ready = recording_agent.plex_mount_available(Path(tv_root), cfg.get('plex_mount_marker', ''))
    rows, year = [], ''
    try:
        import identify_episodes as ident
        import name_episodes as ne
        from episode_match import merge_episodes
        year, tmdb_eps = ne.load_tmdb(cfg['server_url'], show)
        try:
            _y, omdb_eps = ne.load_omdb(cfg['server_url'], show, max_minutes=10)
        except Exception as exc:
            notes.append(f'OMDb plots unavailable ({exc})')
            omdb_eps = []
        episodes = merge_episodes(tmdb_eps, omdb_eps)
        names = [Path(f).name for f in files]
        transcripts, model = {}, None
        for path in files:
            text, model = ident.transcribe(str(path), cfg.get('whisper_model', 'small.en'), model)
            transcripts[Path(path).name] = text
        rows = ident.plan(names, transcripts, episodes)
    except SystemExit as exc:               # the speech package is not installed
        notes.append(str(exc))
    except Exception as exc:
        notes.append(f'could not identify: {exc}')
    by_name = {r['name']: r for r in rows}
    for path in files:
        row = by_name.get(Path(path).name)
        episode = row['episode'] if row and row['status'] == 'ok' else None
        if episode and plex_ready:
            show_dir = recording_agent.resolve_show_dir(tv_root, show, year)
            dest = Path(show_dir) / ('Season %02d' % int(episode['season'])) / ne.target_name(show, episode)
            if dest.exists():
                _move(path, holding_folder(cfg, show) / 'Already in Plex' / Path(path).name)
                held += 1
                log('[filler] %s is already in Plex; kept in the holding folder' % ne.target_name(show, episode))
            else:
                _move(path, dest)
                filed += 1
                log('[filler] filed %s' % dest)
        else:
            _move(path, holding_folder(cfg, show) / Path(path).name)
            held += 1
    return filed, held, notes


def prune_old_captures(cfg, keep_days, now=time.time):
    """Remove working captures (the raw and repaired files) older than ``keep_days``; 0 keeps them all.  Episodes are never touched."""
    if not keep_days:
        return 0
    removed = 0
    cutoff = now() - float(keep_days) * 86400
    for path in captures_dir(cfg).glob('*.ts*'):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def run_capture(cfg, api, info, log=print):
    """One whole capture: record, repair, cut, identify, file.  Reports the outcome to the dashboard."""
    report = {'status': 'failed', 'file': '', 'seconds': 0, 'drops': 0, 'episodes': 0, 'filed': 0, 'held': 0, 'note': ''}
    try:
        path, seconds, drops = capture(cfg, api, info, log=log)
        report.update({'seconds': int(seconds), 'drops': drops})
        if path is None:
            report['note'] = 'nothing was captured'
        elif seconds < MIN_CAPTURE_SECONDS:
            report.update({'status': 'too_short', 'file': str(path), 'note': 'too short to cut into episodes'})
        else:
            clean = repair(cfg, path)
            report['file'] = str(clean)
            files = cut_episodes(cfg, clean, info, log=log)
            report['episodes'] = len(files)
            filed, held, notes = identify_and_file(cfg, files, info, log=log) if files else (0, 0, [])
            report.update({'status': 'done', 'filed': filed, 'held': held, 'note': '; '.join(notes)})
        prune_old_captures(cfg, info.get('keep_days', 7))
    except Exception as exc:
        report['note'] = str(exc)[:300]
        log('[filler] error: %s' % exc)
    try:
        api.filler_finish(info['id'], report)
    except Exception as exc:
        log('[filler] could not report to the dashboard: %s' % exc)
    return report
