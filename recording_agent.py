#!/usr/bin/env python3
"""Mac recording agent for EPG Manager.

The agent polls an EPG Manager server for recording jobs, records locally with
FFmpeg, and transfers verified MP4 files to a mounted Plex library.
"""

import argparse
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
    cfg.setdefault('ffmpeg', 'ffmpeg')
    cfg.setdefault('ffprobe', 'ffprobe')
    cfg.setdefault('transfer_retry_seconds', 30)
    cfg.setdefault('transfer_wait_timeout', 86400)
    try:
        cfg['max_concurrent_recordings'] = max(
            1, min(int(cfg['max_concurrent_recordings']), 16)
        )
    except (TypeError, ValueError):
        cfg['max_concurrent_recordings'] = 6
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
            'lease_seconds': self.lease_seconds,
            'claim_ahead_seconds': int(claim_ahead_seconds),
        }).get('job')

    def heartbeat(self, job_id, status, **fields):
        payload = {
            'agent_id': self.agent_id,
            'lease_seconds': self.lease_seconds,
            'status': status,
            **fields,
        }
        return self._request(
            'POST', f'/epg-web/api/agent/jobs/{job_id}/heartbeat', payload
        )

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


def normalized_title(title):
    base, _year = split_title_year(title)
    return re.sub(r'[^a-z0-9]', '', base.lower())


def parse_rate(value):
    try:
        if '/' in str(value):
            numerator, denominator = str(value).split('/', 1)
            return float(numerator) / float(denominator) if float(denominator) else 0.0
        return float(value or 0)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_media(target, ffprobe='ffprobe', timeout=30):
    cmd = [
        ffprobe, '-v', 'error', '-show_entries',
        'format=duration,size,bit_rate:'
        'stream=codec_type,codec_name,profile,width,height,avg_frame_rate,'
        'r_frame_rate,bit_rate,sample_rate,channels',
        '-of', 'json', str(target),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        if str(target).startswith(('http://', 'https://')):
            raise RuntimeError('ffprobe failed for incoming stream')
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


def find_plex_candidates(plex_root, title):
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
        if entry.is_dir() and normalized_title(entry.name) == wanted:
            candidates.extend(
                path for path in entry.iterdir()
                if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
            )
        elif (entry.is_file() and entry.suffix.lower() in MEDIA_EXTENSIONS and
              normalized_title(entry.stem) == wanted):
            candidates.append(entry)
    return candidates


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


def stream_url(cfg, stream_id):
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


def run_process(api, job, status, cmd, cfg, log_path, file_path=''):
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
            time.sleep(float(cfg['heartbeat_seconds']))
    return process.returncode


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


def verified_transfer(source, plex_root, title, existing_path=None, progress_callback=None, year=''):
    title_name, title_year = split_title_year(title)
    year = year or title_year
    if existing_path and Path(existing_path).suffix.lower() == '.mp4':
        destination = Path(existing_path)
    else:
        folder = safe_filename(f'{title_name} ({year})' if year else title_name)
        destination = Path(plex_root) / folder / f'{safe_filename(title_name)}.mp4'
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


def process_job(api, job, cfg):
    local_dir = Path(os.path.expanduser(cfg['local_recordings']))
    local_dir.mkdir(parents=True, exist_ok=True)
    title_slug = safe_filename(job['title']).replace(' ', '_')
    ts_path = local_dir / f'{title_slug}_{int(job["start_ts"])}.ts'
    mp4_path = ts_path.with_suffix('.mp4')
    log_path = local_dir / f'{title_slug}_{int(job["start_ts"])}.ffmpeg.log'
    url = stream_url(cfg, job['stream_id'])

    api.heartbeat(job['id'], 'preflight', message='Checking Plex and incoming stream quality')
    candidates = find_plex_candidates(cfg['plex_path'], job['title'])
    existing_path, existing_probe = best_existing_copy(candidates, cfg['ffprobe'])
    try:
        incoming_probe = probe_media(url, ffprobe=cfg['ffprobe'], timeout=45)
    except Exception as exc:
        api.heartbeat(job['id'], 'failed', message=f'Incoming stream probe failed: {exc}')
        return

    if existing_probe:
        should_record, decision = quality_decision(existing_probe, incoming_probe)
    else:
        should_record, decision = True, 'recording because Plex has no copy'
    quality = {
        'decision': decision, 'incoming': incoming_probe,
        'existing': existing_probe, 'existing_path': str(existing_path or ''),
    }
    if not should_record:
        api.heartbeat(job['id'], 'skipped_existing_better',
                      quality_decision=decision, result=quality)
        print(f'[skip] {job["title"]}: {decision}', flush=True)
        return

    wait = job['start_ts'] - time.time() - 5
    if wait > 0 and not heartbeat_sleep(
            api, job, 'waiting', wait, cfg, quality_decision=decision):
        return
    remaining = int(job['stop_ts'] - time.time()) + 30
    if remaining < 60:
        api.heartbeat(job['id'], 'skipped_too_short',
                      message='Recording window has already passed', result=quality)
        return

    record_cmd = [
        cfg['ffmpeg'], '-y', '-i', url, '-t', str(remaining), '-c', 'copy', str(ts_path)
    ]
    rc = run_process(api, job, 'recording', record_cmd, cfg, log_path, str(ts_path))
    if rc is None:
        return
    if rc != 0:
        api.heartbeat(job['id'], 'failed', message='FFmpeg recording failed',
                      file=str(ts_path), result=quality)
        return

    convert_cmd = [
        cfg['ffmpeg'], '-y', '-i', str(ts_path), '-c:v', 'copy', '-c:a', 'aac', str(mp4_path)
    ]
    rc = run_process(api, job, 'converting', convert_cmd, cfg, log_path, str(ts_path))
    if rc is None:
        return
    if rc != 0:
        api.heartbeat(job['id'], 'done_ts', message='Conversion failed; kept TS file',
                      file=str(ts_path), quality_decision=decision, result=quality)
        return

    transfer_deadline = time.time() + float(cfg['transfer_wait_timeout'])
    while not plex_mount_available(cfg['plex_path'], cfg.get('plex_mount_marker', '')):
        if time.time() >= transfer_deadline:
            api.heartbeat(job['id'], 'failed', message='Timed out waiting for Plex mount',
                          file=str(mp4_path), result=quality)
            return
        if not heartbeat_sleep(api, job, 'awaiting_transfer',
                               float(cfg['transfer_retry_seconds']), cfg,
                               file=str(mp4_path), quality_decision=decision):
            return

    # Recheck the library after recording. The share may have been unavailable
    # during preflight, or Plex may have received a better copy in the meantime.
    final_candidates = find_plex_candidates(cfg['plex_path'], job['title'])
    final_existing_path, final_existing_probe = best_existing_copy(
        final_candidates, cfg['ffprobe']
    )
    recorded_probe = probe_media(mp4_path, ffprobe=cfg['ffprobe'])
    if final_existing_probe:
        should_transfer, final_decision = quality_decision(
            final_existing_probe, recorded_probe
        )
        quality.update({
            'decision': final_decision,
            'existing': final_existing_probe,
            'existing_path': str(final_existing_path or ''),
            'recorded': recorded_probe,
        })
        if not should_transfer:
            api.heartbeat(
                job['id'], 'skipped_existing_better', file=str(mp4_path),
                quality_decision=final_decision, result=quality,
            )
            print(f'[skip] {job["title"]}: {final_decision}', flush=True)
            return
        decision = final_decision
        existing_path = final_existing_path

    api.heartbeat(job['id'], 'transferring', file=str(mp4_path),
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

    try:
        movie_year = api.movie_year(job['title'])
    except Exception as exc:
        print(f'[metadata] no year for {job["title"]}: {exc}', file=sys.stderr, flush=True)
        movie_year = ''
    destination = verified_transfer(
        mp4_path, cfg['plex_path'], job['title'], existing_path,
        progress_callback=transfer_progress, year=movie_year,
    )
    api.heartbeat(job['id'], 'done', file=str(mp4_path), quality_decision=decision,
                  result={**quality, 'plex_path': str(destination)})
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


def run_agent(cfg, once=False):
    api = AgentAPI(cfg)
    health = api.health()
    max_workers = cfg['max_concurrent_recordings']
    print(f'[agent] connected; server backend={health.get("recording_backend")}; '
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
                job_id = job['id']
                print(f'[claim] {job["title"]} ({job_id})', flush=True)
                worker = threading.Thread(
                    target=_run_claimed_job, args=(api, job, cfg), daemon=True,
                    name=f'epg-record-{job_id}',
                )
                active[job_id] = worker
                worker.start()
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
