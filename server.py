#!/usr/bin/env python3
"""EPG Manager Web — Guide · Recommendations · Channels · Schedule · Conversions"""
VERSION = "v20260719"

import json, os, re, sqlite3, subprocess, threading, time, uuid
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
        'guide_path':    '/Volumes/EPG/guide/guide.xml',
        'guide_db_path': os.path.join(BASE_DIR, 'guide.db'),
        'db_path':       '/Volumes/EPG/Movies.db',
        'timezone':      'America/New_York',
        'ts_input':      os.path.expanduser('~/Movies'),
        'ts_output':     os.path.expanduser('~/Movies/Converted'),
        'sd_user':       '',
        'sd_pass':       '',
        'epg_url':       'http://primestreams.tv:826/',
        'epg_user':      '',
        'epg_pass':      '',
        'plex_path':     '/Volumes/Plex/Movies',
        'rec_path':      os.path.expanduser('~/Movies/Recordings'),
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

# ── Movies.db ────────────────────────────────────────────────────────────────

def get_db():
    cfg = load_config()
    path = cfg.get('db_path', '/Volumes/EPG/Movies.db')
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

def db_rows(sql, params=()):
    try:
        conn = get_db()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[DB] {e}')
        return []

def db_run(sql, params=()):
    try:
        conn = get_db()
        conn.execute(sql, params)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB] {e}')
        return False

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

def get_ps_channel_ids(guide_db_path, movies_db_path):
    """Return set of guide.db channel_ids that have a primestreams stream_id in Movies.db.
    Handles both direct ID matches and name-based fallbacks."""
    try:
        import re as _re
        # All Movies.db guide_channels with a stream
        mconn = sqlite3.connect(movies_db_path)
        mrows = mconn.execute(
            'SELECT guide_channel FROM channels WHERE stream_id IS NOT NULL AND guide_channel IS NOT NULL AND guide_channel != ""'
        ).fetchall()
        mconn.close()
        ps_guide_channels = {r[0] for r in mrows}

        gconn = sqlite3.connect(guide_db_path)
        # All distinct channel_id/channel_name pairs in guide.db
        grows = gconn.execute('SELECT DISTINCT channel_id, channel_name FROM guide').fetchall()
        gconn.close()

        result = set()
        # Build a normalised-name → SET of channel_ids map for fallback
        name_map = {}
        for cid, cname in grows:
            key = _re.sub(r'[^a-z0-9]', '', cname.lower())
            name_map.setdefault(key, set()).add(cid)
            if cid in ps_guide_channels:
                result.add(cid)   # direct match

        # Fallback: normalise Movies.db guide_channel and look up in name_map
        for gc in ps_guide_channels:
            norm = _re.sub(r'[^a-z0-9]', '', gc.lower())  # e.g. cinemaxus
            # Strip common country/quality suffixes to get base name
            base = norm
            for suffix in ('us','uk','za','ca','au','sd','hd','west','east'):
                if norm.endswith(suffix):
                    base = norm[:-len(suffix)]
                    break
            # Exact match on base
            if base in name_map:
                result.update(name_map[base])
                continue
            # Prefix match: guide channel name is a prefix of base (TASTE → tastemade)
            for cname_norm, cids in name_map.items():
                if len(cname_norm) >= 3 and len(base) >= 3:
                    if base.startswith(cname_norm) or cname_norm.startswith(base):
                        result.update(cids)
        return result
    except Exception as e:
        print(f'[ps_channel_ids] {e}')
        return set()

def ensure_guide_db(db_path):
    """Create guide.db with schema if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS guide (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            channel_id TEXT,
            channel_name TEXT,
            start_utc TEXT,
            end_utc TEXT,
            desc TEXT,
            category TEXT
        )
    ''')
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_guide_unique
        ON guide(channel_id, start_utc, title)
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS guide_channels (
            channel_id TEXT PRIMARY KEY,
            channel_name TEXT,
            icon TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS series_recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE,
            created_at TEXT,
            active INTEGER DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()

def import_xml_to_guide_db(xml_path, db_path):
    """Parse XMLTV and INSERT OR IGNORE into guide.db. Returns new rows inserted."""
    import xml.etree.ElementTree as ET
    ensure_guide_db(db_path)

    tree = ET.parse(xml_path)
    root = tree.getroot()

    channel_map = {}
    conn = sqlite3.connect(db_path)

    # Upsert channels
    for ch in root.findall('channel'):
        cid  = ch.get('id', '')
        nel  = ch.find('display-name')
        name = nel.text if nel is not None else cid
        icon_el = ch.find('icon')
        icon = icon_el.get('src','') if icon_el is not None else ''
        channel_map[cid] = name
        conn.execute('''
            INSERT OR REPLACE INTO guide_channels(channel_id, channel_name, icon)
            VALUES (?,?,?)
        ''', (cid, name, icon))

    inserted = 0
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
        start_utc = su.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')
        end_utc   = eu.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')
        del_el = prog.find('desc')
        desc = del_el.text[:300] if del_el is not None and del_el.text else ''
        cat_el = prog.find('category')
        cat = cat_el.text if cat_el is not None else ''
        cur = conn.execute('''
            INSERT OR IGNORE INTO guide(title, channel_id, channel_name, start_utc, end_utc, desc, category)
            VALUES (?,?,?,?,?,?,?)
        ''', (title, ch_id, channel_map.get(ch_id, ch_id), start_utc, end_utc, desc, cat))
        inserted += cur.rowcount

    conn.commit()
    conn.close()
    return inserted

def load_epg_from_db(db_path, tz_str='America/New_York'):
    """Load all accumulated guide data from guide.db into memory."""
    from zoneinfo import ZoneInfo
    local_tz = ZoneInfo(tz_str)

    ensure_guide_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    channels = []
    channel_map = {}
    for row in conn.execute('SELECT channel_id, channel_name, icon FROM guide_channels ORDER BY channel_name'):
        cid, name, icon = row['channel_id'], row['channel_name'], row['icon'] or ''
        channels.append({'id': cid, 'name': name, 'icon': icon})
        channel_map[cid] = name

    programmes = []
    for row in conn.execute('SELECT title, channel_id, channel_name, start_utc, end_utc, desc, category FROM guide ORDER BY start_utc'):
        try:
            su = datetime.strptime(row['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            eu = datetime.strptime(row['end_utc'],   '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        except Exception:
            continue
        sl = su.astimezone(local_tz)
        el = eu.astimezone(local_tz)
        programmes.append({
            'title':      row['title'],
            'channel_id': row['channel_id'],
            'channel':    row['channel_name'] or channel_map.get(row['channel_id'], row['channel_id']),
            'start_ts':   su.timestamp(),
            'stop_ts':    eu.timestamp(),
            'start_iso':  sl.isoformat(),
            'stop_iso':   el.isoformat(),
            'start_fmt':  sl.strftime('%Y-%m-%d %H:%M'),
            'stop_fmt':   el.strftime('%H:%M'),
            'desc':       row['desc'] or '',
            'category':   row['category'] or '',
        })

    conn.close()
    _epg['channels']    = channels
    _epg['channel_map'] = channel_map
    _epg['programmes']  = programmes
    _epg['loaded']      = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return len(programmes)

def load_epg(path, tz_str='America/New_York'):
    """Legacy XML-only load (kept for fallback). Prefers guide.db path."""
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

# ── Recording Engine ──────────────────────────────────────────────────────────

_recs      = {}   # rec_id → {title, channel, start_ts, stop_ts, status, progress, log, pid, file}
_rec_lock  = threading.Lock()

def _stream_url(channel_id):
    """Look up stream_id from Movies.db and build the stream URL."""
    cfg  = load_config()
    rows = db_rows(
        'SELECT stream_id FROM channels WHERE guide_channel=? AND stream_id IS NOT NULL AND stream_id!="" LIMIT 1',
        (channel_id,)
    )
    if not rows:
        # Fallback: look up channel_name from guide.db, then prefix-match Movies.db
        try:
            import re as _re2
            gdb_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
            gconn = sqlite3.connect(gdb_path)
            gr = gconn.execute('SELECT channel_name FROM guide WHERE channel_id=? LIMIT 1', (channel_id,)).fetchone()
            gconn.close()
            if gr:
                ch_norm = _re2.sub(r'[^a-z0-9]', '', gr[0].lower())
                mrows = db_rows('SELECT guide_channel, stream_id FROM channels WHERE stream_id IS NOT NULL AND stream_id!=""')
                for mr in mrows:
                    gc_norm = _re2.sub(r'[^a-z0-9]', '', mr['guide_channel'].lower())
                    base = gc_norm
                    for sfx in ('us','uk','za','ca','au','sd','hd'):
                        if gc_norm.endswith(sfx):
                            base = gc_norm[:-len(sfx)]; break
                    if len(ch_norm) >= 3 and len(base) >= 3:
                        if base.startswith(ch_norm) or ch_norm.startswith(base):
                            rows = [mr]; break
        except Exception:
            pass
    if not rows:
        return None, 'No stream_id found for channel'
    sid = rows[0]['stream_id']
    url = f"{cfg['epg_url'].rstrip('/')}/live/{cfg['epg_user']}/{cfg['epg_pass']}/{sid}.ts"
    return url, None

def _safe_filename(title):
    return re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_')[:60]

def _run_recording(rec_id):
    with _rec_lock:
        rec = _recs[rec_id]
    cfg      = load_config()
    rec_dir  = cfg.get('rec_path', os.path.expanduser('~/Movies/Recordings'))
    plex_dir = cfg.get('plex_path', '/Volumes/Plex/Movies')
    os.makedirs(rec_dir, exist_ok=True)

    title    = rec['title']
    start_ts = rec['start_ts']
    stop_ts  = rec['stop_ts']
    ch_id    = rec['channel_id']

    # Wait until start time (with 5s buffer)
    wait = start_ts - time.time() - 5
    if wait > 0:
        with _rec_lock:
            _recs[rec_id]['status'] = f'scheduled ({int(wait//60)}m away)'
        time.sleep(wait)

    url, err = _stream_url(ch_id)
    if err:
        with _rec_lock:
            _recs[rec_id].update({'status': 'error', 'log': [err]})
        return

    duration = int(stop_ts - max(start_ts, time.time())) + 30  # 30s buffer
    ts_file  = os.path.join(rec_dir, f'{_safe_filename(title)}_{int(start_ts)}.ts')
    mp4_file = ts_file.replace('.ts', '.mp4')

    with _rec_lock:
        _recs[rec_id].update({'status': 'recording', 'file': ts_file})

    try:
        cmd = [
            'ffmpeg', '-y',
            '-i', url,
            '-t', str(duration),
            '-c', 'copy',
            ts_file
        ]
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
        with _rec_lock:
            _recs[rec_id]['pid'] = proc.pid
        for line in proc.stderr:
            with _rec_lock:
                _recs[rec_id].setdefault('log', []).append(line.strip())
                if len(_recs[rec_id]['log']) > 50:
                    _recs[rec_id]['log'] = _recs[rec_id]['log'][-30:]
        proc.wait()

        if proc.returncode != 0:
            with _rec_lock:
                _recs[rec_id]['status'] = 'error'
            return

        with _rec_lock:
            _recs[rec_id]['status'] = 'converting'

        # Convert .ts → .mp4
        conv_cmd = [
            'ffmpeg', '-y', '-i', ts_file,
            '-c:v', 'copy', '-c:a', 'aac',
            mp4_file
        ]
        conv = subprocess.run(conv_cmd, capture_output=True, text=True)
        if conv.returncode == 0:
            os.remove(ts_file)
            with _rec_lock:
                _recs[rec_id].update({'status': 'copying', 'file': mp4_file})
            # Copy to Plex
            if os.path.isdir(plex_dir):
                import shutil
                dest = os.path.join(plex_dir, os.path.basename(mp4_file))
                shutil.copy2(mp4_file, dest)
            with _rec_lock:
                _recs[rec_id].update({'status': 'done', 'file': mp4_file})
        else:
            with _rec_lock:
                _recs[rec_id].update({'status': 'done_ts', 'file': ts_file})  # keep .ts if convert failed

    except Exception as e:
        with _rec_lock:
            _recs[rec_id].update({'status': 'error', 'error': str(e)})

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/epg-web')
def index():
    return render_template_string(HTML, VERSION=VERSION)

@app.route('/epg-web/api/status')
def api_status():
    progs = _epg['programmes']
    extra = {}
    if progs:
        from zoneinfo import ZoneInfo
        cfg = load_config()
        ltz = ZoneInfo(cfg.get('timezone','America/New_York'))
        first = datetime.fromtimestamp(progs[0]['start_ts'], tz=ltz).strftime('%Y-%m-%d %H:%M')
        last  = datetime.fromtimestamp(progs[-1]['start_ts'], tz=ltz).strftime('%Y-%m-%d %H:%M')
        extra = {'range_first': first, 'range_last': last}
    return jsonify({'ok': True, 'time': datetime.now().strftime('%I:%M:%S %p'),
                    'loaded': _epg['loaded'], 'programmes': len(progs), **extra})

@app.route('/epg-web/api/config', methods=['GET'])
def api_get_config():
    return jsonify(load_config())

@app.route('/epg-web/api/config', methods=['POST'])
def api_post_config():
    save_config(request.json or {})
    return jsonify({'ok': True})

@app.route('/epg-web/api/fetch-sd', methods=['POST'])
def api_fetch_sd():
    """Pull fresh guide data from Schedules Direct (runs in background thread)."""
    cfg     = load_config()
    sd_user = cfg.get('sd_user','')
    sd_pass = cfg.get('sd_pass','')
    db_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    tz_str  = cfg.get('timezone','America/New_York')
    days    = int(request.json.get('days', 14) if request.json else 14)
    if not sd_user or not sd_pass:
        return jsonify({'error': 'SD credentials not configured'}), 400
    _sd_status['running'] = True
    _sd_status['log']     = []
    _sd_status['result']  = None
    _sd_status['error']   = None
    def _run():
        try:
            from sd_guide import fetch_sd_guide
            def log(msg):
                print(f'[SD] {msg}')
                _sd_status['log'].append(msg)
            result = fetch_sd_guide(sd_user, sd_pass, db_path, days=days, log=log)
            count = load_epg_from_db(db_path, tz_str)
            _sd_status['result'] = {**result, 'total_loaded': count}
        except Exception as e:
            _sd_status['error'] = str(e)
        finally:
            _sd_status['running'] = False
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True, 'message': f'Fetching {days} days from Schedules Direct…'})

@app.route('/epg-web/api/fetch-sd/status')
def api_fetch_sd_status():
    return jsonify(_sd_status)

_sd_status = {'running': False, 'log': [], 'result': None, 'error': None}

@app.route('/epg-web/api/load-guide', methods=['POST'])
def api_load_guide():
    cfg      = load_config()
    xml_path = cfg.get('guide_path', '/Volumes/EPG/guide/guide.xml')
    db_path  = cfg.get('guide_db_path', '/Volumes/EPG/guide/guide.db')
    tz_str   = cfg.get('timezone', 'America/New_York')
    if not os.path.exists(xml_path):
        return jsonify({'error': f'Not found: {xml_path}'}), 400
    try:
        new_rows = import_xml_to_guide_db(xml_path, db_path)
        count    = load_epg_from_db(db_path, tz_str)
        return jsonify({'ok': True, 'count': count, 'new_rows': new_rows, 'loaded': _epg['loaded']})
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

    ch_filter  = request.args.get('ch', '').lower()
    fav_only   = request.args.get('fav', '0') == '1'
    movie_only = request.args.get('movie', '0') == '1'
    ps_only    = request.args.get('ps',  '0') == '1'
    sd_only    = request.args.get('sd',  '0') == '1'

    # Build allowed channel set from Movies.db if filtering
    allowed_ch_ids = None
    guide_db_path  = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    movies_db_path = cfg.get('db_path', '/Volumes/EPG/Movies.db')
    if fav_only or movie_only or ps_only:
        if ps_only and not fav_only and not movie_only:
            allowed_ch_ids = get_ps_channel_ids(guide_db_path, movies_db_path)
        else:
            # Start with direct guide_channel matches
            where_parts = []
            if fav_only:   where_parts.append('favorite = 1')
            if movie_only: where_parts.append('is_movie_channel = 1')
            where = (' AND '.join(where_parts) + ' AND ' if where_parts else '') + \
                    'guide_channel IS NOT NULL AND guide_channel != ""'
            rows = db_rows(f'SELECT guide_channel FROM channels WHERE {where}')
            direct_ids = {r['guide_channel'] for r in rows}
            # Also include SD channel_ids that name-match these Movies.db entries
            # (e.g. 'hbo.us' → numeric SD id for HBO)
            ps_all = get_ps_channel_ids(guide_db_path, movies_db_path)
            # get_ps_channel_ids returns ALL ps channel_ids; intersect with direct_ids' ps subset
            # Build name-map for just the direct_ids channels
            import re as _re4
            try:
                mconn = sqlite3.connect(movies_db_path)
                mconn.row_factory = sqlite3.Row
                fav_gc = direct_ids  # guide_channels that are favorites/movie
                mconn.close()
            except Exception:
                fav_gc = set()
            # From ps_all, keep only those whose guide_channel (via prefix match) is in direct_ids
            # Simpler: run get_ps_channel_ids but restrict to direct_ids
            allowed_ch_ids = direct_ids.copy()  # direct matches
            # Add name-matched SD ids: channels in guide.db whose name prefix-matches a direct_id
            try:
                gconn = sqlite3.connect(guide_db_path)
                grows = gconn.execute('SELECT DISTINCT channel_id, channel_name FROM guide').fetchall()
                gconn.close()
                name_map = {}
                for cid, cname in grows:
                    key = _re4.sub(r'[^a-z0-9]', '', cname.lower())
                    name_map.setdefault(key, set()).add(cid)
                for gc in direct_ids:
                    norm = _re4.sub(r'[^a-z0-9]', '', gc.lower())
                    base = norm
                    for sfx in ('us','uk','za','ca','au','sd','hd','west','east'):
                        if norm.endswith(sfx):
                            base = norm[:-len(sfx)]; break
                    if base in name_map:
                        allowed_ch_ids.update(name_map[base])
                        continue
                    for cname_norm, cids in name_map.items():
                        if len(cname_norm) >= 3 and len(base) >= 3:
                            if base.startswith(cname_norm) or cname_norm.startswith(base):
                                allowed_ch_ids.update(cids)
            except Exception as e:
                print(f'[fav filter] {e}')

    # For SD-only: channels NOT in Movies.db (no stream_id)
    excluded_ch_ids = None
    if sd_only:
        rows = db_rows('SELECT guide_channel FROM channels WHERE guide_channel IS NOT NULL AND guide_channel != ""')
        excluded_ch_ids = {r['guide_channel'] for r in rows}

    # Collect channels present in window
    ch_set = set()
    progs_in_window = []
    for p in _epg['programmes']:
        if p['stop_ts'] <= ws_ts or p['start_ts'] >= we_ts:
            continue
        if allowed_ch_ids is not None and p['channel_id'] not in allowed_ch_ids:
            continue
        if excluded_ch_ids is not None and p['channel_id'] in excluded_ch_ids:
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

    ch_offset = int(request.args.get('ch_offset', 0))
    ch_cap    = 200
    total_ch  = len(ordered_channels)
    page_chs  = ordered_channels[ch_offset:ch_offset + ch_cap]

    return jsonify({
        'window_start': ws.astimezone(local_tz).isoformat(),
        'window_end':   we.astimezone(local_tz).isoformat(),
        'window_start_ts': ws_ts,
        'window_end_ts':   we_ts,
        'hours':        hours,
        'channels':     page_chs,
        'total_channels': total_ch,
        'ch_offset':    ch_offset,
        'programmes':   progs_in_window,
    })

@app.route('/epg-web/api/search')
def api_search():
    """Search channels and current/upcoming programs in guide.db."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'channels': [], 'programs': []})
    cfg      = load_config()
    db_path  = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    from zoneinfo import ZoneInfo
    local_tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))
    now_utc  = datetime.now(timezone.utc)
    now_str  = now_utc.strftime('%Y%m%d%H%M%S')
    like     = f'%{q}%'
    results  = {'channels': [], 'programs': []}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Channel name matches (only channels with current/future programming)
        ch_rows = conn.execute('''
            SELECT DISTINCT g.channel_id, g.channel_name
            FROM guide g
            WHERE g.channel_name LIKE ? AND g.end_utc > ?
            ORDER BY g.channel_name LIMIT 20
        ''', (like, now_str)).fetchall()
        ch_found = {r['channel_id']: {'id': r['channel_id'], 'name': r['channel_name']} for r in ch_rows}

        # Also search Movies.db guide_channel names (e.g. "tastemade.us") and map to guide.db channel_id
        try:
            mdb_path = cfg.get('db_path', '/Volumes/EPG/Movies.db')
            mconn = sqlite3.connect(mdb_path)
            mconn.row_factory = sqlite3.Row
            mrows = mconn.execute(
                'SELECT guide_channel FROM channels WHERE guide_channel LIKE ? AND guide_channel IS NOT NULL LIMIT 20',
                (like,)
            ).fetchall()
            mconn.close()
            for mr in mrows:
                gc = mr['guide_channel']
                # Find this channel_id in guide.db
                grows = conn.execute(
                    'SELECT DISTINCT channel_id, channel_name FROM guide WHERE channel_id=? AND end_utc > ? LIMIT 1',
                    (gc, now_str)
                ).fetchall()
                for gr in grows:
                    if gr['channel_id'] not in ch_found:
                        ch_found[gr['channel_id']] = {'id': gr['channel_id'], 'name': gr['channel_name']}
        except Exception:
            pass

        results['channels'] = sorted(ch_found.values(), key=lambda x: x['name'])[:20]

        # Program title matches — one row per channel airing it, current/upcoming only
        prog_rows = conn.execute('''
            SELECT title, channel_id, channel_name, start_utc, end_utc, category
            FROM guide
            WHERE title LIKE ? AND end_utc > ?
            ORDER BY start_utc
            LIMIT 40
        ''', (like, now_str)).fetchall()

        programs = []
        for r in prog_rows:
            try:
                su = datetime.strptime(r['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                eu = datetime.strptime(r['end_utc'],   '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                sl = su.astimezone(local_tz)
                on_now = su <= now_utc < eu
                programs.append({
                    'title':        r['title'],
                    'channel_id':   r['channel_id'],
                    'channel_name': r['channel_name'],
                    'start_fmt':    ('ON NOW' if on_now else sl.strftime('%a %-I:%M %p')),
                    'category':     r['category'] or '',
                    'on_now':       on_now,
                })
            except Exception:
                continue
        results['programs'] = programs
        conn.close()
    except Exception as e:
        print(f'[search] {e}')
    return jsonify(results)

@app.route('/epg-web/api/channels')
def api_channels():
    if not _epg['channels']:
        return jsonify({'error': 'Guide not loaded'}), 400
    q      = request.args.get('q','').lower()
    favonly= request.args.get('fav','') == '1'
    # Load favorites from DB
    fav_rows = db_rows('SELECT channel_id, nickname, firestick_no FROM channels WHERE favorite=1')
    fav_ids  = {r['channel_id'] for r in fav_rows}
    fav_nick = {r['channel_id']: r['nickname'] for r in fav_rows}
    fav_fs   = {r['channel_id']: r['firestick_no'] for r in fav_rows}

    chs = _epg['channels']
    # Annotate
    annotated = []
    for c in chs:
        if q and q not in c['name'].lower():
            continue
        is_fav = c['id'] in fav_ids
        if favonly and not is_fav:
            continue
        annotated.append({**c, 'favorite': is_fav,
                          'nickname': fav_nick.get(c['id'],''),
                          'firestick_no': fav_fs.get(c['id'],'')})
    # Favorites first
    annotated.sort(key=lambda c: (not c['favorite'], c['name']))
    return jsonify({'channels': annotated, 'total': len(annotated)})

@app.route('/epg-web/api/schedule', methods=['GET'])
def api_get_schedule():
    status_filter = request.args.get('status', '')
    if status_filter:
        rows = db_rows('SELECT * FROM scheduled_recordings WHERE status=? ORDER BY start_time DESC LIMIT 500', (status_filter,))
    else:
        rows = db_rows('SELECT * FROM scheduled_recordings ORDER BY start_time DESC LIMIT 500')
    # Also include any JSON-scheduled items
    json_sched = load_schedule()
    return jsonify({'schedule': rows, 'pending': json_sched})

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
    # Wanted titles from DB cross-referenced with guide
    wanted = db_rows('SELECT * FROM wanted_titles ORDER BY status, title')
    now_ts = datetime.now(timezone.utc).timestamp()

    # Build quick lookup of next airing per title from guide
    next_airing = {}
    if _epg['programmes']:
        for p in _epg['programmes']:
            if p['stop_ts'] <= now_ts:
                continue
            t = p['title'].lower()
            if t not in next_airing:
                next_airing[t] = p

    result = []
    for w in wanted:
        airing = next_airing.get(w['title'].lower()) or next_airing.get(w['normalized_title'].lower() if w['normalized_title'] else '')
        result.append({
            'id':         w['id'],
            'title':      w['title'],
            'year':       w['year'],
            'type':       w['type'],
            'status':     w['status'],
            'notes':      w['notes'],
            'source':     w['source'],
            'imdb_id':    w['imdb_id'],
            'updated_at': w['updated_at'],
            'next_airing': airing,
        })
    return jsonify({'recommendations': result})

@app.route('/epg-web/api/wanted', methods=['POST'])
def api_wanted():
    data = request.json or {}
    action = data.get('action')
    if action == 'add':
        title = data.get('title','').strip()
        year  = data.get('year','')
        norm  = title.lower().replace("'",'').replace('-',' ')
        db_run('INSERT OR IGNORE INTO wanted_titles (title,normalized_title,year,type,source,status,created_at,updated_at) VALUES (?,?,?,?,?,?,datetime("now"),datetime("now"))',
               (title, norm, year, data.get('type','movie'), 'manual', 'wanted'))
        return jsonify({'ok': True})
    if action == 'remove':
        db_run('DELETE FROM wanted_titles WHERE id=?', (data.get('id'),))
        return jsonify({'ok': True})
    if action == 'update':
        db_run('UPDATE wanted_titles SET status=?,notes=?,updated_at=datetime("now") WHERE id=?',
               (data.get('status'), data.get('notes',''), data.get('id')))
        return jsonify({'ok': True})
    return jsonify({'error': 'Unknown action'}), 400

@app.route('/epg-web/api/library')
def api_library():
    q = request.args.get('q','').strip()
    if q:
        rows = db_rows('SELECT * FROM master_titles WHERE title LIKE ? OR genre LIKE ? OR actors LIKE ? ORDER BY title LIMIT 200',
                       (f'%{q}%', f'%{q}%', f'%{q}%'))
    else:
        rows = db_rows('SELECT * FROM master_titles ORDER BY title LIMIT 500')
    return jsonify({'library': rows, 'total': len(rows)})

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

# ── Programme Info (OMDB/TMDB enrichment) ────────────────────────────────────

@app.route('/epg-web/api/prog-info')
def api_prog_info():
    from urllib import request as urlreq
    from urllib.parse import quote
    title = request.args.get('title', '').strip()
    year  = request.args.get('year', '').strip()
    if not title:
        return jsonify({'error': 'No title'}), 400

    # Strip trailing (YYYY) from titles like "Batman Returns (1992)"
    import re as _re
    m = _re.match(r'^(.+?)\s*\((\d{4})\)\s*$', title)
    if m:
        title = m.group(1).strip()
        if not year:
            year = m.group(2)

    cfg = load_config()
    omdb_key = cfg.get('omdb_key', '')
    tmdb_key = cfg.get('tmdb_key', '')

    # 1. Check master_titles — for in_library flag + local poster fallback
    lib_row = None
    rows = db_rows(
        'SELECT title, poster_url, actors, plot, imdb_rating, genre, year, director, rated FROM master_titles WHERE lower(title)=lower(?) LIMIT 1',
        (title,)
    )
    if not rows:
        rows = db_rows(
            'SELECT title, poster_url, actors, plot, imdb_rating, genre, year, director, rated FROM master_titles WHERE lower(title) LIKE lower(?) LIMIT 1',
            (f'%{title}%',)
        )
    if rows:
        lib_row = rows[0]

    in_library  = lib_row is not None
    local_poster = lib_row['poster_url'] if lib_row and lib_row.get('poster_url') else ''

    # 2. OMDB — primary enrichment source (best actor/director coverage)
    if omdb_key:
        try:
            q   = quote(title)
            yr  = f'&y={year}' if year else ''
            url = f'http://www.omdbapi.com/?t={q}{yr}&apikey={omdb_key}'
            with urlreq.urlopen(url, timeout=5) as resp:
                od = json.loads(resp.read())
            if od.get('Response') == 'True':
                poster = od.get('Poster','')
                if poster == 'N/A': poster = ''
                return jsonify({
                    'source':      'omdb',
                    'in_library':  in_library,
                    'title':       od.get('Title',''),
                    'year':        od.get('Year',''),
                    'genre':       od.get('Genre',''),
                    'rated':       od.get('Rated',''),
                    'plot':        od.get('Plot',''),
                    'actors':      od.get('Actors',''),
                    'director':    od.get('Director',''),
                    'poster':      poster or local_poster,
                    'imdb_rating': od.get('imdbRating',''),
                    'imdb_votes':  od.get('imdbVotes',''),
                })
        except Exception as e:
            print(f'[OMDB] {e}')

    # 3. TMDB fallback
    if tmdb_key:
        try:
            q   = quote(title)
            url = f'https://api.themoviedb.org/3/search/multi?api_key={tmdb_key}&query={q}'
            with urlreq.urlopen(url, timeout=5) as resp:
                td = json.loads(resp.read())
            results = td.get('results', [])
            if results:
                m = results[0]
                poster = f"https://image.tmdb.org/t/p/w300{m['poster_path']}" if m.get('poster_path') else ''
                return jsonify({
                    'source':      'tmdb',
                    'in_library':  in_library,
                    'title':       m.get('title') or m.get('name',''),
                    'year':        (m.get('release_date') or m.get('first_air_date',''))[:4],
                    'genre':       '',
                    'rated':       '',
                    'plot':        m.get('overview',''),
                    'actors':      '',
                    'director':    '',
                    'poster':      poster or local_poster,
                    'imdb_rating': str(round(m.get('vote_average',0),1)),
                    'imdb_votes':  '',
                })
        except Exception as e:
            print(f'[TMDB] {e}')

    # 4. Fall back to whatever we have locally
    if lib_row:
        return jsonify({
            'source':      'library',
            'in_library':  True,
            'title':       lib_row['title'],
            'year':        lib_row['year'] or '',
            'genre':       lib_row['genre'] or '',
            'rated':       lib_row['rated'] or '',
            'plot':        lib_row['plot'] or '',
            'actors':      lib_row['actors'] or '',
            'director':    lib_row['director'] or '',
            'poster':      local_poster,
            'imdb_rating': lib_row['imdb_rating'] or '',
            'imdb_votes':  '',
        })

    # 5. guide_listings
    gl = db_rows('SELECT title, plot, actors, director, year, star_rating, genre FROM guide_listings WHERE lower(title)=lower(?) LIMIT 1', (title,))
    if not gl:
        gl = db_rows('SELECT title, plot, actors, director, year, star_rating, genre FROM guide_listings WHERE lower(title) LIKE lower(?) LIMIT 1', (f'%{title}%',))
    if gl:
        g = gl[0]
        return jsonify({
            'source':      'guide',
            'in_library':  False,
            'title':       g['title'],
            'year':        g['year'] or '',
            'genre':       g['genre'] or '',
            'rated':       '',
            'plot':        g['plot'] or '',
            'actors':      g['actors'] or '',
            'director':    g['director'] or '',
            'poster':      '',
            'imdb_rating': g['star_rating'] or '',
            'imdb_votes':  '',
        })

    return jsonify({'error': 'Not found'}), 404

@app.route('/epg-web/api/airings')
def api_airings():
    """Return all future airings of a title from guide.db."""
    from zoneinfo import ZoneInfo
    title   = request.args.get('title','').strip()
    if not title:
        return jsonify({'airings': []})
    cfg     = load_config()
    db_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    tz_str  = cfg.get('timezone','America/New_York')
    local_tz = ZoneInfo(tz_str)
    now_utc = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')

    # Strip trailing (YYYY) so "Batman Returns (1992)" also matches "Batman Returns"
    import re as _re2
    m2 = _re2.match(r'^(.+?)\s*\((\d{4})\)\s*$', title)
    clean_title = m2.group(1).strip() if m2 else title

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Search both full title and cleaned title
        rows = conn.execute('''
            SELECT channel_id, channel_name, start_utc, end_utc
            FROM guide
            WHERE (lower(title) = lower(?) OR lower(title) = lower(?))
            AND end_utc > ?
            ORDER BY start_utc
            LIMIT 30
        ''', (title, clean_title, now_utc)).fetchall()
        conn.close()
    except Exception:
        return jsonify({'airings': []})

    # Build set of guide.db channel_ids that have a primestreams stream (incl. name-match fallback)
    recordable = get_ps_channel_ids(db_path, cfg.get('db_path', '/Volumes/EPG/Movies.db'))

    airings = []
    for r in rows:
        try:
            su = datetime.strptime(r['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            eu = datetime.strptime(r['end_utc'],   '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            sl = su.astimezone(local_tz)
            el = eu.astimezone(local_tz)
            now_ts = datetime.now(timezone.utc).timestamp()
            airings.append({
                'channel_id':   r['channel_id'],
                'channel_name': r['channel_name'],
                'start_ts':     su.timestamp(),
                'stop_ts':      eu.timestamp(),
                'start_fmt':    sl.strftime('%a %b %-d, %-I:%M %p'),
                'stop_fmt':     el.strftime('%-I:%M %p'),
                'can_record':   r['channel_id'] in recordable,
                'on_now':       su.timestamp() <= now_ts < eu.timestamp(),
            })
        except Exception:
            continue
    return jsonify({'airings': airings})

# ── VLC Play ──────────────────────────────────────────────────────────────────

_vlc_pid = None

@app.route('/epg-web/api/play', methods=['POST'])
def api_play():
    global _vlc_pid
    data       = request.json or {}
    channel_id = data.get('channel_id','')
    cfg        = load_config()
    url, err   = _stream_url(channel_id)
    if err:
        return jsonify({'error': err}), 400
    # Kill existing VLC if running
    if _vlc_pid:
        try:
            import signal
            os.kill(_vlc_pid, signal.SIGTERM)
        except Exception:
            pass
        _vlc_pid = None
    try:
        vlc_paths = [
            '/Applications/VLC.app/Contents/MacOS/VLC',
            '/usr/bin/vlc',
            'vlc',
        ]
        vlc_exe = next((p for p in vlc_paths if os.path.exists(p)), 'vlc')
        proc = subprocess.Popen([vlc_exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _vlc_pid = proc.pid
        return jsonify({'ok': True, 'pid': _vlc_pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/epg-web/api/play/stop', methods=['POST'])
def api_play_stop():
    global _vlc_pid
    if _vlc_pid:
        try:
            import signal
            os.kill(_vlc_pid, signal.SIGTERM)
        except Exception:
            pass
        _vlc_pid = None
    return jsonify({'ok': True})

# ── Recording Routes ──────────────────────────────────────────────────────────

# ── Series Recordings ────────────────────────────────────────────────────────

def _schedule_series_airings(title, guide_db_path, movies_db_path, tz_str='America/New_York'):
    """Queue recordings for all future primestreams airings of title. Returns count scheduled."""
    from zoneinfo import ZoneInfo
    import re as _re3
    local_tz  = ZoneInfo(tz_str)
    now_utc   = datetime.now(timezone.utc)
    now_str   = now_utc.strftime('%Y%m%d%H%M%S')
    clean     = _re3.match(r'^(.+?)\s*\(\d{4}\)\s*$', title)
    clean_title = clean.group(1).strip() if clean else title
    recordable = get_ps_channel_ids(guide_db_path, movies_db_path)
    scheduled = 0
    try:
        conn = sqlite3.connect(guide_db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''
            SELECT channel_id, channel_name, start_utc, end_utc
            FROM guide
            WHERE (lower(title)=lower(?) OR lower(title)=lower(?))
            AND start_utc > ? AND channel_id IN ({})
            ORDER BY start_utc LIMIT 100
        '''.format(','.join('?' * len(recordable))),
            [title, clean_title, now_str] + list(recordable)
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f'[series] query error: {e}')
        return 0
    with _rec_lock:
        existing_keys = {(r['channel_id'], r['start_ts']) for r in _recs.values()
                         if r.get('status') in ('queued','scheduled','recording')}
    for r in rows:
        try:
            su = datetime.strptime(r['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            eu = datetime.strptime(r['end_utc'],   '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            key = (r['channel_id'], su.timestamp())
            if key in existing_keys:
                continue
            rec_id = f"rec_{int(time.time()*1000)}_{r['channel_id'][:8]}"
            with _rec_lock:
                _recs[rec_id] = {
                    'title': title, 'channel_id': r['channel_id'],
                    'channel': r['channel_name'],
                    'start_ts': su.timestamp(), 'stop_ts': eu.timestamp(),
                    'status': 'queued', 'progress': 0, 'log': [], 'pid': None, 'file': None,
                }
                existing_keys.add(key)
            t = threading.Thread(target=_run_recording, args=(rec_id,), daemon=True)
            t.start()
            scheduled += 1
        except Exception as e:
            print(f'[series] airing error: {e}')
    return scheduled

@app.route('/epg-web/api/record/series', methods=['GET'])
def api_series_list():
    cfg = load_config()
    db_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    now_str = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        series = conn.execute('SELECT id, title, created_at, active FROM series_recordings ORDER BY created_at DESC').fetchall()
        result = []
        for s in series:
            # Count upcoming primestreams airings
            recordable = get_ps_channel_ids(db_path, cfg.get('db_path', '/Volumes/EPG/Movies.db'))
            cnt = 0
            if recordable:
                cnt = conn.execute(
                    'SELECT COUNT(*) FROM guide WHERE lower(title)=lower(?) AND start_utc>? AND channel_id IN ({})'.format(
                        ','.join('?'*len(recordable))),
                    [s['title'], now_str] + list(recordable)
                ).fetchone()[0]
            result.append({'id': s['id'], 'title': s['title'], 'created_at': s['created_at'],
                           'active': s['active'], 'upcoming': cnt})
        conn.close()
        return jsonify({'series': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/epg-web/api/record/series', methods=['POST'])
def api_series_add():
    data  = request.json or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'No title'}), 400
    cfg = load_config()
    db_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    ensure_guide_db(db_path)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT OR REPLACE INTO series_recordings(title, created_at, active) VALUES(?,?,1)',
            (title, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    scheduled = _schedule_series_airings(title, db_path,
                    cfg.get('db_path', '/Volumes/EPG/Movies.db'),
                    cfg.get('timezone', 'America/New_York'))
    return jsonify({'ok': True, 'scheduled': scheduled})

@app.route('/epg-web/api/record/series/cancel', methods=['POST'])
def api_series_cancel():
    data  = request.json or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'No title'}), 400
    cfg = load_config()
    db_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    try:
        conn = sqlite3.connect(db_path)
        conn.execute('UPDATE series_recordings SET active=0 WHERE title=?', (title,))
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    # Cancel any queued (not yet started) recordings for this title
    cancelled = 0
    with _rec_lock:
        for rec_id, rec in _recs.items():
            if rec.get('title','').lower() == title.lower() and rec.get('status') == 'queued':
                rec['status'] = 'cancelled'
                cancelled += 1
    return jsonify({'ok': True, 'cancelled': cancelled})

@app.route('/epg-web/api/record', methods=['POST'])
def api_record():
    data       = request.json or {}
    title      = data.get('title', 'Unknown')
    channel_id = data.get('channel_id', '')
    start_ts   = float(data.get('start_ts', time.time()))
    stop_ts    = float(data.get('stop_ts', time.time() + 3600))
    rec_id     = str(uuid.uuid4())[:8]
    with _rec_lock:
        _recs[rec_id] = {
            'title':      title,
            'channel_id': channel_id,
            'start_ts':   start_ts,
            'stop_ts':    stop_ts,
            'status':     'queued',
            'progress':   0,
            'log':        [],
            'pid':        None,
            'file':       None,
        }
    t = threading.Thread(target=_run_recording, args=(rec_id,), daemon=True)
    t.start()
    return jsonify({'ok': True, 'id': rec_id})

@app.route('/epg-web/api/record/status')
def api_rec_status():
    with _rec_lock:
        return jsonify({'recordings': dict(_recs)})

@app.route('/epg-web/api/record/cancel', methods=['POST'])
def api_rec_cancel():
    rec_id = (request.json or {}).get('id','')
    with _rec_lock:
        r = _recs.get(rec_id)
        if r and r.get('pid') and 'recording' in r.get('status',''):
            try:
                import signal
                os.kill(r['pid'], signal.SIGTERM)
                r['status'] = 'cancelled'
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
.ch-fav{border-color:#4a3a00!important;background:#1a1500!important;}
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
    <select id="guide-ch-mode" onchange="fetchAndRenderGuide()" style="background:#1a1a1a;border:1px solid #2d2d2d;border-radius:6px;color:#94a3b8;padding:5px 10px;font-size:13px;">
      <option value="all">All Channels</option>
      <option value="fav">★ Favorites</option>
      <option value="movie">🎬 Movie Channels</option>
      <option value="ps">📡 PrimeStreams Only</option>
      <option value="sd">📺 SD Only</option>
    </select>
    <div style="position:relative;display:inline-block;">
      <input id="ch-filter" placeholder="🔍 Search channels & shows…" oninput="onSearchInput(this.value)" onkeydown="if(event.key==='Escape')clearSearch()" autocomplete="off" style="width:220px;">
      <div id="search-dropdown" style="display:none;position:absolute;top:100%;left:0;width:320px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;z-index:500;max-height:320px;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);margin-top:4px;"></div>
    </div>
    <button id="ch-page-prev" class="btn btn-ghost btn-sm" onclick="chPagePrev()" style="display:none;">◀ Prev 200</button>
    <span id="ch-page-info" style="font-size:12px;color:#64748b;"></span>
    <button id="ch-page-next" class="btn btn-ghost btn-sm" onclick="chPageNext()" style="display:none;">Next 200 ▶</button>
    <button class="btn btn-primary btn-sm" onclick="loadGuide()">Load Guide</button>
    <button class="btn btn-ghost btn-sm" onclick="fetchSD()" id="btn-sd" title="Pull 14 days from Schedules Direct">📡 Fetch SD</button>
  </div>
  <div id="guide-status" class="status-msg"></div>
  <div id="sd-status" class="status-msg" style="display:none;"></div>
  <div class="guide-wrap" id="guide-wrap" style="display:none;">
    <div id="guide-inner"></div>
  </div>
  <!-- Recordings panel -->
  <div id="rec-panel" style="margin-top:16px;display:none;">
    <h3 style="font-size:13px;color:#64748b;margin-bottom:8px;">🔴 Active Recordings</h3>
    <div id="rec-list"></div>
  </div>
  <!-- Series Recordings panel -->
  <div style="margin-top:24px;">
    <h3 style="font-size:13px;color:#64748b;margin-bottom:8px;">📺 Series Recordings</h3>
    <div id="series-list" style="max-height:300px;overflow-y:auto;"></div>
  </div>
</div>

<!-- Programme detail modal -->
<div id="prog-modal-overlay" onclick="if(event.target===this)closeProg()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;align-items:center;justify-content:center;">
  <div style="background:#111827;border:1px solid #1e2d3d;border-radius:14px;width:90%;max-width:620px;box-shadow:0 24px 80px rgba(0,0,0,.7);overflow:hidden;position:relative;">
    <!-- Close -->
    <button onclick="closeProg()" style="position:absolute;top:12px;right:14px;background:none;border:none;color:#64748b;font-size:20px;cursor:pointer;line-height:1;">✕</button>
    <!-- Loading state -->
    <div id="pm-loading" style="padding:48px;text-align:center;color:#64748b;font-size:14px;">Loading…</div>
    <!-- Content -->
    <div id="pm-content" style="display:none;">
      <!-- Backdrop / poster row -->
      <div style="display:flex;gap:0;min-height:180px;">
        <div id="pm-poster-wrap" style="flex-shrink:0;width:130px;background:#0d1117;">
          <img id="pm-poster" src="" alt="" style="width:130px;height:195px;object-fit:cover;display:block;">
        </div>
        <div style="flex:1;padding:20px 20px 14px;overflow-y:auto;">
            <div style="display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap;margin-bottom:6px;">
              <h3 id="pm-title" style="font-size:18px;font-weight:700;color:#f1f5f9;margin:0;line-height:1.3;"></h3>
              <span id="pm-library-badge" style="display:none;background:#166534;color:#86efac;font-size:10px;font-weight:600;padding:2px 7px;border-radius:99px;white-space:nowrap;margin-top:3px;">IN LIBRARY</span>
            </div>
            <div id="pm-air" style="font-size:12px;color:#3b82f6;margin-bottom:8px;font-weight:500;"></div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
              <span id="pm-year"  style="font-size:12px;color:#94a3b8;"></span>
              <span id="pm-rated" style="font-size:11px;background:#1e293b;color:#94a3b8;padding:1px 6px;border-radius:4px;"></span>
              <span id="pm-genre" style="font-size:12px;color:#94a3b8;"></span>
              <span id="pm-imdb"  style="font-size:12px;color:#fbbf24;font-weight:600;"></span>
            </div>
            <div id="pm-actors" style="font-size:12px;color:#94a3b8;margin-bottom:3px;"></div>
            <div id="pm-director" style="font-size:12px;color:#94a3b8;margin-bottom:10px;"></div>
            <p id="pm-plot" style="font-size:13px;color:#94a3b8;line-height:1.6;margin:0;"></p>
        </div>
      </div>
      <!-- Next primestreams airing (featured) -->
      <div id="pm-next-wrap" style="display:none;border-top:1px solid #1e293b;padding:14px 20px;background:#0f1923;">
        <div style="font-size:11px;font-weight:600;color:#3b82f6;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px;">📡 Next on PrimeStreams</div>
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <div id="pm-next-info" style="flex:1;font-size:13px;color:#e2e8f0;"></div>
          <button id="pm-play-btn" class="btn btn-ghost" onclick="playStream()" style="border-color:#22c55e;color:#22c55e;">▶ Play</button>
          <button id="pm-rec-next-btn" class="btn btn-primary" onclick="recordNext()">⏱ Record</button>
        </div>
      </div>
      <!-- All future airings -->
      <div id="pm-airings-wrap" style="display:none;border-top:1px solid #1e293b;padding:14px 20px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
          <span style="font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.05em;">📅 All Future Airings</span>
          <button id="pm-series-btn" class="btn btn-ghost btn-sm" onclick="recordSeries()" style="font-size:11px;padding:3px 10px;">📺 Record Series</button>
        </div>
        <div id="pm-airings-list" style="max-height:160px;overflow-y:auto;"></div>
      </div>
      <!-- Footer -->
      <div style="padding:12px 20px;border-top:1px solid #1e293b;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <button class="btn btn-ghost" onclick="closeProg()">Close</button>
        <div id="pm-status" class="status-msg" style="margin:0;flex:1;text-align:right;"></div>
      </div>
    </div>
  </div>
</div>

<!-- RECOMMENDATIONS -->
<div id="pane-recommendations" class="pane">
  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
      <h2 style="margin:0;">Wanted Titles</h2>
      <div style="display:flex;gap:8px;">
        <button class="btn btn-primary btn-sm" onclick="addWanted()">+ Add</button>
        <button class="btn btn-ghost btn-sm" onclick="loadRecs()">↻ Refresh</button>
      </div>
    </div>
    <div id="rec-status" class="status-msg"></div>
    <div style="overflow-x:auto;">
      <table><thead><tr>
        <th>Title</th><th>Next Airing on Guide</th><th>Status</th><th>Actions</th>
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
      <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:#64748b;white-space:nowrap;cursor:pointer;">
        <input type="checkbox" id="ch-fav-only" onchange="loadChannels()"> ★ Favorites only
      </label>
    </div>
    <div id="ch-status" class="status-msg"></div>
    <div id="ch-grid" class="ch-grid"></div>
  </div>
</div>

<!-- SCHEDULE -->
<div id="pane-schedule" class="pane">
  <div class="card">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;">
      <h2 style="margin:0;">Recording Schedule</h2>
      <select id="sched-filter" onchange="loadSchedule()" style="background:#1a1a1a;border:1px solid #2d2d2d;border-radius:6px;color:#94a3b8;padding:5px 8px;font-size:12px;">
        <option value="">All statuses</option>
        <option value="scheduled">Scheduled</option>
        <option value="completed">Completed</option>
        <option value="failed">Failed</option>
        <option value="cancelled">Cancelled</option>
      </select>
      <button class="btn btn-ghost btn-sm" style="margin-left:auto;" onclick="loadSchedule()">↻ Refresh</button>
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
      <input id="s-path" placeholder="/Volumes/EPG/guide/guide.xml"></div>
    <div class="mrow"><label>Guide DB path (accumulates data over time)</label>
      <input id="s-guidedb" placeholder="/Volumes/EPG/guide/guide.db"></div>
    <div class="mrow"><label>Movies.db path</label>
      <input id="s-db" placeholder="/Volumes/EPG/Movies.db"></div>
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
let _chOffset = 0;
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

// Auto-render guide on page load; if SD is running, poll until done then render
async function autoLoad() {
  const s = await (await fetch('/epg-web/api/status')).json();
  if (s.programmes > 0) {
    await fetchAndRenderGuide();
  }
  // If SD fetch is running in background, show progress and re-render when done
  const sd = await (await fetch('/epg-web/api/fetch-sd/status')).json();
  if (sd.running) {
    const sdEl = document.getElementById('sd-status');
    sdEl.style.display = '';
    sdEl.textContent = '📡 Fetching from Schedules Direct…';
    if (_sdPoll) clearInterval(_sdPoll);
    _sdPoll = setInterval(async () => {
      const s2 = await (await fetch('/epg-web/api/fetch-sd/status')).json();
      const last = s2.log.length ? s2.log[s2.log.length-1] : '…';
      if (s2.running) {
        sdEl.textContent = '📡 ' + last;
      } else if (s2.error) {
        sdEl.textContent = '❌ ' + s2.error;
        sdEl.className = 'status-msg err';
        clearInterval(_sdPoll);
      } else if (s2.result) {
        const r = s2.result;
        sdEl.textContent = `✅ SD done — ${r.inserted} new, ${r.total_loaded.toLocaleString()} total`;
        sdEl.className = 'status-msg ok';
        clearInterval(_sdPoll);
        await fetchAndRenderGuide();
      }
    }, 2000);
  }
}
autoLoad();

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name) {
  const names = ['guide','recommendations','channels','schedule','conversions'];
  document.querySelectorAll('.tab').forEach((t,i) =>
    t.classList.toggle('active', names[i] === name));
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-'+name).classList.add('active');
  if (name === 'recommendations') loadRecs();
  if (name === 'channels') loadChannels();
  if (name === 'schedule') { loadSchedule(); loadSeriesRecordings(); }
  if (name === 'conversions') { loadTsFiles(); pollConversions(); }
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function openSettings() {
  const cfg = await (await fetch('/epg-web/api/config')).json();
  document.getElementById('s-path').value    = cfg.guide_path    || '';
  document.getElementById('s-guidedb').value = cfg.guide_db_path || '';
  document.getElementById('s-db').value      = cfg.db_path       || '';
  document.getElementById('s-tz').value    = cfg.timezone   || 'America/New_York';
  document.getElementById('s-tsin').value  = cfg.ts_input   || '';
  document.getElementById('s-tsout').value = cfg.ts_output  || '';
  document.getElementById('modal-overlay').classList.add('show');
}
function closeSettings() { document.getElementById('modal-overlay').classList.remove('show'); }
async function saveSettings() {
  await post('/epg-web/api/config', {
    guide_path:    document.getElementById('s-path').value.trim(),
    guide_db_path: document.getElementById('s-guidedb').value.trim(),
    db_path:       document.getElementById('s-db').value.trim(),
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
    const newInfo = d.new_rows > 0 ? ` (+${d.new_rows.toLocaleString()} new)` : ' (no new data)';
    setGS(`${d.count.toLocaleString()} programmes loaded${newInfo} · ${d.loaded}`, 'ok');
    await fetchAndRenderGuide();
  } catch(e) { setGS('Failed: '+e.message,'err'); }
  finally { btn.disabled=false; btn.textContent='↻ Refresh'; }
}
function setGS(msg,cls='') {
  const el=document.getElementById('guide-status');
  el.textContent=msg; el.className='status-msg '+(cls||'');
}

let _sdPoll = null;
async function fetchSD() {
  const btn = document.getElementById('btn-sd');
  const sdEl = document.getElementById('sd-status');
  btn.disabled = true;
  sdEl.style.display = '';
  sdEl.className = 'status-msg';
  sdEl.textContent = 'Starting Schedules Direct fetch…';
  await post('/epg-web/api/fetch-sd', {days: 14});
  if (_sdPoll) clearInterval(_sdPoll);
  _sdPoll = setInterval(async () => {
    const s = await (await fetch('/epg-web/api/fetch-sd/status')).json();
    const last = s.log.length ? s.log[s.log.length - 1] : '…';
    if (s.running) {
      sdEl.textContent = '📡 ' + last;
    } else if (s.error) {
      sdEl.textContent = '❌ ' + s.error;
      sdEl.className = 'status-msg err';
      clearInterval(_sdPoll); btn.disabled = false;
    } else if (s.result) {
      const r = s.result;
      sdEl.textContent = `✅ SD done — ${r.inserted} new, ${r.total_loaded.toLocaleString()} total · reload guide to see`;
      sdEl.className = 'status-msg ok';
      clearInterval(_sdPoll); btn.disabled = false;
      await loadGuide();
    }
  }, 2000);
}
function guideNav(hours) {
  if (!_guideWindowStart) return;
  const d = new Date(_guideWindowStart);
  d.setHours(d.getHours() + hours);
  _guideWindowStart = d.toISOString();
  fetchAndRenderGuide();
}
let _searchTimer = null;
function onSearchInput(val) {
  clearTimeout(_searchTimer);
  const dd = document.getElementById('search-dropdown');
  if (val.length < 2) { dd.style.display = 'none'; fetchAndRenderGuide(); return; }
  _searchTimer = setTimeout(async () => {
    const r = await fetch('/epg-web/api/search?q=' + encodeURIComponent(val));
    const d = await r.json();
    let html = '';
    if (d.channels && d.channels.length) {
      html += '<div style="padding:6px 12px;font-size:11px;color:#3b82f6;font-weight:600;text-transform:uppercase;letter-spacing:.05em;">📺 Channels</div>';
      html += d.channels.map(c =>
        `<div class="sr" onclick="jumpToChannel(${JSON.stringify(c.id)},${JSON.stringify(c.name)})" style="padding:8px 14px;cursor:pointer;font-size:13px;color:#e2e8f0;border-bottom:1px solid #1e293b;">${esc(c.name)}</div>`
      ).join('');
    }
    if (d.programs && d.programs.length) {
      html += '<div style="padding:6px 12px;font-size:11px;color:#f59e0b;font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-top:4px;">🎬 On Now / Upcoming</div>';
      html += d.programs.map(p =>
        `<div class="sr" onclick="searchOpenProg(${JSON.stringify(p.title).replace(/"/g,'&quot;')})" style="padding:8px 14px;cursor:pointer;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:10px;">
          <span style="font-size:12px;min-width:70px;color:${p.on_now?'#22c55e':'#94a3b8'};font-weight:${p.on_now?'600':'400'};">${esc(p.start_fmt)}</span>
          <span style="flex:1;font-size:13px;color:#e2e8f0;">${esc(p.title)}</span>
          <span style="font-size:11px;color:#64748b;text-align:right;">${esc(p.channel_name)}</span>
        </div>`
      ).join('');
    }
    if (!html) html = '<div style="padding:12px 14px;color:#64748b;font-size:13px;">No results</div>';
    dd.innerHTML = html;
    dd.style.display = 'block';
    // hover highlight
    dd.querySelectorAll('.sr').forEach(el => {
      el.onmouseenter = () => el.style.background = '#1e293b';
      el.onmouseleave = () => el.style.background = '';
    });
  }, 250);
}
function clearSearch() {
  document.getElementById('ch-filter').value = '';
  document.getElementById('search-dropdown').style.display = 'none';
  _chOffset = 0; fetchAndRenderGuide();
}
function jumpToChannel(id, name) {
  document.getElementById('search-dropdown').style.display = 'none';
  document.getElementById('ch-filter').value = name;
  _chOffset = 0; fetchAndRenderGuide();
}
async function searchOpenProg(title) {
  document.getElementById('search-dropdown').style.display = 'none';
  document.getElementById('ch-filter').value = '';
  // Open programme modal directly via prog-info
  const p = { title, channel:'', channel_id:'', start_fmt:'', stop_fmt:'', desc:'' };
  openProg(p);
}
// Close dropdown when clicking outside
document.addEventListener('click', e => {
  if (!e.target.closest('#ch-filter') && !e.target.closest('#search-dropdown'))
    document.getElementById('search-dropdown').style.display = 'none';
});

async function fetchAndRenderGuide() {
  const params = new URLSearchParams();
  if (_guideWindowStart) params.set('start', _guideWindowStart);
  params.set('hours', _guideHours);
  const ch = document.getElementById('ch-filter').value.trim();
  if (ch) params.set('ch', ch);
  const mode = document.getElementById('guide-ch-mode').value;
  if (mode === 'fav')   params.set('fav',   '1');
  if (mode === 'movie') params.set('movie', '1');
  if (mode === 'ps')    params.set('ps',    '1');
  if (mode === 'sd')    params.set('sd',    '1');
  params.set('ch_offset', _chOffset);
  try {
    const r = await fetch('/epg-web/api/guide?' + params);
    const d = await r.json();
    if (d.error) { setGS(d.error,'err'); return; }
    _guideData = d;
    if (!_guideWindowStart) _guideWindowStart = d.window_start;
    renderGuide();
    // Update channel page nav
    const total = d.total_channels || 0;
    const offset = d.ch_offset || 0;
    const cap = 200;
    const pageEl = document.getElementById('ch-page-info');
    const prevEl = document.getElementById('ch-page-prev');
    const nextEl = document.getElementById('ch-page-next');
    if (pageEl) {
      const from = total ? offset + 1 : 0;
      const to   = Math.min(offset + cap, total);
      pageEl.textContent = total > cap ? `Channels ${from}–${to} of ${total}` : '';
      prevEl.style.display = offset > 0 ? '' : 'none';
      nextEl.style.display = offset + cap < total ? '' : 'none';
    }
  } catch(e) { setGS('Failed: '+e.message,'err'); }
}
function chPagePrev() { _chOffset = Math.max(0, _chOffset - 200); fetchAndRenderGuide(); }
function chPageNext() { _chOffset += 200; fetchAndRenderGuide(); }
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

  // Channels already filtered server-side
  const channels = d.channels;

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
        onclick="openProg(${pd.replace(/"/g,'&quot;')})">
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

// ── Programme modal + recording ───────────────────────────────────────────────
let _currentProg = null;
async function openProg(p) {
  hideTip();
  _currentProg = p;
  // Show overlay in loading state
  const overlay = document.getElementById('prog-modal-overlay');
  overlay.style.display = 'flex';
  document.getElementById('pm-loading').style.display = 'block';
  document.getElementById('pm-content').style.display = 'none';
  document.getElementById('pm-status').textContent = '';

  // Check if already being recorded
  const now = Date.now() / 1000;
  const recStatus = await (await fetch('/epg-web/api/record/status')).json();
  const alreadyRec = Object.values(recStatus.recordings || {}).some(r =>
    r.title === p.title && r.channel_id === p.channel_id &&
    Math.abs(r.start_ts - p.start_ts) < 60 &&
    ['queued','scheduled','recording'].includes(r.status)
  );

  // Fetch enriched info
  let info = {};
  try {
    const r  = await fetch(`/epg-web/api/prog-info?title=${encodeURIComponent(p.title)}`);
    if (r.ok) info = await r.json();
  } catch(e) {}

  // Populate modal
  document.getElementById('pm-title').textContent = info.title || p.title;
  document.getElementById('pm-air').textContent   = (p.channel || p.channel_id) + '  ·  ' + p.start_fmt + ' – ' + p.stop_fmt;
  document.getElementById('pm-plot').textContent  = info.plot || p.desc || p.category || '';
  document.getElementById('pm-year').textContent  = info.year || '';
  document.getElementById('pm-rated').textContent = info.rated || '';
  document.getElementById('pm-genre').textContent = info.genre || '';
  document.getElementById('pm-imdb').textContent  = info.imdb_rating ? '★ ' + info.imdb_rating : '';
  document.getElementById('pm-actors').textContent   = info.actors  ? '🎭 ' + info.actors  : '';
  document.getElementById('pm-director').textContent = info.director ? '🎬 ' + info.director : '';

  const libBadge = document.getElementById('pm-library-badge');
  libBadge.style.display = info.in_library ? 'inline-block' : 'none';

  const posterEl = document.getElementById('pm-poster');
  const posterWrap = document.getElementById('pm-poster-wrap');
  if (info.poster) {
    posterEl.src = info.poster;
    posterEl.style.display = 'block';
    posterWrap.style.display = 'block';
  } else {
    posterEl.style.display = 'none';
    posterWrap.style.display = 'none';
  }

  document.getElementById('pm-loading').style.display = 'none';
  document.getElementById('pm-content').style.display = 'block';

  // Fetch future airings
  document.getElementById('pm-next-wrap').style.display    = 'none';
  document.getElementById('pm-airings-wrap').style.display = 'none';
  _nextAiring = null;
  try {
    const ar = await (await fetch(`/epg-web/api/airings?title=${encodeURIComponent(p.title)}`)).json();
    if (ar.airings && ar.airings.length > 0) {
      const recMap = {};
      Object.values(recStatus.recordings || {}).forEach(r => {
        if (['queued','scheduled','recording'].includes(r.status))
          recMap[r.channel_id + '|' + r.start_ts] = true;
      });

      // Find currently-airing primestreams show (for Play), then next future one (for Record)
      const livePS   = ar.airings.find(a => a.can_record && a.on_now);
      const futurePS = ar.airings.find(a => a.can_record && !a.on_now);
      const featPS   = livePS || futurePS;
      if (featPS) {
        _nextAiring = featPS;
        _nextAiring._title = p.title;
        const label = featPS.on_now
          ? `ON NOW  ·  ${featPS.channel_name}  (until ${featPS.stop_fmt})`
          : `${featPS.start_fmt} – ${featPS.stop_fmt}  ·  ${featPS.channel_name}`;
        document.getElementById('pm-next-info').textContent = label;

        // Play button: only when currently airing
        const pBtn = document.getElementById('pm-play-btn');
        pBtn.style.display = featPS.on_now ? '' : 'none';

        // Record button: only for future airings
        const rBtn = document.getElementById('pm-rec-next-btn');
        const key = featPS.channel_id + '|' + featPS.start_ts;
        if (featPS.on_now) {
          rBtn.style.display = 'none';
        } else if (recMap[key]) {
          rBtn.textContent = '✅ Scheduled'; rBtn.disabled = true; rBtn.style.display = '';
        } else {
          rBtn.textContent = '⏱ Record'; rBtn.disabled = false; rBtn.style.display = '';
        }
        document.getElementById('pm-next-wrap').style.display = 'block';
      }

      // Full airings list
      document.getElementById('pm-airings-list').innerHTML = ar.airings.map(a => {
        const key = a.channel_id + '|' + a.start_ts;
        const scheduled = recMap[key];
        return `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #1a2332;font-size:12px;">
          <span style="color:#94a3b8;min-width:170px;">${esc(a.start_fmt)} – ${esc(a.stop_fmt)}</span>
          <span style="color:#64748b;flex:1;">${esc(a.channel_name)}</span>
          ${scheduled
            ? `<span style="color:#22c55e;font-size:11px;">✅</span>`
            : a.can_record
              ? `<button class="btn btn-primary btn-sm" onclick="recordAiring(${JSON.stringify(a).replace(/"/g,'&quot;')},${JSON.stringify(p.title).replace(/"/g,'&quot;')})">⏱</button>`
              : ``
          }
        </div>`;
      }).join('');
      document.getElementById('pm-airings-wrap').style.display = 'block';
    }
  } catch(e) {}
}

let _nextAiring = null;

async function playStream() {
  if (!_nextAiring) return;
  const btn = document.getElementById('pm-play-btn');
  btn.disabled = true; btn.textContent = '▶ Playing…';
  const r = await post('/epg-web/api/play', {channel_id: _nextAiring.channel_id});
  if (r.ok) {
    btn.textContent = '■ Stop'; btn.disabled = false;
    btn.onclick = stopStream;
  } else {
    btn.disabled = false; btn.textContent = '▶ Play';
    document.getElementById('pm-status').textContent = '❌ ' + (r.error || 'VLC failed');
    document.getElementById('pm-status').className = 'status-msg err';
  }
}

async function stopStream() {
  await post('/epg-web/api/play/stop', {});
  const btn = document.getElementById('pm-play-btn');
  btn.textContent = '▶ Play'; btn.disabled = false;
  btn.onclick = playStream;
}

async function recordNext() {
  if (!_nextAiring) return;
  await recordAiring(_nextAiring, _nextAiring._title);
}

async function recordSeries() {
  const title = _currentProg && _currentProg.title;
  if (!title) return;
  const btn = document.getElementById('pm-series-btn');
  btn.disabled = true; btn.textContent = '⏳ Scheduling…';
  const r = await post('/epg-web/api/record/series', {title});
  if (r.ok) {
    btn.textContent = `✅ Series (${r.scheduled} queued)`;
    document.getElementById('pm-status').textContent = `📺 Series recording set for "${title}" — ${r.scheduled} airings queued`;
    document.getElementById('pm-status').className = 'status-msg ok';
    loadSeriesRecordings();
  } else {
    btn.disabled = false; btn.textContent = '📺 Record Series';
    document.getElementById('pm-status').textContent = '❌ ' + (r.error || 'Failed');
    document.getElementById('pm-status').className = 'status-msg err';
  }
}

async function cancelSeries(title) {
  if (!confirm(`Stop recording series "${title}"?`)) return;
  const r = await post('/epg-web/api/record/series/cancel', {title});
  if (r.ok) loadSeriesRecordings();
}

async function loadSeriesRecordings() {
  try {
    const d = await (await fetch('/epg-web/api/record/series')).json();
    const el = document.getElementById('series-list');
    if (!el) return;
    const active = (d.series || []).filter(s => s.active);
    const inactive = (d.series || []).filter(s => !s.active);
    if (!d.series || !d.series.length) {
      el.innerHTML = '<div style="color:#64748b;font-size:13px;">No series recordings set up.</div>';
      return;
    }
    const renderRow = (s) => `
      <div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid #1e293b;">
        <span style="flex:1;font-size:13px;color:${s.active?'#e2e8f0':'#64748b'};">${esc(s.title)}</span>
        <span style="font-size:11px;color:#94a3b8;min-width:90px;">${s.upcoming} upcoming</span>
        ${s.active
          ? `<button class="btn btn-ghost btn-sm" onclick="cancelSeries(${JSON.stringify(s.title).replace(/"/g,'&quot;')})" style="font-size:11px;color:#ef4444;border-color:#ef4444;">❌ Cancel</button>`
          : `<span style="font-size:11px;color:#64748b;">Cancelled</span>`
        }
      </div>`;
    el.innerHTML =
      (active.length ? '<div style="font-size:11px;color:#3b82f6;font-weight:600;margin-bottom:6px;">ACTIVE</div>' + active.map(renderRow).join('') : '') +
      (inactive.length ? '<div style="font-size:11px;color:#64748b;font-weight:600;margin:12px 0 6px;">CANCELLED</div>' + inactive.map(renderRow).join('') : '');
  } catch(e) {}
}

function closeProg() {
  document.getElementById('prog-modal-overlay').style.display = 'none';
  // reset play button for next open (VLC keeps running)
  const btn = document.getElementById('pm-play-btn');
  btn.textContent = '▶ Play'; btn.disabled = false;
  btn.onclick = playStream;
}
async function recordAiring(airing, title) {
  const btn = event.target;
  btn.disabled = true; btn.textContent = '…';
  const r = await post('/epg-web/api/record', {
    title:      title,
    channel_id: airing.channel_id,
    start_ts:   airing.start_ts,
    stop_ts:    airing.stop_ts,
  });
  if (r.ok) {
    btn.textContent = '✅ Scheduled';
    btn.style.background = '#166534';
    document.getElementById('pm-status').textContent = `✅ "${title}" queued`;
    document.getElementById('pm-status').className = 'status-msg ok';
    startRecPoll();
  } else {
    btn.disabled = false; btn.textContent = '⏱ Record';
    document.getElementById('pm-status').textContent = '❌ ' + (r.error || 'Failed');
    document.getElementById('pm-status').className = 'status-msg err';
  }
}

// ── Recordings panel ──────────────────────────────────────────────────────────
let _recPoll = null;
function startRecPoll() {
  if (_recPoll) return;
  _recPoll = setInterval(updateRecPanel, 3000);
  updateRecPanel();
}
async function updateRecPanel() {
  const d = await (await fetch('/epg-web/api/record/status')).json();
  const recs = Object.entries(d.recordings || {});
  if (recs.length === 0) {
    document.getElementById('rec-panel').style.display = 'none';
    return;
  }
  document.getElementById('rec-panel').style.display = 'block';
  const statusIcons = {
    queued:'⏳', scheduled:'⏱', recording:'🔴', converting:'⚙️',
    copying:'📤', done:'✅', done_ts:'✅', error:'❌', cancelled:'🚫'
  };
  document.getElementById('rec-list').innerHTML = recs.map(([id, r]) => {
    const icon = statusIcons[r.status] || '•';
    const active = ['queued','scheduled','recording','converting','copying'].includes(r.status);
    return `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #1e1e1e;font-size:13px;">
      <span style="font-size:16px;">${icon}</span>
      <span style="flex:1;color:#c7d2e7;">${esc(r.title)}</span>
      <span style="color:#64748b;font-size:11px;">${r.status}</span>
      ${active ? `<button class="btn btn-danger btn-sm" onclick="cancelRec('${id}')">■</button>` : ''}
    </div>`;
  }).join('');
  // Stop polling when nothing active
  const anyActive = recs.some(([,r]) => ['queued','scheduled','recording','converting','copying'].includes(r.status));
  if (!anyActive) { clearInterval(_recPoll); _recPoll = null; }
}
async function cancelRec(id) { await post('/epg-web/api/record/cancel', {id}); }

// ── Recommendations ───────────────────────────────────────────────────────────
async function loadRecs() {
  document.getElementById('rec-status').textContent = 'Loading…';
  try {
    const d = await (await fetch('/epg-web/api/recommendations')).json();
    if (d.error) { setEl('rec-status',d.error,'err'); return; }
    const recs = d.recommendations || [];
    setEl('rec-status', recs.length + ' wanted titles','');
    const STATUS_BADGE = {wanted:'badge-record', found:'badge-wl', recorded:'badge-recorded', cancelled:'badge-skipped'};
    const tbody = document.getElementById('rec-body');
    tbody.innerHTML = recs.map(r => {
      const a = r.next_airing;
      return `<tr>
        <td class="title-cell">${esc(r.title)} ${r.year?'<span style="color:#555;font-size:11px;">('+r.year+')</span>':''}
          <span class="badge ${STATUS_BADGE[r.status]||'badge-record'}" style="margin-left:5px;">${esc(r.status||'wanted')}</span>
        </td>
        <td class="ch-cell">${a ? esc(a.channel) : '<span style="color:#333">Not in guide</span>'}</td>
        <td class="time-cell">${a ? esc(a.start_fmt) : ''}</td>
        <td class="act-cell">
          ${a ? `<button class="btn btn-success btn-sm" onclick='addToSchedule(${JSON.stringify(a)})'>+ Schedule</button>` : ''}
          <button class="btn btn-ghost btn-sm" onclick='updateWanted(${r.id},"recorded")'>✅ Got it</button>
          <button class="btn btn-danger btn-sm" onclick='removeWanted(${r.id})'>✕</button>
        </td>
      </tr>`;
    }).join('');
  } catch(e) { setEl('rec-status','Failed: '+e.message,'err'); }
}
async function updateWanted(id, status) {
  await post('/epg-web/api/wanted', {action:'update', id, status});
  loadRecs();
}
async function removeWanted(id) {
  if (!confirm('Remove from wanted list?')) return;
  await post('/epg-web/api/wanted', {action:'remove', id});
  loadRecs();
}
async function addWanted() {
  const title = prompt('Movie/show title:');
  if (!title) return;
  await post('/epg-web/api/wanted', {action:'add', title, type:'movie'});
  loadRecs();
}

// ── Channels ──────────────────────────────────────────────────────────────────
async function loadChannels() {
  const q    = document.getElementById('ch-search').value.trim();
  const fav  = document.getElementById('ch-fav-only').checked ? '1' : '0';
  try {
    const d = await (await fetch(`/epg-web/api/channels?q=${encodeURIComponent(q)}&fav=${fav}`)).json();
    if (d.error) { setEl('ch-status',d.error,'err'); return; }
    setEl('ch-status',`${d.total} channels`,'');
    document.getElementById('ch-grid').innerHTML = d.channels.map((c,i) =>
      `<div class="ch-card ${c.favorite?'ch-fav':''}">
        <span class="ch-num">${c.firestick_no||i+1}</span>
        ${c.favorite?'<span style="color:#fcd34d;margin-right:4px;">★</span>':''}
        ${esc(c.nickname||c.name)}
      </div>`
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
  const statusFilter = document.getElementById('sched-filter') ? document.getElementById('sched-filter').value : '';
  const url = '/epg-web/api/schedule' + (statusFilter ? '?status='+statusFilter : '');
  const d   = await (await fetch(url)).json();
  const sched = d.schedule || [];
  const tbl = document.getElementById('sched-table');
  const emp = document.getElementById('sched-empty');
  if (!sched.length) { tbl.style.display='none'; emp.style.display='block'; return; }
  tbl.style.display='table'; emp.style.display='none';
  const SB = {
    scheduled:'badge-record', recording:'badge-wl',
    completed:'badge-recorded', failed:'badge-skipped',
    cancelled:'badge-skipped', to_record:'badge-record',
    recorded:'badge-recorded', skipped:'badge-skipped'
  };
  document.getElementById('sched-body').innerHTML = sched.map((r,i) => `
    <tr>
      <td class="title-cell">${esc(r.title)}
        ${r.episode_title?`<br><span style="font-size:11px;color:#555;">S${r.season_number||'?'}E${r.episode_number||'?'} ${esc(r.episode_title)}</span>`:''}
      </td>
      <td class="ch-cell">${esc(r.channel)}</td>
      <td class="time-cell">${esc(r.start_time||r.start_fmt||'')}</td>
      <td><span class="badge ${SB[r.status]||''}">${esc(r.status||'')}</span></td>
      <td style="font-size:11px;color:#555;">${esc(r.failure_reason||'')}</td>
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

# ── Startup auto-load ────────────────────────────────────────────────────────

def _startup_load():
    cfg     = load_config()
    db_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    tz_str  = cfg.get('timezone', 'America/New_York')
    sd_user = cfg.get('sd_user', '')
    sd_pass = cfg.get('sd_pass', '')

    # Load whatever's already in guide.db
    if os.path.exists(db_path):
        try:
            count = load_epg_from_db(db_path, tz_str)
            print(f'[startup] Loaded {count} programmes from guide.db')
        except Exception as e:
            print(f'[startup] guide.db load failed: {e}')

    # If SD credentials exist and guide is empty or stale (last entry < 24h from now), auto-fetch
    if sd_user and sd_pass:
        stale = True
        if _epg['programmes']:
            last_ts = _epg['programmes'][-1]['stop_ts']
            stale = last_ts < (time.time() + 86400)  # less than 1 day of future data
        if stale:
            print('[startup] Guide stale — auto-fetching from Schedules Direct…')
            _sd_status['running'] = True
            _sd_status['log']     = []
            _sd_status['result']  = None
            _sd_status['error']   = None
            def _run():
                try:
                    from sd_guide import fetch_sd_guide
                    def log(msg):
                        print(f'[SD] {msg}')
                        _sd_status['log'].append(msg)
                    result = fetch_sd_guide(sd_user, sd_pass, db_path, days=14, log=log)
                    count  = load_epg_from_db(db_path, tz_str)
                    _sd_status['result'] = {**result, 'total_loaded': count}
                    print(f'[startup] SD fetch complete — {count} programmes loaded')
                except Exception as e:
                    _sd_status['error'] = str(e)
                    print(f'[startup] SD fetch error: {e}')
                finally:
                    _sd_status['running'] = False
            threading.Thread(target=_run, daemon=True).start()

_startup_load()

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import webbrowser
    print(f'\n  EPG Manager Web {VERSION}')
    print(  '  ──────────────────────')
    print(  '  Open: http://localhost:5001/epg-web\n')
    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5001/epg-web')).start()
    app.run(host='127.0.0.1', port=5001, debug=False)
