#!/usr/bin/env python3
"""EPG Manager Play Agent — launches VLC locally on behalf of the QNAP server."""
import json, os, subprocess, threading, time
from flask import Flask, jsonify, request

VERSION = 'v20261106b'

app = Flask(__name__)

_vlc_lock    = threading.Lock()
_vlc_streams = {}   # channel_id -> {'pid': int, 'ch_name': str, 'title': str}
MAX_STREAMS  = 4

def _proc_dead(pid):
    try:
        os.kill(pid, 0)
        return False
    except (ProcessLookupError, PermissionError):
        return True

def _start_vlc(channel_id, url, title='', ch_label='', ch_name=''):
    import signal as _sig
    with _vlc_lock:
        dead = [cid for cid, v in _vlc_streams.items() if _proc_dead(v['pid'])]
        for cid in dead:
            del _vlc_streams[cid]
        if channel_id in _vlc_streams:
            try:
                os.kill(_vlc_streams[channel_id]['pid'], _sig.SIGTERM)
            except Exception:
                pass
            del _vlc_streams[channel_id]
        if len(_vlc_streams) >= MAX_STREAMS:
            return None, f'Max {MAX_STREAMS} streams already playing'
        try:
            vlc_paths = [
                '/Applications/VLC.app/Contents/MacOS/VLC',
                '/usr/bin/vlc',
                'vlc',
            ]
            vlc_exe = next((p for p in vlc_paths if os.path.exists(p)), 'vlc')
            parts = [p for p in [title, ch_label] if p]
            display_title = '  —  '.join(parts) if parts else channel_id
            cmd = [vlc_exe, url,
                   '--network-caching', '1500',
                   '--live-caching', '1500',
                   '--clock-jitter', '0',
                   '--clock-synchro', '0',
                   '--meta-title', display_title,
                   '--video-title', display_title,
                   '--video-title-timeout', '5000']
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _vlc_streams[channel_id] = {'pid': proc.pid, 'ch_name': ch_name, 'title': title}
        except Exception as exc:
            return None, str(exc)
    return dict(_vlc_streams), None

def _streams_payload(snapshot):
    return {'ok': True, 'streams': [
        {'channel_id': cid, 'ch_name': v['ch_name'], 'title': v['title']}
        for cid, v in snapshot.items()
    ]}

@app.route('/play', methods=['POST'])
def api_play():
    data       = request.json or {}
    channel_id = data.get('channel_id', '')
    url        = data.get('url', '')
    title      = data.get('title', '')
    ch_label   = data.get('ch_label', '')
    ch_name    = data.get('ch_name', channel_id)
    if not url:
        return jsonify({'error': 'url required'}), 400
    snapshot, err = _start_vlc(channel_id, url, title, ch_label, ch_name)
    if err:
        return jsonify({'error': err}), 500
    return jsonify(_streams_payload(snapshot))

@app.route('/play-file', methods=['POST'])
def api_play_file():
    data  = request.json or {}
    fpath = data.get('file', '')
    title = data.get('title', '')
    if not fpath:
        return jsonify({'error': 'file required'}), 400
    if not os.path.exists(fpath):
        return jsonify({'error': f'file not found: {fpath}'}), 404
    try:
        vlc_paths = [
            '/Applications/VLC.app/Contents/MacOS/VLC',
            '/usr/bin/vlc',
            'vlc',
        ]
        vlc_exe = next((p for p in vlc_paths if os.path.exists(p)), 'vlc')
        cmd = [vlc_exe, fpath]
        if title:
            cmd += ['--meta-title', title, '--video-title', title, '--video-title-timeout', '5000']
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'ok': True, 'file': fpath})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

@app.route('/stop', methods=['POST'])
def api_stop():
    import signal as _sig
    data       = request.json or {}
    channel_id = data.get('channel_id', '')
    with _vlc_lock:
        dead = [cid for cid, v in _vlc_streams.items() if _proc_dead(v['pid'])]
        for cid in dead:
            del _vlc_streams[cid]
        if channel_id and channel_id in _vlc_streams:
            try:
                os.kill(_vlc_streams[channel_id]['pid'], _sig.SIGTERM)
            except Exception:
                pass
            del _vlc_streams[channel_id]
        snapshot = dict(_vlc_streams)
    return jsonify(_streams_payload(snapshot))

@app.route('/status')
def api_status():
    with _vlc_lock:
        dead = [cid for cid, v in _vlc_streams.items() if _proc_dead(v['pid'])]
        for cid in dead:
            del _vlc_streams[cid]
        snapshot = dict(_vlc_streams)
    return jsonify(_streams_payload(snapshot))

@app.route('/health')
def api_health():
    return jsonify({'ok': True, 'version': VERSION})

if __name__ == '__main__':
    print(f'[play-agent] {VERSION} starting on port 5002', flush=True)
    app.run(host='0.0.0.0', port=5002, debug=False, threaded=True)
