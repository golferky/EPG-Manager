#!/usr/bin/env python3
"""EPG Manager Web — Guide · Recommendations · Channels · Schedule · Conversions"""
VERSION = "v20260718"

import json, os, re, subprocess, threading, time, uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
app.secret_key = os.urandom(24)

BASE_DIR         = os.path.expanduser('~/epg')
CONFIG_FILE      = os.path.join(BASE_DIR, 'epg_config.json')
SCHEDULE_FILE    = os.path.join(BASE_DIR, 'epg_schedule.json')
WATCHLIST_FILE   = os.path.join(BASE_DIR, 'epg_watchlist.json')

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        'guide_path':  '/Volumes/EPG/guide.xml',
        'timezone':    'America/New_York',
        'ts_input':    os.path.expanduser('~/Movies'),
        'ts_output':   os.path.expanduser('~/Movies/Converted'),
    }

def save_config(cfg):
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

# ── Schedule ──────────────────────────────────────────────────────────────────

def load_schedule():
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE) as f:
            return json.load(f)
    return []

def save_schedule(s):
    with open(SCHEDULE_FILE, 'w') as f:
        json.dump(s, f, indent=2)

# ── Watchlist ─────────────────────────────────────────────────────────────────

def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    return []

def save_watchlist(wl):
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump(wl, f, indent=2)

# ── EPG Parsing ───────────────────────────────────────────────────────────────

_epg = {'channels': [], 'channel_map': {}, 'programmes': [], 'loaded': None}

def _parse_dt(s):
    s = s.strip()
    tz = timezone.utc
    if ' ' in s:
        dt_str, tz_str = s.split(' ', 1)
        sign = 1 if tz_str[0] == '+' else -1
        tz_h, tz_m = int(tz_str[1:3]), int(tz_str[3:5])
        tz = timezone(timedelta(hours=tz_h, minutes=tz_m) * sign)
    else:
        dt_str = s
    return datetime.strptime(dt_str[:14], '%Y%m%d%H%M%S').replace(tzinfo=tz)

def load_epg(path, tz_str='America/New_York'):
    import xml.etree.ElementTree as ET
    from zoneinfo import ZoneInfo
    local_tz = ZoneInfo(tz_str)

    tree = ET.parse(path)
    root = tree.getroot()

    channels = []
    channel_map = {}
    for ch in root.findall('channel'):
        cid  = ch.get('id', '')
        nel  = ch.find('display-name')
        name = nel.text if nel is not None else cid
        icon_el = ch.find('icon')
        icon = icon_el.get('src','') if icon_el is not None else ''
        channels.append({'id': cid, 'name': name, 'icon': icon})
        channel_map[cid] = name

    programmes = []
    for prog in root.findall('programme'):
        ss = prog.get('start',''); es = prog.get('stop','')
        ch_id = prog.get('channel','')
        tel   = prog.find('title')
        title = tel.text if tel is not None else ''
        if not ss or not title:
            continue
        try:
            su = _parse_dt(ss)
            eu = _parse_dt(es) if es else su + timedelta(hours=1)
        except Exception:
            continue
        sl = su.astimezone(local_tz)
        el = eu.astimezone(local_tz)
        del_el = prog.find('desc')
        desc = del_el.text[:300] if del_el is not None and del_el.text else ''
        cat_el = prog.find('category')
        cat = cat_el.text if cat_el is not None else ''
        programmes.append({
            'title':      title,
            'channel_id': ch_id,
            'channel':    channel_map.get(ch_id, ch_id),
            'start_ts':   su.timestamp(),
            'stop_ts':    eu.timestamp(),
            'start_iso':  sl.isoformat(),
            'stop_iso':   el.isoformat(),
            'start_fmt':  sl.strftime('%Y-%m-%d %H:%M'),
            'stop_fmt':   el.strftime('%H:%M'),
            'desc':       desc,
            'category':   cat,
        })

    programmes.sort(key=lambda p: p['start_ts'])
    _epg['channels']    = channels
    _epg['channel_map'] = channel_map
    _epg['programmes']  = programmes
    _epg['loaded']      = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return len(programmes)

# ── Conversions ───────────────────────────────────────────────────────────────

_convs = {}   # conv_id -> {file, status, progress, log, pid}
_conv_lock = threading.Lock()

def _run_conv(conv_id, inp, out):
    cmd = ['ffmpeg', '-y', '-i', inp,
           '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
           '-movflags', '+faststart', out]
    with _conv_lock:
        _convs[conv_id].update({'status': 'running', 'progress': 0, 'log': []})
    try:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, text=True)
        with _conv_lock:
            _convs[conv_id]['pid'] = proc.pid
        duration = None
        for line in proc.stderr:
            line = line.strip()
            with _conv_lock:
                _convs[conv_id]['log'].append(line)
                if len(_convs[conv_id]['log']) > 100:
                    _convs[conv_id]['log'] = _convs[conv_id]['log'][-50:]
            if not duration:
                m = re.search(r'Duration:\s*(\d+):(\d+):(\d+)', line)
                if m:
                    h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    duration = h*3600 + mn*60 + s
            if duration:
                m = re.search(r'time=(\d+):(\d+):(\d+)', line)
                if m:
                    h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    pct = min(99, int((h*3600+mn*60+s) / duration * 100))
                    with _conv_lock:
                        _convs[conv_id]['progress'] = pct
        proc.wait()
        with _conv_lock:
            if proc.returncode == 0:
                _convs[conv_id].update({'status': 'done', 'progress': 100})
            else:
                _convs[conv_id]['status'] = 'error'
    except Exception as e:
        with _conv_lock:
            _convs[conv_id].update({'status': 'error', 'error': str(e)})

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/epg-web')
def index():
    return render_template_string(HTML, VERSION=VERSION)

@app.route('/epg-web/api/status')
def api_status():
    return jsonify({'ok': True, 'time': datetime.now().strftime('%I:%M:%S %p'),
                    'loaded': _epg['loaded'], 'programmes': len(_epg['programmes'])})

@app.route('/epg-web/api/config', methods=['GET'])
def api_get_config():
    return jsonify(load_config())

@app.route('/epg-web/api/config', methods=['POST'])
def api_post_config():
    save_config(request.json or {})
    return jsonify({'ok': True})

@app.route('/epg-web/api/load-guide', methods=['POST'])
def api_load_guide():
    cfg = load_config()
    path = cfg.get('guide_path','')
    if not os.path.exists(path):
        return jsonify({'error': f'Not found: {path}'}), 400
    try:
        count = load_epg(path, cfg.get('timezone','America/New_York'))
        return jsonify({'ok': True, 'count': count, 'loaded': _epg['loaded']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/epg-web/api/guide')
def api_guide():
    """Return programmes in a time window for the grid."""
    if not _epg['programmes']:
        return jsonify({'error': 'Guide not loaded'}), 400
    cfg     = load_config()
    from zoneinfo import ZoneInfo
    local_tz = ZoneInfo(cfg.get('timezone','America/New_York'))

    # window_start = query param or now rounded to hour
    ws_param = request.args.get('start')
    if ws_param:
        try:
            ws = datetime.fromisoformat(ws_param).astimezone(timezone.utc)
        except Exception:
            ws = datetime.now(timezone.utc)
    else:
        now = datetime.now(local_tz)
        ws = now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    hours  = int(request.args.get('hours', 4))
    we     = ws + timedelta(hours=hours)
    ws_ts  = ws.timestamp()
    we_ts  = we.timestamp()

    ch_filter = request.args.get('ch', '').lower()

    # Collect channels present in window
    ch_set = set()
    progs_in_window = []
    for p in _epg['programmes']:
        if p['stop_ts'] <= ws_ts or p['start_ts'] >= we_ts:
            continue
        if ch_filter and ch_filter not in p['channel'].lower():
            continue
        ch_set.add(p['channel_id'])
        progs_in_window.append({
            'title':      p['title'],
            'channel_id': p['channel_id'],
            'channel':    p['channel'],
            'start_ts':   p['start_ts'],
            'stop_ts':    p['stop_ts'],
            'start_fmt':  p['start_fmt'],
            'stop_fmt':   p['stop_fmt'],
            'desc':       p['desc'],
            'category':   p['category'],
        })

    # Ordered channels
    ordered_channels = [c for c in _epg['channels'] if c['id'] in ch_set]
    if ch_filter:
        ordered_channels = [c for c in ordered_channels if ch_filter in c['name'].lower()]

    return jsonify({
        'window_start': ws.astimezone(local_tz).isoformat(),
        'window_end':   we.astimezone(local_tz).isoformat(),
        'window_start_ts': ws_ts,
        'window_end_ts':   we_ts,
        'hours':        hours,
        'channels':     ordered_channels[:80],
        'programmes':   progs_in_window,
    })

@app.route('/epg-web/api/channels')
def api_channels():
    if not _epg['channels']:
        return jsonify({'error': 'Guide not loaded'}), 400
    q = request.args.get('q','').lower()
    chs = _epg['channels']
    if q:
        chs = [c for c in chs if q in c['name'].lower()]
    return jsonify({'channels': chs, 'total': len(chs)})

@app.route('/epg-web/api/schedule', methods=['GET'])
def api_get_schedule():
    return jsonify({'schedule': load_schedule()})

@app.route('/epg-web/api/schedule', methods=['POST'])
def api_post_schedule():
    data = request.json or {}
    action = data.get('action')
    sched = load_schedule()

    if action == 'add':
        prog = data.get('programme', {})
        key = (prog.get('title',''), prog.get('channel_id',''), prog.get('start_fmt',''))
        if not any((r['title'], r['channel_id'], r['start_fmt']) == key for r in sched):
            sched.append({
                'title':      prog.get('title',''),
                'channel':    prog.get('channel',''),
                'channel_id': prog.get('channel_id',''),
                'start_fmt':  prog.get('start_fmt',''),
                'stop_fmt':   prog.get('stop_fmt',''),
                'desc':       prog.get('desc',''),
                'status':     'to_record',
                'added':      datetime.now().strftime('%Y-%m-%d %H:%M'),
            })
            save_schedule(sched)
        return jsonify({'ok': True})

    if action == 'update':
        idx = data.get('index'); status = data.get('status')
        if idx is not None and 0 <= idx < len(sched):
            sched[idx]['status'] = status
            save_schedule(sched)
        return jsonify({'ok': True})

    if action == 'remove':
        idx = data.get('index')
        if idx is not None and 0 <= idx < len(sched):
            sched.pop(idx)
            save_schedule(sched)
        return jsonify({'ok': True})

    return jsonify({'error': 'Unknown action'}), 400

@app.route('/epg-web/api/recommendations')
def api_recommendations():
    if not _epg['programmes']:
        return jsonify({'error': 'Guide not loaded'}), 400
    now_ts = datetime.now(timezone.utc).timestamp()
    wl = load_watchlist()
    wl_titles = {w['title'].lower() for w in wl}

    # Count upcoming airings per title
    from collections import Counter
    title_count = Counter()
    title_progs = {}
    for p in _epg['programmes']:
        if p['stop_ts'] <= now_ts:
            continue
        t = p['title']
        title_count[t] += 1
        if t not in title_progs:
            title_progs[t] = p

    # Watchlist shows first, then most-aired
    recs = []
    for title, prog in title_progs.items():
        recs.append({**prog, 'airings': title_count[title],
                     'on_watchlist': title.lower() in wl_titles})

    recs.sort(key=lambda r: (not r['on_watchlist'], -r['airings']))
    return jsonify({'recommendations': recs[:100]})

@app.route('/epg-web/api/watchlist', methods=['GET'])
def api_get_watchlist():
    return jsonify({'watchlist': load_watchlist()})

@app.route('/epg-web/api/watchlist', methods=['POST'])
def api_post_watchlist():
    data = request.json or {}
    action = data.get('action')
    title = data.get('title','').strip()
    wl = load_watchlist()
    if action == 'add':
        if not any(w['title'].lower() == title.lower() for w in wl):
            wl.append({'title': title, 'added': datetime.now().strftime('%Y-%m-%d')})
            save_watchlist(wl)
        return jsonify({'ok': True, 'watchlist': wl})
    if action == 'remove':
        wl = [w for w in wl if w['title'].lower() != title.lower()]
        save_watchlist(wl)
        return jsonify({'ok': True, 'watchlist': wl})
    return jsonify({'error': 'Unknown action'}), 400

# ── Conversion routes ─────────────────────────────────────────────────────────

@app.route('/epg-web/api/convert/list')
def api_conv_list():
    cfg = load_config()
    inp = cfg.get('ts_input', os.path.expanduser('~/Movies'))
    if not os.path.isdir(inp):
        return jsonify({'files': [], 'dir': inp})
    files = sorted([f for f in os.listdir(inp) if f.lower().endswith('.ts')])
    return jsonify({'files': files, 'dir': inp})

@app.route('/epg-web/api/convert/start', methods=['POST'])
def api_conv_start():
    cfg  = load_config()
    data = request.json or {}
    fname = data.get('file','')
    if not fname:
        return jsonify({'error': 'No file specified'}), 400
    inp = os.path.join(cfg.get('ts_input', os.path.expanduser('~/Movies')), fname)
    if not os.path.exists(inp):
        return jsonify({'error': f'File not found: {inp}'}), 400
    out_dir = cfg.get('ts_output', os.path.expanduser('~/Movies/Converted'))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.splitext(fname)[0] + '.mp4')
    conv_id = str(uuid.uuid4())[:8]
    with _conv_lock:
        _convs[conv_id] = {'file': fname, 'output': out, 'status': 'starting',
                           'progress': 0, 'log': [], 'pid': None}
    t = threading.Thread(target=_run_conv, args=(conv_id, inp, out), daemon=True)
    t.start()
    return jsonify({'ok': True, 'id': conv_id})

@app.route('/epg-web/api/convert/status')
def api_conv_status():
    with _conv_lock:
        return jsonify({'conversions': dict(_convs)})

@app.route('/epg-web/api/convert/cancel', methods=['POST'])
def api_conv_cancel():
    conv_id = (request.json or {}).get('id','')
    with _conv_lock:
        c = _convs.get(conv_id)
        if c and c.get('pid') and c['status'] == 'running':
            try:
                import signal
                os.kill(c['pid'], signal.SIGTERM)
                c['status'] = 'cancelled'
            except Exception:
                pass
    return jsonify({'ok': True})

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EPG Manager Web</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0d0d0d;color:#e2e8f0;min-height:100vh;}

/* Header */
header{background:#111;border-bottom:1px solid #222;padding:10px 20px;
       display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.brand{font-size:16px;font-weight:700;color:#4f8ef7;}
.brand span{font-weight:400;color:#555;}
.badge-live{background:#1a3a1a;color:#4ade80;border:1px solid #2d5a2d;
            border-radius:20px;padding:3px 10px;font-size:12px;font-weight:600;}
#clock{font-size:13px;color:#64748b;font-variant-numeric:tabular-nums;}
.spacer{flex:1;}
.btn{display:inline-flex;align-items:center;gap:5px;padding:6px 14px;
     border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;
     border:none;transition:all .15s;white-space:nowrap;}
.btn:disabled{opacity:.4;cursor:default;}
.btn-sm{padding:4px 10px;font-size:12px;}
.btn-primary{background:#3b5bdb;color:#fff;}
.btn-primary:hover:not(:disabled){background:#2f4ac5;}
.btn-ghost{background:#1e1e1e;color:#94a3b8;border:1px solid #2d2d2d;}
.btn-ghost:hover:not(:disabled){background:#2a2a2a;color:#e2e8f0;}
.btn-success{background:#166534;color:#4ade80;}
.btn-success:hover:not(:disabled){background:#15803d;}
.btn-danger{background:#7f1d1d;color:#fca5a5;}
.btn-danger:hover:not(:disabled){background:#991b1b;}
.btn-warn{background:#78350f;color:#fcd34d;}

/* Tabs */
nav{background:#111;border-bottom:1px solid #1e1e1e;padding:0 20px;
    display:flex;gap:2px;overflow-x:auto;}
.tab{padding:10px 16px;font-size:13px;cursor:pointer;color:#555;white-space:nowrap;
     border-bottom:2px solid transparent;transition:all .15s;user-select:none;}
.tab:hover{color:#94a3b8;}
.tab.active{color:#4f8ef7;border-bottom-color:#4f8ef7;}

.pane{display:none;padding:20px;}
.pane.active{display:block;}

/* Guide grid */
.guide-toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;}
.guide-toolbar input{background:#1a1a1a;border:1px solid #2d2d2d;border-radius:6px;
  color:#e2e8f0;padding:6px 10px;font-size:13px;width:220px;}
.guide-wrap{overflow:auto;max-height:calc(100vh - 180px);border:1px solid #1e1e1e;
            border-radius:8px;}
.guide-grid{display:grid;min-width:max-content;}
.time-header{display:flex;position:sticky;top:0;z-index:10;background:#111;
             border-bottom:1px solid #222;}
.ch-name-hdr{width:160px;flex-shrink:0;padding:6px 10px;font-size:11px;
              color:#555;border-right:1px solid #222;background:#111;}
.time-slot{width:240px;flex-shrink:0;padding:6px 8px;font-size:11px;color:#555;
           border-right:1px solid #1a1a1a;text-align:center;}
.guide-row{display:flex;border-bottom:1px solid #1a1a1a;}
.guide-row:hover{background:#141414;}
.ch-name{width:160px;flex-shrink:0;padding:8px 10px;font-size:12px;font-weight:500;
          color:#94a3b8;border-right:1px solid #1e1e1e;position:sticky;left:0;
          background:#0d0d0d;z-index:5;white-space:nowrap;overflow:hidden;
          text-overflow:ellipsis;}
.prog-row{display:flex;flex:1;position:relative;height:42px;}
.prog-block{position:absolute;top:2px;bottom:2px;border-radius:4px;
            background:#1a2744;border:1px solid #243460;overflow:hidden;
            cursor:pointer;transition:background .1s;padding:0 6px;
            display:flex;align-items:center;min-width:4px;}
.prog-block:hover{background:#243460;border-color:#3b5bdb;}
.prog-block.now{background:#1a3a2a;border-color:#2d5a3d;}
.prog-title{font-size:11px;color:#c7d2e7;white-space:nowrap;overflow:hidden;
            text-overflow:ellipsis;}
.now-line{position:absolute;top:0;bottom:0;width:2px;background:#ef4444;z-index:8;
          pointer-events:none;}

/* Cards */
.card{background:#111;border:1px solid #1e1e1e;border-radius:10px;
      padding:20px;margin-bottom:16px;}
.card h2{font-size:13px;font-weight:600;color:#555;text-transform:uppercase;
          letter-spacing:.05em;margin-bottom:14px;}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:13px;}
th{color:#555;font-weight:500;text-align:left;padding:6px 10px;
   border-bottom:1px solid #1e1e1e;}
td{padding:8px 10px;border-bottom:1px solid #141414;vertical-align:top;}
tr:hover td{background:#141414;}
.title-cell{font-weight:500;color:#e2e8f0;}
.ch-cell{color:#64748b;}
.time-cell{color:#555;white-space:nowrap;font-size:12px;}
.act-cell{display:flex;gap:5px;flex-wrap:wrap;}

/* Badges */
.badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;
       border-radius:4px;text-transform:uppercase;}
.badge-record{background:#1e3a5f;color:#60a5fa;}
.badge-recorded{background:#14532d;color:#4ade80;}
.badge-skipped{background:#3d1515;color:#f87171;}
.badge-wl{background:#3b2a00;color:#fcd34d;}

/* Channels grid */
.ch-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;}
.ch-card{background:#1a1a1a;border:1px solid #222;border-radius:8px;
         padding:10px 14px;display:flex;align-items:center;gap:10px;
         font-size:13px;color:#94a3b8;}
.ch-card .ch-num{color:#555;font-size:11px;min-width:24px;}

/* Conversions */
.conv-list{display:flex;flex-direction:column;gap:8px;}
.conv-item{background:#1a1a1a;border:1px solid #222;border-radius:8px;
           padding:12px 16px;display:flex;align-items:center;gap:12px;}
.conv-file{flex:1;font-size:13px;color:#94a3b8;word-break:break-all;}
.conv-bar-wrap{width:120px;height:6px;background:#2d2d2d;border-radius:3px;flex-shrink:0;}
.conv-bar{height:6px;background:#3b5bdb;border-radius:3px;transition:width .5s;}
.conv-bar.done{background:#166534;}
.conv-bar.error{background:#7f1d1d;}
.conv-pct{font-size:12px;color:#64748b;min-width:36px;text-align:right;}

/* Tooltip */
.tooltip{position:fixed;background:#1e293b;border:1px solid #334155;
         border-radius:8px;padding:10px 14px;font-size:12px;z-index:999;
         max-width:300px;pointer-events:none;display:none;}
.tooltip .tt-title{font-weight:600;color:#e2e8f0;margin-bottom:4px;}
.tooltip .tt-time{color:#64748b;margin-bottom:4px;}
.tooltip .tt-desc{color:#94a3b8;}

/* Modal */
#modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
               z-index:200;align-items:center;justify-content:center;}
#modal-overlay.show{display:flex;}
.modal{background:#111;border:1px solid #2d2d2d;border-radius:12px;
       padding:24px;width:480px;max-width:95vw;}
.modal h3{font-size:16px;font-weight:600;margin-bottom:18px;}
.mrow{margin-bottom:12px;}
.mrow label{display:block;font-size:11px;color:#555;margin-bottom:4px;}
.mrow input{width:100%;background:#0d0d0d;border:1px solid #2d2d2d;border-radius:6px;
            color:#e2e8f0;padding:8px 10px;font-size:13px;}
.mrow input:focus{outline:none;border-color:#3b5bdb;}
.mfoot{display:flex;justify-content:flex-end;gap:8px;margin-top:18px;}

.spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.2);
      border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
.status-msg{font-size:13px;color:#555;margin:8px 0;}
.status-msg.ok{color:#4ade80;} .status-msg.err{color:#f87171;}
.empty{color:#333;text-align:center;padding:40px;font-size:14px;}
.search-row{display:flex;gap:8px;margin-bottom:14px;}
.search-row input{flex:1;background:#1a1a1a;border:1px solid #2d2d2d;border-radius:6px;
                  color:#e2e8f0;padding:7px 10px;font-size:13px;}
.search-row input:focus{outline:none;border-color:#3b5bdb;}
</style>
</head>
<body>

<header>
  <span class="brand">📺 EPG Manager <span>Web</span></span>
  <span style="font-size:11px;color:#333;">{{ VERSION }}</span>
  <span class="badge-live" id="live-badge">● Server live</span>
  <span id="clock">--:-- --</span>
  <div class="spacer"></div>
  <button class="btn btn-ghost btn-sm" id="btn-refresh" onclick="loadGuide()">↻ Refresh</button>
  <button class="btn btn-ghost btn-sm" onclick="openSettings()">⚙ Settings</button>
</header>

<nav>
  <div class="tab active" onclick="switchTab('guide')">📺 Guide</div>
  <div class="tab" onclick="switchTab('recommendations')">⭐ Recommendations</div>
  <div class="tab" onclick="switchTab('channels')">📡 Channels</div>
  <div class="tab" onclick="switchTab('schedule')">📅 Schedule</div>
  <div class="tab" onclick="switchTab('conversions')">🔄 Conversions</div>
</nav>

<!-- GUIDE -->
<div id="pane-guide" class="pane active">
  <div class="guide-toolbar">
    <button class="btn btn-ghost btn-sm" onclick="guideNav(-4)">◀ Earlier</button>
    <span id="guide-window" style="font-size:13px;color:#555;"></span>
    <button class="btn btn-ghost btn-sm" onclick="guideNav(4)">Later ▶</button>
    <input id="ch-filter" placeholder="Filter channels…" oninput="renderGuide()" style="margin-left:auto;">
    <button class="btn btn-primary btn-sm" onclick="loadGuide()">Load Guide</button>
  </div>
  <div id="guide-status" class="status-msg"></div>
  <div class="guide-wrap" id="guide-wrap" style="display:none;">
    <div id="guide-inner"></div>
  </div>
</div>

<!-- RECOMMENDATIONS -->
<div id="pane-recommendations" class="pane">
  <div class="card">
    <h2>Recommended Shows</h2>
    <div id="rec-status" class="status-msg"></div>
    <div style="overflow-x:auto;">
      <table><thead><tr>
        <th>Title</th><th>Channel</th><th>Next Airing</th><th>Airings</th><th>Actions</th>
      </tr></thead><tbody id="rec-body"></tbody></table>
    </div>
  </div>
</div>

<!-- CHANNELS -->
<div id="pane-channels" class="pane">
  <div class="card">
    <h2>All Channels</h2>
    <div class="search-row">
      <input id="ch-search" placeholder="Search channels…" oninput="loadChannels()">
    </div>
    <div id="ch-status" class="status-msg"></div>
    <div id="ch-grid" class="ch-grid"></div>
  </div>
</div>

<!-- SCHEDULE -->
<div id="pane-schedule" class="pane">
  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
      <h2 style="margin:0;">Recording Schedule</h2>
      <button class="btn btn-ghost btn-sm" onclick="loadSchedule()">↻ Refresh</button>
    </div>
    <div id="sched-empty" class="empty" style="display:none;">
      Schedule Empty<br><span style="font-size:12px;color:#333;margin-top:6px;display:block;">
      Add programs from the Guide or Recommendations tab.</span>
    </div>
    <div style="overflow-x:auto;">
      <table id="sched-table" style="display:none;">
        <thead><tr><th>Title</th><th>Channel</th><th>Time</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody id="sched-body"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- CONVERSIONS -->
<div id="pane-conversions" class="pane">
  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
      <h2 style="margin:0;">TS → MP4 Converter</h2>
      <button class="btn btn-ghost btn-sm" onclick="loadTsFiles()">↻ Refresh</button>
    </div>
    <div id="conv-dir" style="font-size:12px;color:#333;margin-bottom:12px;"></div>
    <div id="ts-list" class="conv-list"></div>
  </div>
  <div class="card" id="conv-jobs-card" style="display:none;">
    <h2>Active Conversions</h2>
    <div id="conv-jobs" class="conv-list"></div>
  </div>
</div>

<!-- Tooltip -->
<div class="tooltip" id="tooltip">
  <div class="tt-title" id="tt-title"></div>
  <div class="tt-time" id="tt-time"></div>
  <div class="tt-desc" id="tt-desc"></div>
</div>

<!-- Settings modal -->
<div id="modal-overlay" onclick="if(event.target===this)closeSettings()">
  <div class="modal">
    <h3>⚙ Settings</h3>
    <div class="mrow"><label>Guide XML path</label>
      <input id="s-path" placeholder="/Volumes/.../epg/guide.xml"></div>
    <div class="mrow"><label>Timezone</label>
      <input id="s-tz" placeholder="America/New_York"></div>
    <div class="mrow"><label>TS input folder (source .ts files)</label>
      <input id="s-tsin" placeholder="~/Movies"></div>
    <div class="mrow"><label>MP4 output folder (Plex library)</label>
      <input id="s-tsout" placeholder="~/Movies/Converted"></div>
    <div class="mfoot">
      <button class="btn btn-ghost" onclick="closeSettings()">Cancel</button>
      <button class="btn btn-primary" onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let _guideData = null;
let _guideWindowStart = null;   // ISO string
let _guideHours = 4;
const PX_PER_MIN = 4;           // 1 min = 4px → 30min = 120px, 1hr = 240px

// ── Clock + live status ───────────────────────────────────────────────────────
function tickClock() {
  document.getElementById('clock').textContent =
    new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
setInterval(tickClock, 1000);
tickClock();

async function refreshStatus() {
  try {
    const d = await (await fetch('/epg-web/api/status')).json();
    if (d.programmes) {
      document.getElementById('live-badge').textContent =
        `● Server live · ${d.programmes.toLocaleString()} prog`;
    }
  } catch(e) {}
}
setInterval(refreshStatus, 30000);
refreshStatus();

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name) {
  const names = ['guide','recommendations','channels','schedule','conversions'];
  document.querySelectorAll('.tab').forEach((t,i) =>
    t.classList.toggle('active', names[i] === name));
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-'+name).classList.add('active');
  if (name === 'recommendations') loadRecs();
  if (name === 'channels') loadChannels();
  if (name === 'schedule') loadSchedule();
  if (name === 'conversions') { loadTsFiles(); pollConversions(); }
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function openSettings() {
  const cfg = await (await fetch('/epg-web/api/config')).json();
  document.getElementById('s-path').value  = cfg.guide_path || '';
  document.getElementById('s-tz').value    = cfg.timezone   || 'America/New_York';
  document.getElementById('s-tsin').value  = cfg.ts_input   || '';
  document.getElementById('s-tsout').value = cfg.ts_output  || '';
  document.getElementById('modal-overlay').classList.add('show');
}
function closeSettings() { document.getElementById('modal-overlay').classList.remove('show'); }
async function saveSettings() {
  await post('/epg-web/api/config', {
    guide_path: document.getElementById('s-path').value.trim(),
    timezone:   document.getElementById('s-tz').value.trim() || 'America/New_York',
    ts_input:   document.getElementById('s-tsin').value.trim(),
    ts_output:  document.getElementById('s-tsout').value.trim(),
  });
  closeSettings();
}

// ── Guide ─────────────────────────────────────────────────────────────────────
async function loadGuide() {
  const btn = document.getElementById('btn-refresh');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  setGS('Loading guide…');
  try {
    const r = await fetch('/epg-web/api/load-guide', {method:'POST'});
    const d = await r.json();
    if (d.error) { setGS('Error: '+d.error, 'err'); return; }
    setGS(`${d.count.toLocaleString()} programmes loaded · ${d.loaded}`, 'ok');
    await fetchAndRenderGuide();
  } catch(e) { setGS('Failed: '+e.message,'err'); }
  finally { btn.disabled=false; btn.textContent='↻ Refresh'; }
}
function setGS(msg,cls='') {
  const el=document.getElementById('guide-status');
  el.textContent=msg; el.className='status-msg '+(cls||'');
}
function guideNav(hours) {
  if (!_guideWindowStart) return;
  const d = new Date(_guideWindowStart);
  d.setHours(d.getHours() + hours);
  _guideWindowStart = d.toISOString();
  fetchAndRenderGuide();
}
async function fetchAndRenderGuide() {
  const params = new URLSearchParams();
  if (_guideWindowStart) params.set('start', _guideWindowStart);
  params.set('hours', _guideHours);
  const ch = document.getElementById('ch-filter').value.trim();
  if (ch) params.set('ch', ch);
  try {
    const r = await fetch('/epg-web/api/guide?' + params);
    const d = await r.json();
    if (d.error) { setGS(d.error,'err'); return; }
    _guideData = d;
    if (!_guideWindowStart) _guideWindowStart = d.window_start;
    renderGuide();
  } catch(e) { setGS('Failed: '+e.message,'err'); }
}
function renderGuide() {
  if (!_guideData) return;
  const d = _guideData;
  const wsTs = d.window_start_ts;
  const weTs = d.window_end_ts;
  const totalMins = (weTs - wsTs) / 60;
  const totalPx   = totalMins * PX_PER_MIN;
  const nowTs     = Date.now() / 1000;

  // Window label
  const ws = new Date(d.window_start);
  const we = new Date(d.window_end);
  document.getElementById('guide-window').textContent =
    ws.toLocaleString([], {weekday:'short',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})
    + ' – ' + we.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});

  // Channel filter
  const chFilter = document.getElementById('ch-filter').value.trim().toLowerCase();
  const channels = chFilter
    ? d.channels.filter(c => c.name.toLowerCase().includes(chFilter))
    : d.channels;

  // Build time header
  let timeHTML = `<div class="time-header"><div class="ch-name-hdr"></div>`;
  for (let t = wsTs; t < weTs; t += 1800) {
    const lbl = new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    timeHTML += `<div class="time-slot" style="width:${30*PX_PER_MIN}px;">${lbl}</div>`;
  }
  timeHTML += '</div>';

  // Now-line offset
  const nowOffPx = Math.max(0, Math.min(totalPx, (nowTs - wsTs)/60 * PX_PER_MIN));

  // Build rows
  let rowsHTML = '';
  for (const ch of channels) {
    const chProgs = d.programmes.filter(p => p.channel_id === ch.id);
    let progHTML = `<div class="prog-row" style="width:${totalPx}px;">`;
    // now line
    if (nowTs > wsTs && nowTs < weTs) {
      progHTML += `<div class="now-line" style="left:${nowOffPx}px;"></div>`;
    }
    for (const p of chProgs) {
      const pStart = Math.max(p.start_ts, wsTs);
      const pEnd   = Math.min(p.stop_ts,  weTs);
      const left   = (pStart - wsTs) / 60 * PX_PER_MIN;
      const width  = Math.max(2, (pEnd - pStart) / 60 * PX_PER_MIN - 2);
      const isNow  = p.start_ts <= nowTs && p.stop_ts > nowTs;
      const pd = JSON.stringify(p).replace(/'/g, "\\'");
      progHTML += `<div class="prog-block${isNow?' now':''}"
        style="left:${left}px;width:${width}px;"
        onmouseenter="showTip(event,${pd.replace(/"/g,'&quot;')})"
        onmouseleave="hideTip()"
        onclick="addToSchedule(${pd.replace(/"/g,'&quot;')})">
        <span class="prog-title">${esc(p.title)}</span>
      </div>`;
    }
    progHTML += '</div>';
    rowsHTML += `<div class="guide-row">
      <div class="ch-name" title="${esc(ch.name)}">${esc(ch.name)}</div>
      ${progHTML}
    </div>`;
  }

  document.getElementById('guide-inner').innerHTML = timeHTML + rowsHTML;
  document.getElementById('guide-wrap').style.display = 'block';
}

// Tooltip
function showTip(e, p) {
  const tt = document.getElementById('tooltip');
  document.getElementById('tt-title').textContent = p.title;
  document.getElementById('tt-time').textContent  = p.start_fmt + ' – ' + p.stop_fmt;
  document.getElementById('tt-desc').textContent  = p.desc || p.category || '';
  tt.style.display = 'block';
  tt.style.left    = Math.min(e.clientX + 12, window.innerWidth - 320) + 'px';
  tt.style.top     = Math.min(e.clientY + 12, window.innerHeight - 150) + 'px';
}
function hideTip() { document.getElementById('tooltip').style.display='none'; }

// ── Recommendations ───────────────────────────────────────────────────────────
async function loadRecs() {
  document.getElementById('rec-status').textContent = 'Loading…';
  try {
    const d = await (await fetch('/epg-web/api/recommendations')).json();
    if (d.error) { setEl('rec-status',d.error,'err'); return; }
    setEl('rec-status','','');
    const wl = (await (await fetch('/epg-web/api/watchlist')).json()).watchlist || [];
    const wlSet = new Set(wl.map(w=>w.title.toLowerCase()));
    const tbody = document.getElementById('rec-body');
    tbody.innerHTML = d.recommendations.map(r => `
      <tr>
        <td class="title-cell">${esc(r.title)}
          ${wlSet.has(r.title.toLowerCase()) ? '<span class="badge badge-wl" style="margin-left:5px;">★</span>' : ''}
        </td>
        <td class="ch-cell">${esc(r.channel)}</td>
        <td class="time-cell">${esc(r.start_fmt)}</td>
        <td style="color:#555;font-size:12px;">${r.airings}×</td>
        <td class="act-cell">
          <button class="btn btn-ghost btn-sm" onclick='wlToggle(${JSON.stringify(r.title)},${wlSet.has(r.title.toLowerCase())})'>
            ${wlSet.has(r.title.toLowerCase()) ? '★ Watching' : '☆ Watch'}
          </button>
          <button class="btn btn-success btn-sm" onclick='addToSchedule(${JSON.stringify(r)})'>+ Schedule</button>
        </td>
      </tr>`).join('');
  } catch(e) { setEl('rec-status','Failed: '+e.message,'err'); }
}
async function wlToggle(title, isOn) {
  await post('/epg-web/api/watchlist', {action: isOn ? 'remove' : 'add', title});
  loadRecs();
}

// ── Channels ──────────────────────────────────────────────────────────────────
async function loadChannels() {
  const q = document.getElementById('ch-search').value.trim();
  try {
    const d = await (await fetch(`/epg-web/api/channels?q=${encodeURIComponent(q)}`)).json();
    if (d.error) { setEl('ch-status',d.error,'err'); return; }
    setEl('ch-status',`${d.total} channels`,'');
    document.getElementById('ch-grid').innerHTML = d.channels.map((c,i) =>
      `<div class="ch-card"><span class="ch-num">${i+1}</span>${esc(c.name)}</div>`
    ).join('');
  } catch(e) { setEl('ch-status','Failed','err'); }
}

// ── Schedule ──────────────────────────────────────────────────────────────────
async function addToSchedule(prog) {
  await post('/epg-web/api/schedule', {action:'add', programme:prog});
  const msg = `"${prog.title}" added to schedule.`;
  setGS(msg,'ok');
}
async function loadSchedule() {
  const d = await (await fetch('/epg-web/api/schedule')).json();
  const sched = d.schedule || [];
  const tbl = document.getElementById('sched-table');
  const emp = document.getElementById('sched-empty');
  if (!sched.length) { tbl.style.display='none'; emp.style.display='block'; return; }
  tbl.style.display='table'; emp.style.display='none';
  const SL = {to_record:'📋 To Record', recorded:'✅ Recorded', skipped:'⛔ Skipped'};
  const SB = {to_record:'badge-record', recorded:'badge-recorded', skipped:'badge-skipped'};
  document.getElementById('sched-body').innerHTML = sched.map((r,i) => `
    <tr>
      <td class="title-cell">${esc(r.title)}</td>
      <td class="ch-cell">${esc(r.channel)}</td>
      <td class="time-cell">${esc(r.start_fmt)}<br>${esc(r.stop_fmt)}</td>
      <td><span class="badge ${SB[r.status]||''}">${SL[r.status]||r.status}</span></td>
      <td class="act-cell">
        ${r.status!=='recorded'?`<button class="btn btn-success btn-sm" onclick="schedUpdate(${i},'recorded')">✅</button>`:''}
        ${r.status!=='skipped' ?`<button class="btn btn-ghost btn-sm"   onclick="schedUpdate(${i},'skipped')">⛔</button>`:''}
        ${r.status!=='to_record'?`<button class="btn btn-ghost btn-sm"  onclick="schedUpdate(${i},'to_record')">↩</button>`:''}
        <button class="btn btn-danger btn-sm" onclick="schedRemove(${i})">✕</button>
      </td>
    </tr>`).join('');
}
async function schedUpdate(i,s){await post('/epg-web/api/schedule',{action:'update',index:i,status:s});loadSchedule();}
async function schedRemove(i){await post('/epg-web/api/schedule',{action:'remove',index:i});loadSchedule();}

// ── Conversions ───────────────────────────────────────────────────────────────
async function loadTsFiles() {
  const d = await (await fetch('/epg-web/api/convert/list')).json();
  document.getElementById('conv-dir').textContent = 'Source: ' + (d.dir||'');
  const el = document.getElementById('ts-list');
  if (!d.files || !d.files.length) {
    el.innerHTML = '<div class="empty">No .ts files found in source folder.</div>';
    return;
  }
  el.innerHTML = d.files.map(f => `
    <div class="conv-item">
      <span class="conv-file">${esc(f)}</span>
      <button class="btn btn-primary btn-sm" onclick="startConv(${JSON.stringify(f)})">▶ Convert</button>
    </div>`).join('');
}
async function startConv(file) {
  const d = await post('/epg-web/api/convert/start', {file});
  if (d.error) { alert('Error: '+d.error); return; }
  pollConversions();
}
let _pollTimer = null;
function pollConversions() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(async () => {
    const d = await (await fetch('/epg-web/api/convert/status')).json();
    const convs = d.conversions || {};
    const ids = Object.keys(convs);
    const card = document.getElementById('conv-jobs-card');
    if (!ids.length) { card.style.display='none'; return; }
    card.style.display='block';
    const running = ids.some(id => convs[id].status === 'running' || convs[id].status === 'starting');
    if (!running) { clearInterval(_pollTimer); _pollTimer=null; }
    document.getElementById('conv-jobs').innerHTML = ids.map(id => {
      const c = convs[id];
      const barCls = c.status==='done'?'done':c.status==='error'?'error':'';
      const statusText = c.status==='done'?'✅ Done':c.status==='error'?'❌ Error':
                         c.status==='cancelled'?'⛔ Cancelled':`${c.progress||0}%`;
      return `<div class="conv-item">
        <span class="conv-file">${esc(c.file)}</span>
        <div class="conv-bar-wrap"><div class="conv-bar ${barCls}" style="width:${c.progress||0}%"></div></div>
        <span class="conv-pct">${statusText}</span>
        ${c.status==='running'?`<button class="btn btn-danger btn-sm" onclick="cancelConv('${id}')">■</button>`:''}
      </div>`;
    }).join('');
  }, 1500);
}
async function cancelConv(id) { await post('/epg-web/api/convert/cancel',{id}); }

// ── Helpers ───────────────────────────────────────────────────────────────────
async function post(url,body) {
  const r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return r.json();
}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function setEl(id,msg,cls){const e=document.getElementById(id);e.textContent=msg;e.className='status-msg '+(cls||'');}

// ── Init ──────────────────────────────────────────────────────────────────────
window.onload = async () => {
  // Try to load guide data if already in memory
  try {
    const d = await (await fetch('/epg-web/api/guide')).json();
    if (!d.error && d.channels && d.channels.length) {
      _guideData = d;
      _guideWindowStart = d.window_start;
      renderGuide();
      setGS(`Guide loaded · ${d.programmes ? d.programmes.length : ''} programmes in window`, 'ok');
    } else {
      setGS('Click "Load Guide" to load the XMLTV data.');
    }
  } catch(e) {}
};
</script>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import webbrowser
    print(f'\n  EPG Manager Web {VERSION}')
    print(  '  ──────────────────────')
    print(  '  Open: http://localhost:5001/epg-web\n')
    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5001/epg-web')).start()
    app.run(host='127.0.0.1', port=5001, debug=False)
