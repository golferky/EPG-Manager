#!/usr/bin/env python3
"""Mac recording agent for EPG Manager.

The agent polls an EPG Manager server for recording jobs, records locally with
FFmpeg, and transfers verified MP4 files to a mounted Plex library.
"""

import unicodedata
import argparse
import base64
import difflib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


MEDIA_EXTENSIONS = {'.mp4', '.mkv', '.m4v', '.ts'}
DEFAULT_RECORDING_TAIL_SECONDS = 45

# Shown in the recorder's log when it connects.  Bump it with every change to the recorder,
# the same way as the dashboard's VERSION: "v" + the date + a letter (a, b, c ...), the
# letter starting again at "a" on a new day.
AGENT_VERSION = "v20261114a"
TERMINAL_STATES = {
    'done', 'done_ts', 'cancelled', 'failed', 'error',
    'skipped_existing_better', 'skipped_too_short',
}


def load_config(path):
    with open(path, encoding='utf-8') as handle:
        cfg = json.load(handle)
    required = ('server_url', 'agent_token', 'epg_url', 'epg_user', 'epg_pass')
    missing = [key for key in required if not str(cfg.get(key, '')).strip()]
    if missing:
        raise ValueError(f'Missing agent configuration: {", ".join(missing)}')
    cfg.setdefault('agent_id', f'{socket.gethostname()}-recorder')
    cfg.setdefault('poll_seconds', 10)
    cfg.setdefault('heartbeat_seconds', 20)
    cfg.setdefault('lease_seconds', 90)
    cfg.setdefault('claim_ahead_seconds', 300)
    cfg.setdefault('max_concurrent_recordings', 6)
    cfg.setdefault('local_recordings', os.path.expanduser('~/Movies/Recordings'))
    cfg.setdefault('plex_path', '/Volumes/Plex/Movies')
    cfg.setdefault('plex_tv_path', '/Volumes/Plex/TV Shows')
    # Keep partial captures on the QNAP, but outside Plex's movie/TV folders
    # so an incomplete game cannot be scanned as a finished title.
    cfg.setdefault('incomplete_recordings_path', '')
    cfg.setdefault('ffmpeg', 'ffmpeg')
    cfg.setdefault('ffprobe', 'ffprobe')
    # Decode every finished recording once and count the errors: a stream stall can leave a
    # file the right length that is corrupt in places (green or blocky video).
    cfg.setdefault('check_video_after_recording', True)
    cfg.setdefault('damaged_error_lines', DAMAGED_ERROR_LINES)
    cfg.setdefault('transfer_retry_seconds', 30)
    cfg.setdefault('transfer_wait_timeout', 86400)
    # A small cushion keeps a provider's slightly-late final scene/credits from
    # being cut off, without routinely capturing much of the following program.
    cfg.setdefault('recording_tail_seconds', DEFAULT_RECORDING_TAIL_SECONDS)
    try:
        cfg['max_concurrent_recordings'] = max(
            1, min(int(cfg['max_concurrent_recordings']), 16)
        )
    except (TypeError, ValueError):
        cfg['max_concurrent_recordings'] = 6
    try:
        cfg['recording_tail_seconds'] = max(0, min(int(cfg['recording_tail_seconds']), 120))
    except (TypeError, ValueError):
        cfg['recording_tail_seconds'] = DEFAULT_RECORDING_TAIL_SECONDS
    return cfg


class AgentAPI:
    def __init__(self, cfg):
        self.base = cfg['server_url'].rstrip('/')
        self.token = cfg['agent_token']
        self.agent_id = cfg['agent_id']
        self.lease_seconds = int(cfg['lease_seconds'])

    def _request(self, method, path, payload=None, timeout=30):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header('Authorization', f'Bearer {self.token}')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'EPG API HTTP {exc.code}: {detail}') from exc

    def health(self):
        return self._request('GET', '/epg-web/api/agent/health')

    def claim(self, claim_ahead_seconds):
        return self._request('POST', '/epg-web/api/agent/jobs/claim', {
            'agent_id': self.agent_id,
            'agent_version': AGENT_VERSION,
            'lease_seconds': self.lease_seconds,
            'claim_ahead_seconds': int(claim_ahead_seconds),
        }).get('job')

    def note_stream_info(self, job_id, probe):
        """Remember what the incoming stream looks like, so every heartbeat can report it."""
        keep = ('width', 'height', 'fps', 'video_codec', 'audio_codec', 'audio_channels', 'total_bitrate')
        if not hasattr(self, '_stream_info'):
            self._stream_info = {}
        self._stream_info[job_id] = {key: probe.get(key) for key in keep if probe.get(key)}

    def heartbeat(self, job_id, status, **fields):
        payload = {
            'agent_id': self.agent_id,
            'agent_version': AGENT_VERSION,
            'lease_seconds': self.lease_seconds,
            'status': status,
            **fields,
        }
        info = getattr(self, '_stream_info', {}).get(job_id)
        if info and 'stream_info' not in payload:
            payload['stream_info'] = info
        return self._request(
            'POST', f'/epg-web/api/agent/jobs/{job_id}/heartbeat', payload
        )

    def patient_heartbeat(self, job_id, status, wait_seconds=7200, pause=20, **fields):
        """A heartbeat that rides out a server outage.

        Used once a recording exists: an unreachable server must not make the recorder
        abandon a finished file.  Network failures and server errors are retried every
        ``pause`` seconds for up to ``wait_seconds``; an HTTP error that retrying cannot
        fix (job not found, leased elsewhere) is raised at once.
        """
        deadline = time.time() + wait_seconds
        while True:
            try:
                return self.heartbeat(job_id, status, **fields)
            except RuntimeError as exc:
                text = str(exc)
                if text.startswith('EPG API HTTP') and not text.startswith('EPG API HTTP 5'):
                    raise
                error = exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                error = exc
            if time.time() >= deadline:
                raise error
            print(f'[heartbeat] server unavailable ({error}); retrying in {pause}s', file=sys.stderr, flush=True)
            time.sleep(pause)

    def movie_year(self, title):
        """Ask the EPG server's cached guide/OMDb lookup for a movie year."""
        path = '/epg-web/api/prog-info?' + urllib.parse.urlencode({'title': title})
        info = self._request('GET', path)
        match = re.search(r'\b(19\d{2}|20\d{2})\b', str(info.get('year', '')))
        return match.group(1) if match else ''


def safe_filename(value):
    cleaned = re.sub(r'[<>:"/\\|?*]', '', value or '').strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned[:140] or 'Untitled'


def split_title_year(title):
    match = re.match(r'^(.+?)\s*\((19\d{2}|20\d{2})\)\s*$', title or '')
    if match:
        return match.group(1).strip(), match.group(2)
    return (title or '').strip(), ''


_TITLE_LIGATURES = {'\u00e6': 'ae', '\u0153': 'oe', '\u00df': 'ss', '\u00f8': 'o', '\u0111': 'd',
                    '\u00f0': 'd', '\u00fe': 'th', '\u0142': 'l', '\u0131': 'i'}


def fold_title(value):
    """Lower case, accents and ligatures folded to plain letters, only letters and digits kept
    (so "\u00c6on Flux" and "Aeon Flux" are the same title)."""
    text = str(value or '').lower().replace('&', ' and ')
    for special, plain in _TITLE_LIGATURES.items():
        text = text.replace(special, plain)
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r'[^a-z0-9]', '', text)


def normalized_title(title):
    base, _year = split_title_year(title)
    return fold_title(base)


def parse_rate(value):
    try:
        if '/' in str(value):
            numerator, denominator = str(value).split('/', 1)
            return float(numerator) / float(denominator) if float(denominator) else 0.0
        return float(value or 0)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def safe_probe_reason(stderr):
    """A short reason from ffprobe's error text, with every stream address removed.

    The address holds the provider login, so it must never reach a log or the dashboard.
    """
    text = str(stderr or '')
    found = None
    for found in STREAM_ERROR_LINE.finditer(text):
        pass
    line = found.group(1) if found else next((ln for ln in text.splitlines() if ln.strip()), '')
    line = re.sub(r'\b\w+://\S+', '<address>', line)
    return re.sub(r'\s+', ' ', line).strip(' .')[:120]


def probe_media(target, ffprobe='ffprobe', timeout=30):
    cmd = [
        ffprobe, '-v', 'error', '-show_entries',
        'format=duration,size,bit_rate:'
        'stream=codec_type,codec_name,profile,width,height,avg_frame_rate,'
        'r_frame_rate,bit_rate,sample_rate,channels',
        '-of', 'json', str(target),
    ]
    is_stream = str(target).startswith(('http://', 'https://'))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if is_stream:
            raise RuntimeError(f'ffprobe failed for incoming stream: no answer in {timeout} s')
        raise
    if result.returncode != 0:
        if is_stream:
            reason = safe_probe_reason(result.stderr)
            raise RuntimeError('ffprobe failed for incoming stream' + (f': {reason}' if reason else ''))
        raise RuntimeError(result.stderr.strip() or f'ffprobe failed for {target}')
    raw = json.loads(result.stdout)
    video = next((s for s in raw.get('streams', []) if s.get('codec_type') == 'video'), {})
    audio = next((s for s in raw.get('streams', []) if s.get('codec_type') == 'audio'), {})
    frame_rate = parse_rate(video.get('avg_frame_rate') or video.get('r_frame_rate'))
    return {
        'width': int(video.get('width') or 0),
        'height': int(video.get('height') or 0),
        'fps': round(frame_rate, 3),
        'video_codec': video.get('codec_name', ''),
        'video_profile': video.get('profile', ''),
        'video_bitrate': int(video.get('bit_rate') or 0),
        'total_bitrate': int(raw.get('format', {}).get('bit_rate') or 0),
        'audio_codec': audio.get('codec_name', ''),
        'audio_sample_rate': int(audio.get('sample_rate') or 0),
        'audio_channels': int(audio.get('channels') or 0),
        'duration': float(raw.get('format', {}).get('duration') or 0),
        'size': int(raw.get('format', {}).get('size') or 0),
    }


def quality_decision(existing, incoming):
    """Return (record, explanation) using resolution as the primary signal."""
    existing_pixels = existing.get('width', 0) * existing.get('height', 0)
    incoming_pixels = incoming.get('width', 0) * incoming.get('height', 0)
    if incoming_pixels > existing_pixels:
        return True, (f'upgrade {existing.get("height", 0)}p → '
                      f'{incoming.get("height", 0)}p')
    if incoming_pixels < existing_pixels:
        return False, (f'Plex is {existing.get("height", 0)}p; incoming is '
                       f'{incoming.get("height", 0)}p')

    existing_fps = existing.get('fps', 0)
    incoming_fps = incoming.get('fps', 0)
    if incoming_fps >= existing_fps * 1.25 and incoming_fps - existing_fps >= 10:
        return True, f'upgrade frame rate {existing_fps:.1f} → {incoming_fps:.1f} fps'
    if existing_fps >= incoming_fps * 1.25 and existing_fps - incoming_fps >= 10:
        return False, f'Plex frame rate is better ({existing_fps:.1f} vs {incoming_fps:.1f} fps)'

    existing_rate = existing.get('video_bitrate') or existing.get('total_bitrate') or 0
    incoming_rate = incoming.get('video_bitrate') or incoming.get('total_bitrate') or 0
    if existing_rate and incoming_rate >= existing_rate * 1.20:
        return True, f'upgrade bitrate {existing_rate // 1000} → {incoming_rate // 1000} kbps'
    return False, 'existing Plex copy is equal or better quality'


def incomplete_for_scheduled_window(existing, start_ts, stop_ts):
    """True when the Plex copy is plainly shorter than this scheduled airing.

    Provider streams can end cleanly from FFmpeg's point of view even when the
    provider stops sending them early.  A 60-minute file for a 160-minute movie
    must be eligible for replacement even if both copies are 720p.
    """
    duration = float(existing.get('duration') or 0)
    scheduled = float(stop_ts or 0) - float(start_ts or 0)
    return scheduled >= 900 and duration > 0 and duration < scheduled * 0.80


# A complete recording runs a little LONGER than its slot (it starts a few seconds
# early and keeps a tail), so anything well short of the slot lost real content.
SHORT_RECORDING_MIN_SECONDS = 180
SHORT_RECORDING_FRACTION = 0.04


def recording_missing_seconds(recorded, start_ts, stop_ts):
    """Seconds of the scheduled slot that a fresh recording did not capture.

    Returns 0 when the recording is complete enough.  This is stricter than
    ``incomplete_for_scheduled_window`` (which allows 20% short, because it also
    judges library files that may be edited versions): a recording is of the
    very stream and slot we scheduled, so a hole of a few minutes means missing
    scenes - for example the opening, after the provider cut the stream.
    """
    duration = float(recorded.get('duration') or 0)
    scheduled = float(stop_ts or 0) - float(start_ts or 0)
    if scheduled < 900 or duration <= 0:
        return 0
    missing = scheduled - duration
    allowed = max(SHORT_RECORDING_MIN_SECONDS, scheduled * SHORT_RECORDING_FRACTION)
    return missing if missing > allowed else 0


def _name_year(name):
    """The (YYYY) at the end of a Plex folder or file name, or ''."""
    return split_title_year(name)[1]


def find_plex_candidates(plex_root, title, year=''):
    """Existing Plex copies of ``title``.

    With a ``year`` only an entry for that year is a candidate.  One named for a
    different year is a different film (a remake with the same title), and one with
    no year in its name cannot be shown to be the same film, so it is not assumed.
    """
    year = str(year or '').strip()[:4] or split_title_year(title)[1]
    root = Path(plex_root)
    if not root.is_dir():
        return []
    wanted = normalized_title(title)
    candidates = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        entry_year = _name_year(entry.name if entry.is_dir() else entry.stem)
        if year and entry_year != year:
            continue
        if entry.is_dir() and normalized_title(entry.name) == wanted:
            candidates.extend(
                path for path in entry.iterdir()
                if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
            )
        elif (entry.is_file() and entry.suffix.lower() in MEDIA_EXTENSIONS and
              normalized_title(entry.stem) == wanted):
            candidates.append(entry)
    return candidates


def episode_metadata(job):
    """Return validated TV episode data, or None when this is a movie job."""
    try:
        season = int(job.get('season_num'))
        episode = int(job.get('episode_num'))
    except (TypeError, ValueError):
        return None
    if season < 0 or episode < 0:
        return None
    return {
        'season': season,
        'episode': episode,
        'title': safe_filename(job.get('episode_title') or ''),
    }


def resolve_show_dir(tv_root, show_title, year=''):
    """The Plex folder for a show: an existing one if there is one, else the new name.

    ``Show`` and ``Show (1964)`` are the same title, so either is found.  With a year,
    a folder named for a DIFFERENT year is another show (a reboot) and is never used.
    A folder with no year is used while the library is being renamed; a folder for the
    exact year is preferred.  A show Plex does not have yet gets ``Show (Year)`` when
    the year is known, else ``Show``.
    """
    base, title_year = split_title_year(show_title)
    year = str(year or '').strip()[:4] or title_year
    wanted = normalized_title(base)
    root = Path(tv_root)
    matches = []
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith(('.', '_')):
            continue
        if normalized_title(entry.name) != wanted:
            continue
        entry_year = _name_year(entry.name)
        if year and entry_year and entry_year != year:
            continue
        matches.append(entry)
    if matches:
        matches.sort(key=lambda entry: _name_year(entry.name) != year)   # exact year first
        return matches[0]
    name = f'{base} ({year})' if year else base
    return root / safe_filename(name)


def find_tv_episode_candidates(tv_root, show_title, season, episode, year=''):
    """Find only the matching SxxExx episode within this show's Plex folder."""
    show_dir = resolve_show_dir(tv_root, show_title, year)
    if not show_dir.is_dir():
        return []
    pattern = re.compile(rf'\bS{season:02d}E{episode:02d}\b', re.IGNORECASE)
    try:
        return [path for path in show_dir.rglob('*')
                if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
                and pattern.search(path.stem)]
    except OSError:
        return []


def private_movies_root(cfg):
    """Folder, outside the Plex Movies library, where private films are kept."""
    configured = str(cfg.get('private_movies_path', '') or '').strip()
    if configured:
        return Path(os.path.expanduser(configured))
    return Path(cfg['plex_path']).parent / 'Movies Private'


def find_movie_candidates(cfg, library_root, title, year=''):
    """Existing copies of a film: in the Plex library, and in the private folder.

    A private film is hidden from Plex, but it is still a film we have: finding it here
    stops the recorder making a second copy.
    """
    roots, found = [], []
    for root in (library_root, cfg.get('plex_path', ''), private_movies_root(cfg)):
        if root and str(root) not in roots:
            roots.append(str(root))
            found += find_plex_candidates(root, title, year)
    return found


def ensure_private_root(cfg):
    """The private folder, created if the Plex share is mounted and it is not there yet."""
    root = private_movies_root(cfg)
    if not root.is_dir() and plex_mount_available(Path(cfg['plex_path']), cfg.get('plex_mount_marker', '')):
        try:
            root.mkdir(parents=False, exist_ok=True)
        except OSError:
            pass
    return root


def best_existing_copy(paths, ffprobe):
    probed = []
    for path in paths:
        try:
            probed.append((path, probe_media(path, ffprobe=ffprobe)))
        except Exception as exc:
            print(f'[quality] unable to probe {path}: {exc}', flush=True)
    if not probed:
        return None, None
    return max(probed, key=lambda item: (
        item[1]['width'] * item[1]['height'], item[1]['fps'],
        item[1]['video_bitrate'] or item[1]['total_bitrate'],
    ))


def stream_url(cfg, stream_id, provider='primestreams', extension='ts'):
    """Resolve credentials locally for the source selected by the EPG server."""
    if provider == 'eaglecast':
        path = os.path.expanduser(cfg.get(
            'eaglecast_config_path', '~/epg/eaglecast_test_config.json'
        ))
        try:
            with open(path, encoding='utf-8') as handle:
                eaglecast = json.load(handle)
            base = str(eaglecast['server_url']).rstrip('/')
            username = urllib.parse.quote(str(eaglecast['username']), safe='')
            password = urllib.parse.quote(str(eaglecast['password']), safe='')
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f'Eaglecast settings are unavailable on this Mac: {exc}') from exc
        return f"{base}/live/{username}/{password}/{stream_id}.{str(extension or 'ts').lstrip('.')}"
    base = cfg['epg_url'].rstrip('/')
    return f"{base}/live/{cfg['epg_user']}/{cfg['epg_pass']}/{stream_id}.ts"


def heartbeat_sleep(api, job, status, seconds, cfg, **fields):
    deadline = time.time() + max(0, seconds)
    while time.time() < deadline:
        try:
            response = api.heartbeat(job['id'], status, **fields)
        except Exception as exc:
            # A short Flask restart must not make the Mac abandon a claimed job.
            print(f'[heartbeat] server unavailable: {exc}', file=sys.stderr, flush=True)
            response = {}
        if response.get('cancel_requested'):
            return False
        time.sleep(min(float(cfg['heartbeat_seconds']), max(0.1, deadline - time.time())))
    return True


class WrongProgramme(str):
    """Returned by run_process when the stream is clearly showing a different programme."""


def provider_api(cfg, provider):
    """(base, username, password) for a provider's Xtream player API, or None."""
    if provider == 'eaglecast':
        path = os.path.expanduser(cfg.get(
            'eaglecast_config_path', '~/epg/eaglecast_test_config.json'))
        try:
            with open(path, encoding='utf-8') as handle:
                eagle = json.load(handle)
            return (str(eagle['server_url']).rstrip('/'),
                    str(eagle['username']), str(eagle['password']))
        except (OSError, KeyError, TypeError, ValueError):
            return None
    try:
        return cfg['epg_url'].rstrip('/'), cfg['epg_user'], cfg['epg_pass']
    except (KeyError, AttributeError):
        return None


def _decode_title(value):
    text = str(value or '')
    try:
        return base64.b64decode(text, validate=True).decode('utf-8', 'replace').strip()
    except Exception:
        return text.strip()


def fetch_now_playing(cfg, provider, stream_id, now=None, timeout=10):
    """The programme the provider says is on a stream right now, or None if unknown."""
    api = provider_api(cfg, provider)
    if not api or not stream_id:
        return None
    base, user, password = api
    now = now if now is not None else time.time()
    query = urllib.parse.urlencode({
        'username': user, 'password': password,
        'action': 'get_short_epg', 'stream_id': stream_id, 'limit': 4})
    try:
        with urllib.request.urlopen(f'{base}/player_api.php?{query}', timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace'))
    except Exception:
        return None
    for item in (data.get('epg_listings') or []) if isinstance(data, dict) else []:
        try:
            start = float(item.get('start_timestamp'))
            stop = float(item.get('stop_timestamp'))
        except (TypeError, ValueError):
            continue
        if start - 60 <= now < stop:
            title = _decode_title(item.get('title'))
            if title:
                return {'title': title, 'start': start, 'stop': stop}
    return None


def _title_key(value):
    text = re.sub(r'\s*\(\d{4}\)\s*$', '', str(value or '')).lower()
    text = re.sub(r'[^a-z0-9 ]', ' ', text)
    words = [w for w in text.split() if w not in ('the', 'a', 'an')]
    return ' '.join(words)


def titles_clearly_differ(scheduled, shown):
    """True only when two titles are plainly different programmes.

    Provider guides word titles differently ("007: Quantum of Solace", "Untouchables,
    The"), so this errs towards "same": a recording is only stopped when the match is poor.
    """
    a, b = _title_key(scheduled), _title_key(shown)
    if not a or not b:
        return False
    if a in b or b in a:
        return False
    wa, wb = set(a.split()), set(b.split())
    if wa and wb and len(wa & wb) / min(len(wa), len(wb)) >= 0.5:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() < 0.45


class ProgrammeWatchdog:
    """Checks a minute or so into a recording that the stream carries the scheduled programme.

    A wrong stream mapping can deliver another movie that gets saved under the
    scheduled title.  The provider's own guide is asked what is on the stream; the
    recording is stopped only after two lookups in a row clearly disagree.  A missing
    or unreadable provider guide never stops a recording.
    """
    START_AFTER = 75        # seconds after the scheduled start
    RECHECK_AFTER = 30
    MAX_LOOKUPS = 6

    def __init__(self, cfg, job, now=time.time, fetch=None):
        self.cfg, self.job, self.now = cfg, job, now
        self.fetch = fetch or fetch_now_playing
        self.enabled = bool(cfg.get('verify_stream_programme', True))
        self.lookups = 0
        self.mismatches = []
        self.next_check = 0.0
        self.done = False

    def __call__(self):
        if not self.enabled or self.done:
            return None
        now = self.now()
        job = self.job
        if now < float(job['start_ts']) + self.START_AFTER or now >= float(job['stop_ts']) - 300:
            return None
        if now < self.next_check or not job.get('stream_id'):
            return None
        self.next_check = now + self.RECHECK_AFTER
        self.lookups += 1
        playing = self.fetch(self.cfg, job.get('stream_provider') or 'primestreams',
                             job.get('stream_id'), now)
        if playing and titles_clearly_differ(job['title'], playing['title']):
            self.mismatches.append(playing['title'])
            if len(self.mismatches) >= 2 and self.mismatches[-1] == self.mismatches[-2]:
                self.done = True
                return WrongProgramme(
                    f'provider shows "{playing["title"]}" but "{job["title"]}" was scheduled')
        else:
            self.mismatches = []
            if playing:            # confirmed: nothing more to check
                self.done = True
        if self.lookups >= self.MAX_LOOKUPS:
            self.done = True
        return None


def run_process(api, job, status, cmd, cfg, log_path, file_path='', watchdog=None):
    with open(log_path, 'a', encoding='utf-8') as log:
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log, text=True)
        while process.poll() is None:
            try:
                response = api.heartbeat(job['id'], status, file=file_path)
            except Exception as exc:
                # FFmpeg is intentionally independent of Flask. Keep recording
                # through a server restart and renew the lease when it returns.
                print(f'[heartbeat] server unavailable: {exc}', file=sys.stderr, flush=True)
                response = {}
            if response.get('cancel_requested'):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                return None
            if watchdog is not None:
                problem = watchdog()
                if problem:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return problem
            time.sleep(float(cfg['heartbeat_seconds']))
    return process.returncode


PROGRESS_LINE = re.compile(r'time=(\d+):(\d+):(\d+(?:\.\d+)?).*?elapsed=(\d+):(\d+):(\d+(?:\.\d+)?)')
STREAM_ERROR_LINE = re.compile(
    r'(Server returned \d{3}[^,\]]*|HTTP error \d{3}[^,\]]*|Connection (?:reset|refused|timed out|closed)[^,\]]*'
    r'|Operation timed out|Connection timed out|End of file|Input/output error|Immediate exit requested'
    r'|Will reconnect[^\n]*|Error (?:in|while)[^\n]*|Invalid data[^\n]*|Stream ends prematurely[^\n]*)',
    re.IGNORECASE)


def ffmpeg_end_reason(log_path, max_bytes=400000):
    """A short, plain reason why a live capture stopped, from FFmpeg's own log ('' if unknown).

    Looks for the last error FFmpeg printed (a refused or reset connection, a timeout), then for
    pauses where no new video arrived, and says so when the stream simply ended with no error.
    Stream addresses are never included: they hold the provider login.
    """
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            text = handle.read()
    except OSError:
        return ''
    if not text.strip():
        return ''
    last_error = ''
    longest_pause, run_start, last_time, last_elapsed = 0.0, None, None, 0.0
    for line in re.split(r'[\r\n]+', text):
        match = PROGRESS_LINE.search(line)
        if match:
            media = (int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3)))
            elapsed = (int(match.group(4)) * 3600 + int(match.group(5)) * 60 + float(match.group(6)))
            if media != last_time:
                run_start, last_time = elapsed, media
            elif run_start is not None:
                longest_pause = max(longest_pause, elapsed - run_start)
            last_elapsed = elapsed
            continue
        found = STREAM_ERROR_LINE.search(line)
        if found:
            last_error = re.sub(r'\s+', ' ', re.sub(r'\b\w+://\S+', '<address>', found.group(1))).strip(' .')
    pause = f'; video paused up to {longest_pause:.0f} s before that' if longest_pause >= 8 else ''
    if last_error:
        return f'FFmpeg reported "{last_error[:90]}"{pause}'
    return 'the provider ended the stream with no error' + pause


def _log_excerpt(log_path, max_chars=12000):
    """Return the useful tail of FFmpeg's log for the web health report."""
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_chars))
            text = handle.read()
        if size > max_chars:
            text = '[Earlier FFmpeg output omitted]\n' + text
        return text.strip()
    except FileNotFoundError:
        return ''
    except OSError as exc:
        return f'[Unable to read FFmpeg log: {exc}]'


def archive_terminal_result(api, job, status, log_path, **fields):
    """Store a readable log tail in guide.db, then remove the raw log safely."""
    result = dict(fields.pop('result', {}) or {})
    excerpt = _log_excerpt(log_path)
    if excerpt:
        result['log_excerpt'] = excerpt
    response = api.patient_heartbeat(job['id'], status, result=result, **fields)
    # The heartbeat is the durable database write.  Only reclaim disk space
    # after it succeeds; an offline server leaves the original log intact.
    try:
        Path(log_path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f'[cleanup] kept raw FFmpeg log {log_path}: {exc}', file=sys.stderr, flush=True)
    return response


def discard_failed_media(*paths):
    """Health/log data is durable; failed fragments need not consume disk space."""
    for path in paths:
        try:
            Path(path).unlink()
            print(f'[cleanup] removed failed recording fragment {path}', flush=True)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f'[cleanup] kept failed fragment {path}: {exc}', file=sys.stderr, flush=True)


def incomplete_recordings_root(cfg):
    """Dedicated QNAP folder for watchable-but-incomplete recordings."""
    configured = str(cfg.get('incomplete_recordings_path', '') or '').strip()
    if configured:
        return Path(os.path.expanduser(configured))
    return Path(cfg['plex_path']).parent / 'Incomplete Recordings'


DAMAGED_ERROR_LINES = 50


def count_decode_errors(api, job, cfg, media_path):
    """Decode a finished recording from start to end and count FFmpeg's error lines.

    Returns the number of error lines (0 for a clean file), or None if the check could not run
    or the job was cancelled.  A recording that is the right length can still be corrupt where
    the stream stalled, and only decoding it shows that.
    """
    check_log = Path(str(media_path) + '.check.log')
    cmd = [cfg['ffmpeg'], '-v', 'error', '-i', str(media_path), '-f', 'null', '-']
    try:
        rc = run_process(api, job, 'converting', cmd, cfg, check_log, str(media_path))
        if rc is None:
            return None
        lines = [ln for ln in check_log.read_text(encoding='utf-8', errors='replace').splitlines() if ln.strip()]
        return len(lines)
    except OSError as exc:
        print(f'[check] could not check {job["title"]}: {exc}', file=sys.stderr, flush=True)
        return None
    finally:
        try:
            check_log.unlink()
        except OSError:
            pass


DAMAGED_TAIL_SECONDS = 600


def tail_decode_errors(path, cfg, seconds=DAMAGED_TAIL_SECONDS):
    """Decode the last ``seconds`` of a video and count FFmpeg's error lines; None if it cannot run."""
    cmd = [cfg.get('ffmpeg', 'ffmpeg'), '-nostdin', '-v', 'error', '-sseof', f'-{int(seconds)}',
           '-i', str(path), '-f', 'null', '-']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return len([line for line in result.stderr.splitlines() if line.strip()])


def replace_if_existing_is_damaged(should_replace, decision, existing_path, cfg):
    """A damaged Plex copy must never block its own replacement.

    The quality comparison looks at resolution, frame rate and bitrate, so a damaged copy of a
    higher-quality recording can look "better" and make the recorder skip the good one.  When the
    comparison says to keep the existing copy, decode its last ten minutes: with the damage limit
    or more errors it is replaced after all.  Returns (should_replace, decision).
    """
    if should_replace or not existing_path or not cfg.get('check_video_after_recording', True):
        return should_replace, decision
    errors = tail_decode_errors(existing_path, cfg)
    limit = int(cfg.get('damaged_error_lines') or DAMAGED_ERROR_LINES)
    if errors is not None and errors >= limit:
        return True, f'replacing damaged Plex copy ({errors} decode errors in its last 10 minutes)'
    return should_replace, decision


def preserve_incomplete_recording(source, cfg, title, start_ts, progress_callback=None, label='INCOMPLETE'):
    """Copy a partial MP4 to the QNAP, keeping it out of Plex libraries."""
    destination_root = incomplete_recordings_root(cfg)
    # Verify the actual QNAP share, not the destination folder (which may not
    # exist until the first partial recording is saved there).
    if not plex_mount_available(destination_root.parent, cfg.get('plex_mount_marker', '')):
        raise RuntimeError('QNAP recording share is not available')
    stamp = time.strftime('%Y-%m-%d_%H%M', time.localtime(start_ts))
    filename = f'{safe_filename(title)} - {label} - {stamp}.mp4'
    return _verified_copy(source, destination_root / filename, progress_callback)


def plex_mount_available(plex_root, marker=''):
    root = Path(plex_root)
    if not root.is_dir():
        return False
    parts = root.parts
    if len(parts) >= 3 and parts[1] == 'Volumes':
        share_root = Path('/', parts[1], parts[2])
        if not os.path.ismount(share_root):
            return False
    return not marker or (root / marker).exists()


def _verified_copy(source, destination, progress_callback=None):
    """Copy source to destination atomically, verifying its final byte count."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.partial')
    source = Path(source)
    total = source.stat().st_size
    copied = 0
    last_callback = 0.0
    try:
        with open(source, 'rb') as src, open(partial, 'wb') as dst:
            while True:
                chunk = src.read(8 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
                now = time.time()
                if progress_callback and now - last_callback >= 10:
                    progress_callback(copied, total)
                    last_callback = now
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, partial)
        if partial.stat().st_size != total:
            raise RuntimeError('Plex transfer size verification failed')
        os.replace(partial, destination)
    except Exception:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        raise
    return destination


def verified_transfer(source, plex_root, title, existing_path=None, progress_callback=None, year=''):
    title_name, title_year = split_title_year(title)
    year = year or title_year
    if existing_path and Path(existing_path).suffix.lower() == '.mp4':
        destination = Path(existing_path)
    else:
        folder = safe_filename(f'{title_name} ({year})' if year else title_name)
        destination = Path(plex_root) / folder / f'{safe_filename(title_name)}.mp4'
    return _verified_copy(source, destination, progress_callback)


def verified_episode_transfer(source, tv_root, show_title, season, episode,
                              episode_title='', existing_path=None,
                              progress_callback=None, year=''):
    if existing_path and Path(existing_path).suffix.lower() == '.mp4':
        destination = Path(existing_path)
    else:
        show = safe_filename(split_title_year(show_title)[0])
        episode_suffix = f' - {safe_filename(episode_title)}' if episode_title else ''
        filename = f'{show} - S{season:02d}E{episode:02d}{episode_suffix}.mp4'
        destination = (resolve_show_dir(tv_root, show_title, year) /
                       f'Season {season:02d}' / filename)
    return _verified_copy(source, destination, progress_callback)


def needs_year_root(cfg):
    """Holding folder, outside the Movies library, for a film whose year is unknown."""
    configured = str(cfg.get('needs_year_path', '') or '').strip()
    if configured:
        return Path(os.path.expanduser(configured))
    return Path(cfg['plex_path']).parent / 'Movies Needing Year'


def verified_unyeared_movie_transfer(source, cfg, title, start_ts, progress_callback=None):
    """File a film whose year could not be found next to, not inside, the library.

    Every film in Plex has a year in its folder name.  Without one it cannot be told
    from a remake with the same title, so it waits here until the year is known.
    """
    name = safe_filename(split_title_year(title)[0])
    aired = time.strftime('%Y-%m-%d_%H%M', time.localtime(start_ts))
    return _verified_copy(source, needs_year_root(cfg) / f'{name} - {aired}.mp4',
                          progress_callback)


def verified_unidentified_episode_transfer(source, tv_root, show_title, start_ts,
                                           progress_callback=None, year=''):
    """Keep an identified-as-TV airing out of Movies until S/E data is available."""
    base, title_year = split_title_year(show_title)
    year = str(year or '').strip()[:4] or title_year
    show = safe_filename(base)
    folder = safe_filename(f'{base} ({year})' if year else base)
    aired = time.strftime('%Y-%m-%d_%H%M', time.localtime(start_ts))
    destination = (Path(tv_root) / '_Needs Episode Info' / folder /
                   f'{show} - {aired}.mp4')
    return _verified_copy(source, destination, progress_callback)


RESUME_PAUSE_SECONDS = 20
RESUME_MAX_ATTEMPTS = 20
RESUME_MAX_EMPTY_SEGMENTS = 5


def segment_path(ts_path, attempt):
    """Path of the n-th recording segment; the first is the final file itself.

    The name keeps a .ts extension: FFmpeg refuses an output whose format it
    cannot work out from the name (e.g. "show.ts.part1").
    """
    ts_path = Path(ts_path)
    if attempt == 0:
        return ts_path
    return ts_path.with_name(f'{ts_path.stem}.part{attempt}{ts_path.suffix or ".ts"}')


def build_record_command(cfg, url, remaining, segment):
    return [
        cfg['ffmpeg'], '-y',
        '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_at_eof', '1',
        '-reconnect_delay_max', '30', '-rw_timeout', '15000000',
        '-i', url, '-t', str(remaining), '-c', 'copy', '-f', 'mpegts', str(segment)
    ]


def record_with_resume(api, job, cfg, url, ts_path, log_path, end_time):
    """Record a live stream to ``ts_path``, resuming if the provider cuts it early.

    FFmpeg's own reconnect gives up after about half a minute, and providers
    often refuse a new connection (HTTP 403) while the old one is still counted.
    When FFmpeg stops before ``end_time``, wait, start it again on a new segment
    and, at the end, append the segments to ``ts_path`` (MPEG-TS joins by
    concatenation).  Returns FFmpeg's last exit code, or None if cancelled.
    """
    ts_path = Path(ts_path)
    segments = []
    empty_in_a_row = 0
    attempt = 0
    rc = 0
    watchdog = ProgrammeWatchdog(cfg, job)
    while True:
        segment = segment_path(ts_path, attempt)
        remaining = int(end_time - time.time())
        record_cmd = build_record_command(cfg, url, remaining, segment)
        rc = run_process(api, job, 'recording', record_cmd, cfg, log_path, str(ts_path),
                         watchdog=watchdog)
        if isinstance(rc, WrongProgramme):
            for extra in segments[1:] + [segment]:
                if extra != ts_path:
                    discard_failed_media(extra)
            return rc
        if rc is None:
            for extra in segments[1:] + [segment]:
                if extra != ts_path:
                    discard_failed_media(extra)
            return None
        segments.append(segment)
        size = segment.stat().st_size if segment.exists() else 0
        empty_in_a_row = empty_in_a_row + 1 if size == 0 else 0
        if end_time - time.time() < 90:
            break
        attempt += 1
        if attempt > RESUME_MAX_ATTEMPTS or empty_in_a_row >= RESUME_MAX_EMPTY_SEGMENTS:
            break
        print(f'[resume] {job["title"]}: stream stopped early; retrying in '
              f'{RESUME_PAUSE_SECONDS}s (attempt {attempt})', flush=True)
        if not heartbeat_sleep(api, job, 'recording', RESUME_PAUSE_SECONDS, cfg,
                               file=str(ts_path)):
            for extra in segments[1:]:
                discard_failed_media(extra)
            return None
    # Join the extra segments onto the first one.
    for extra in segments[1:]:
        if extra.exists() and extra.stat().st_size:
            with open(ts_path, 'ab') as out, open(extra, 'rb') as src:
                shutil.copyfileobj(src, out)
        try:
            extra.unlink()
        except FileNotFoundError:
            pass
    if rc != 0 and ts_path.exists() and ts_path.stat().st_size:
        # We kept what was captured; the incomplete-recording check decides next.
        rc = 0
    return rc


PROBE_ATTEMPTS = 3
PROBE_PAUSE_SECONDS = 20


def probe_incoming_stream(api, job, cfg, url, sleep=None):
    """Probe the incoming stream, trying again a couple of times if it is refused.

    A provider can refuse a new connection for a few seconds while a finished one is still
    counted, so one refusal must not fail the recording.  Returns the probe, None if the job
    was cancelled while waiting, or raises the last error.
    """
    sleep = sleep or heartbeat_sleep
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        try:
            return probe_media(url, ffprobe=cfg['ffprobe'], timeout=45)
        except Exception as exc:
            if attempt == PROBE_ATTEMPTS:
                raise RuntimeError(f'{exc} (tried {PROBE_ATTEMPTS} times)')
            print(f'[probe] {job["title"]}: {exc}; trying again in {PROBE_PAUSE_SECONDS}s '
                  f'(attempt {attempt} of {PROBE_ATTEMPTS})', flush=True)
            if not sleep(api, job, 'preflight', PROBE_PAUSE_SECONDS, cfg):
                return None


def process_job(api, job, cfg):
    local_dir = Path(os.path.expanduser(cfg['local_recordings']))
    local_dir.mkdir(parents=True, exist_ok=True)
    title_slug = safe_filename(job['title']).replace(' ', '_')
    ts_path = local_dir / f'{title_slug}_{int(job["start_ts"])}.ts'
    mp4_path = ts_path.with_suffix('.mp4')
    log_path = local_dir / f'{title_slug}_{int(job["start_ts"])}.ffmpeg.log'
    url = stream_url(cfg, job['stream_id'], job.get('stream_provider', 'primestreams'),
                     job.get('stream_extension', 'ts'))
    episode = episode_metadata(job)
    is_series = bool(job.get('is_series'))
    # A film recorded privately goes to the private folder, never into the Plex library.
    private_film = bool(job.get('private')) and not (episode or is_series)
    library_root = (cfg['plex_tv_path'] if (episode or is_series) else
                    str(ensure_private_root(cfg)) if private_film else cfg['plex_path'])

    api.patient_heartbeat(job['id'], 'preflight', message='Checking Plex and incoming stream quality')
    candidates = (find_tv_episode_candidates(
        library_root, job['title'], episode['season'], episode['episode'], job.get('year', ''))
        if episode else ([] if is_series else find_movie_candidates(cfg, library_root, job['title'], job.get('year', ''))))
    existing_path, existing_probe = best_existing_copy(candidates, cfg['ffprobe'])
    try:
        incoming_probe = probe_incoming_stream(api, job, cfg, url)
    except Exception as exc:
        api.patient_heartbeat(job['id'], 'failed', message=f'Incoming stream probe failed: {exc}')
        return
    if incoming_probe is None:        # cancelled while waiting to try again
        return
    api.note_stream_info(job['id'], incoming_probe)

    incomplete_copy = bool(existing_probe and incomplete_for_scheduled_window(
        existing_probe, job['start_ts'], job['stop_ts']
    ))
    if incomplete_copy:
        should_record, decision = True, (
            f'replacing incomplete Plex copy ({existing_probe["duration"] / 60:.0f} min '
            f'of {((job["stop_ts"] - job["start_ts"]) / 60):.0f} min scheduled)'
        )
    elif existing_probe:
        should_record, decision = quality_decision(existing_probe, incoming_probe)
        should_record, decision = replace_if_existing_is_damaged(should_record, decision, existing_path, cfg)
    else:
        should_record, decision = True, 'recording because Plex has no copy'
    quality = {
        'decision': decision, 'incoming': incoming_probe,
        'existing': existing_probe, 'existing_path': str(existing_path or ''),
    }
    if not should_record:
        api.patient_heartbeat(job['id'], 'skipped_existing_better',
                      quality_decision=decision, result=quality)
        print(f'[skip] {job["title"]}: {decision}', flush=True)
        return

    wait = job['start_ts'] - time.time() - 5
    if wait > 0 and not heartbeat_sleep(
            api, job, 'waiting', wait, cfg, quality_decision=decision):
        return
    remaining = int(job['stop_ts'] - time.time()) + cfg['recording_tail_seconds']
    if remaining < 60:
        api.patient_heartbeat(job['id'], 'skipped_too_short',
                      message='Recording window has already passed', result=quality)
        return

    # IPTV providers close long-lived connections; resume so a dropped stream
    # continues instead of ending the recording early.
    rc = record_with_resume(api, job, cfg, url, ts_path, log_path,
                            time.time() + remaining)
    quality['record_log_tail'] = _log_excerpt(log_path, 3000)
    if isinstance(rc, WrongProgramme):
        message = f'Wrong programme on stream: {rc}'
        quality['wrong_programme'] = str(rc)
        archive_terminal_result(api, job, 'failed', log_path, message=message,
                                file=str(ts_path), result=quality)
        discard_failed_media(ts_path, mp4_path)
        print(f'[wrong-programme] {job["title"]}: {message}', flush=True)
        return
    if rc is None:
        return
    if rc != 0:
        archive_terminal_result(api, job, 'failed', log_path,
                                message='FFmpeg recording failed',
                                file=str(ts_path), result=quality)
        discard_failed_media(ts_path, mp4_path)
        return

    convert_cmd = [
        cfg['ffmpeg'], '-y', '-i', str(ts_path), '-c:v', 'copy', '-c:a', 'aac', str(mp4_path)
    ]
    rc = run_process(api, job, 'converting', convert_cmd, cfg, log_path, str(ts_path))
    if rc is None:
        return
    if rc != 0:
        archive_terminal_result(api, job, 'done_ts', log_path,
                                message='Conversion failed; kept TS file',
                                file=str(ts_path), quality_decision=decision,
                                result=quality)
        discard_failed_media(ts_path, mp4_path)
        return

    transfer_deadline = time.time() + float(cfg['transfer_wait_timeout'])
    while not plex_mount_available(library_root, cfg.get('plex_mount_marker', '')):
        if time.time() >= transfer_deadline:
            archive_terminal_result(api, job, 'failed', log_path,
                                    message='Timed out waiting for Plex mount',
                                    file=str(mp4_path), result=quality)
            discard_failed_media(ts_path, mp4_path)
            return
        if not heartbeat_sleep(api, job, 'awaiting_transfer',
                               float(cfg['transfer_retry_seconds']), cfg,
                               file=str(mp4_path), quality_decision=decision):
            return

    # Recheck the library after recording. The share may have been unavailable
    # during preflight, or Plex may have received a better copy in the meantime.
    final_candidates = (find_tv_episode_candidates(
        library_root, job['title'], episode['season'], episode['episode'], job.get('year', ''))
        if episode else ([] if is_series else find_movie_candidates(cfg, library_root, job['title'], job.get('year', ''))))
    final_existing_path, final_existing_probe = best_existing_copy(
        final_candidates, cfg['ffprobe']
    )
    recorded_probe = probe_media(mp4_path, ffprobe=cfg['ffprobe'])
    # FFmpeg can exit successfully when a live HTTP source simply stops
    # sending data. Do not let a short fragment replace the Plex copy.
    missing_seconds = recording_missing_seconds(recorded_probe, job['start_ts'], job['stop_ts'])
    if missing_seconds or incomplete_for_scheduled_window(
            recorded_probe, job['start_ts'], job['stop_ts']):
        expected_minutes = (job['stop_ts'] - job['start_ts']) / 60
        actual_minutes = recorded_probe['duration'] / 60
        message = (f'Recording ended early: {actual_minutes:.0f} min captured '
                   f'of {expected_minutes:.0f} min scheduled')
        if missing_seconds:
            message += f' (about {missing_seconds / 60:.0f} min missing)'
        end_reason = ffmpeg_end_reason(log_path)
        if end_reason:
            message += f'; why: {end_reason}'
            quality['end_reason'] = end_reason
        quality.update({'decision': 'incomplete recording', 'recorded': recorded_probe})
        incomplete_path = None
        try:
            def incomplete_progress(copied, total):
                try:
                    api.heartbeat(job['id'], 'transferring', file=str(mp4_path),
                                  progress=round(copied / total * 100, 1) if total else 0,
                                  quality_decision='saving incomplete recording to QNAP')
                except Exception as exc:    # a progress report must never abort the copy
                    print(f'[heartbeat] server unavailable: {exc}', file=sys.stderr, flush=True)
            incomplete_path = preserve_incomplete_recording(
                mp4_path, cfg, job['title'], job['start_ts'], incomplete_progress
            )
            quality['incomplete_path'] = str(incomplete_path)
            message += f'; saved to QNAP: {incomplete_path.name}'
        except Exception as exc:
            # A failed QNAP transfer must never turn a partial recording into
            # a deletion.  Leave the local MP4 in place for manual recovery.
            quality['incomplete_save_error'] = str(exc)
            message += '; kept on Mac because QNAP save failed'
        archive_terminal_result(api, job, 'failed', log_path, message=message,
                                file=str(incomplete_path or mp4_path),
                                quality_decision='incomplete recording', result=quality)
        if incomplete_path:
            discard_failed_media(ts_path, mp4_path)
        print(f'[incomplete] {job["title"]}: {message}', flush=True)
        return
    # The right length is not enough: decode the file to see whether the video is intact.
    if cfg.get('check_video_after_recording', True):
        api.patient_heartbeat(job['id'], 'converting', file=str(mp4_path),
                              quality_decision='checking the video for damage')
        damage = count_decode_errors(api, job, cfg, mp4_path)
        if damage is not None:
            quality['decode_errors'] = damage
            limit = int(cfg.get('damaged_error_lines') or DAMAGED_ERROR_LINES)
            if damage >= limit:
                message = (f'Video is damaged: {damage} decode errors (the limit is {limit}); '
                           'not added to Plex')
                try:
                    saved = preserve_incomplete_recording(mp4_path, cfg, job['title'], job['start_ts'],
                                                          label='DAMAGED')
                    quality['incomplete_path'] = str(saved)
                    message += f'; saved to QNAP: {saved.name}'
                except Exception as exc:
                    quality['incomplete_save_error'] = str(exc)
                    message += '; kept on Mac because QNAP save failed'
                    saved = None
                archive_terminal_result(api, job, 'failed', log_path, message=message,
                                        file=str(saved or mp4_path), quality_decision='damaged video',
                                        result=quality)
                if saved:
                    discard_failed_media(ts_path, mp4_path)
                print(f'[damaged] {job["title"]}: {message}', flush=True)
                return
            if damage:
                decision = f'{decision}; {damage} minor video errors'
    final_incomplete_copy = bool(final_existing_probe and incomplete_for_scheduled_window(
        final_existing_probe, job['start_ts'], job['stop_ts']
    ))
    if final_incomplete_copy:
        should_transfer, final_decision = True, (
            f'replacing incomplete Plex copy ({final_existing_probe["duration"] / 60:.0f} min)'
        )
        quality.update({
            'decision': final_decision,
            'existing': final_existing_probe,
            'existing_path': str(final_existing_path or ''),
            'recorded': recorded_probe,
        })
        decision = final_decision
        existing_path = final_existing_path
    elif final_existing_probe:
        should_transfer, final_decision = quality_decision(
            final_existing_probe, recorded_probe
        )
        should_transfer, final_decision = replace_if_existing_is_damaged(
            should_transfer, final_decision, final_existing_path, cfg
        )
        quality.update({
            'decision': final_decision,
            'existing': final_existing_probe,
            'existing_path': str(final_existing_path or ''),
            'recorded': recorded_probe,
        })
        if not should_transfer:
            archive_terminal_result(
                api, job, 'skipped_existing_better', log_path, file=str(mp4_path),
                quality_decision=final_decision, result=quality,
            )
            discard_failed_media(ts_path, mp4_path)
            print(f'[skip] {job["title"]}: {final_decision}', flush=True)
            return
        decision = final_decision
        existing_path = final_existing_path

    api.patient_heartbeat(job['id'], 'transferring', file=str(mp4_path),
                          quality_decision=decision)
    def transfer_progress(copied, total):
        try:
            response = api.heartbeat(
                job['id'], 'transferring', file=str(mp4_path),
                progress=round(copied / total * 100, 1) if total else 0,
                quality_decision=decision,
            )
        except Exception as exc:
            print(f'[heartbeat] server unavailable: {exc}', file=sys.stderr, flush=True)
            response = {}
        if response.get('cancel_requested'):
            raise RuntimeError('Transfer cancelled by user')

    if episode:
        destination = verified_episode_transfer(
            mp4_path, library_root, job['title'], episode['season'],
            episode['episode'], episode['title'], existing_path,
            progress_callback=transfer_progress, year=job.get('year', ''),
        )
    elif is_series:
        destination = verified_unidentified_episode_transfer(
            mp4_path, library_root, job['title'], job['start_ts'],
            progress_callback=transfer_progress, year=job.get('year', ''),
        )
    else:
        # The year chosen when the recording was set up beats a title lookup, which
        # cannot tell two films of the same name apart.
        movie_year = str(job.get('year') or '').strip()[:4]
        if not movie_year:
            try:
                movie_year = api.movie_year(job['title'])
            except Exception as exc:
                print(f'[metadata] no year for {job["title"]}: {exc}', file=sys.stderr, flush=True)
                movie_year = ''
        if movie_year or existing_path:
            destination = verified_transfer(
                mp4_path, library_root, job['title'], existing_path,
                progress_callback=transfer_progress, year=movie_year,
            )
        else:
            destination = verified_unyeared_movie_transfer(
                mp4_path, cfg, job['title'], job['start_ts'],
                progress_callback=transfer_progress,
            )
            print(f'[needs-year] {job["title"]}: no year found; saved to {destination}', flush=True)
    # The Plex file was copied atomically and byte-verified above.  Keep local
    # recordings only when a recording/transfer failed; successful transfers
    # should not silently fill the Mac's recording disk.
    archive_terminal_result(
        api, job, 'done', log_path, file=str(mp4_path), quality_decision=decision,
        result={**quality, 'plex_path': str(destination),
                'content_type': ('episode' if episode else
                                 'episode_needs_metadata' if is_series else
                                 'movie' if (movie_year or existing_path) else 'movie_needs_year')},
    )
    try:
        mp4_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        # Plex is already safe; retain the local copy rather than turn a good
        # completed recording into a failed job because cleanup was blocked.
        print(f'[cleanup] kept local MP4 {mp4_path}: {exc}', file=sys.stderr, flush=True)
    try:
        ts_path.unlink()
    except FileNotFoundError:
        pass
    print(f'[done] {job["title"]} → {destination}', flush=True)


def _run_claimed_job(api, job, cfg):
    """Run one independent recording lifecycle in its own worker thread."""
    try:
        process_job(api, job, cfg)
    except Exception as exc:
        try:
            api.heartbeat(job['id'], 'failed', message=str(exc))
        except Exception:
            pass
        print(f'[error] {job["title"]}: {exc}', file=sys.stderr, flush=True)


def start_job_once(api, job, cfg, active, thread_factory=threading.Thread):
    """Start a worker for a claimed job unless this recorder is already running it.

    The server can offer a job again when it could not record the recorder's heartbeats
    (for example while its database was locked) and the lease ran out.  Starting a second
    worker for the same job made two FFmpeg processes write the same file and fight over
    the provider's connection.  Returns True if a worker was started.
    """
    job_id = job['id']
    existing = active.get(job_id)
    if existing is not None and existing.is_alive():
        print(f'[claim] {job["title"]} ({job_id}) is already recording here; '
              'ignoring the repeat claim', flush=True)
        return False
    print(f'[claim] {job["title"]} ({job_id})', flush=True)
    worker = thread_factory(target=_run_claimed_job, args=(api, job, cfg), daemon=True,
                            name=f'epg-record-{job_id}')
    active[job_id] = worker
    worker.start()
    return True


def run_agent(cfg, once=False):
    api = AgentAPI(cfg)
    health = api.health()
    max_workers = cfg['max_concurrent_recordings']
    print(f'[agent] connected; version {AGENT_VERSION}; server backend={health.get("recording_backend")}; '
          f'max recordings={max_workers}', flush=True)
    active = {}
    while True:
        try:
            # Drop completed workers, then fill every available recording slot.
            active = {job_id: worker for job_id, worker in active.items()
                      if worker.is_alive()}
            while len(active) < max_workers:
                job = api.claim(cfg['claim_ahead_seconds'])
                if not job:
                    break
                start_job_once(api, job, cfg, active)
            if once:
                for worker in list(active.values()):
                    worker.join()
                return
        except Exception as exc:
            print(f'[agent] poll error: {exc}', file=sys.stderr, flush=True)
            if once:
                raise
        time.sleep(float(cfg['poll_seconds']))


def main():
    parser = argparse.ArgumentParser(description='EPG Manager Mac recording agent')
    parser.add_argument('--config', default=os.path.expanduser('~/EPG-Manager/recording_agent.json'))
    parser.add_argument('--once', action='store_true', help='Poll once and exit when no job is ready')
    args = parser.parse_args()
    run_agent(load_config(args.config), once=args.once)


if __name__ == '__main__':
    main()
