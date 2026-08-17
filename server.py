#!/usr/bin/env python3
"""EPG Manager Web — Guide · Recommendations · Channels · Schedule · Conversions"""
VERSION = "v20260817b"

import hmac, json, os, re, shutil, sqlite3, subprocess, threading, time, uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

BASE_DIR = os.path.abspath(
    os.environ.get('EPG_BASE_DIR', os.path.expanduser('~/epg'))
)

# Always create the recordings table on startup, regardless of how Flask is launched
def _bootstrap():
    try:
        import sqlite3 as _sq3, os as _os, json as _js
        _cfg_path = _os.path.join(BASE_DIR, 'epg_config.json')
        _cfg = _js.load(open(_cfg_path)) if _os.path.exists(_cfg_path) else {}
        _db  = _cfg.get('guide_db_path', _os.path.join(BASE_DIR, 'guide.db'))
        _os.makedirs(_os.path.dirname(_db), exist_ok=True)
        ensure_guide_db(_db)
        _c   = _sq3.connect(_db)
        _c.execute('''CREATE TABLE IF NOT EXISTS recordings (
            rec_id TEXT PRIMARY KEY, title TEXT, channel TEXT, channel_id TEXT,
            start_ts REAL, stop_ts REAL, start_time TEXT,
            status TEXT DEFAULT "queued", failure_reason TEXT, file TEXT,
            created_at TEXT, backend TEXT DEFAULT "local", stream_id TEXT,
            agent_id TEXT, lease_until REAL, heartbeat_at REAL, updated_at TEXT,
            episode_title TEXT, season_num INTEGER, episode_num INTEGER,
            is_series INTEGER DEFAULT 0,
            quality_decision TEXT, result_json TEXT)''')
        for _col, _typedef in [
                ('backend', 'TEXT DEFAULT "local"'), ('stream_id', 'TEXT'),
                ('agent_id', 'TEXT'), ('lease_until', 'REAL'),
                ('heartbeat_at', 'REAL'), ('updated_at', 'TEXT'),
                ('quality_decision', 'TEXT'), ('result_json', 'TEXT'),
                ('episode_title', 'TEXT'), ('season_num', 'INTEGER'), ('episode_num', 'INTEGER'),
                ('is_series', 'INTEGER DEFAULT 0')]:
            try:
                _c.execute(f'ALTER TABLE recordings ADD COLUMN {_col} {_typedef}')
            except _sq3.OperationalError as _e:
                if 'duplicate column name' not in str(_e).lower():
                    raise
        # Migrate guide table — add episode columns if missing (safe no-op if already present)
        for _col, _typedef in [('episode_title', 'TEXT'), ('season_num', 'INTEGER'), ('episode_num', 'INTEGER'), ('prog_type', 'TEXT')]:
            try:
                _c.execute(f'ALTER TABLE guide ADD COLUMN {_col} {_typedef}')
            except Exception:
                pass
        _c.commit(); _c.close()
        print('[bootstrap] recordings table ready')
    except Exception as _e:
        print(f'[bootstrap] recordings table ERROR: {_e}')
app.secret_key = os.urandom(24)

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
        'recording_backend': 'local',
        'recording_agent_token': '',
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

def _channel_match_base(value):
    """Normalize provider/guide channel names, including known rebrands."""
    base = re.sub(r'[^a-z0-9]', '', (value or '').lower())
    base = base.replace('paramountwithshowtime', 'showtime')
    if base.startswith('sho2'):
        base = 'showtime2' + base[4:]
    geographic = ''
    if base.endswith('pacific'):
        base = base[:-7]
        geographic = 'west'
    changed = True
    while changed:
        changed = False
        for suffix in ('us', 'uk', 'za', 'ca', 'au', 'uhd', 'hd', 'sd'):
            if base.endswith(suffix) and len(base) > len(suffix) + 2:
                base = base[:-len(suffix)]
                changed = True
                break
    return base + geographic

def _is_premium_channel(name):
    """Networks normally treated as premium in the Favorites guide section."""
    normalized = _channel_match_base(name)
    premium_prefixes = (
        'hbo', 'showtime', 'starz', 'encore', 'cinemax', 'mgm', 'epix',
    )
    return normalized.startswith(premium_prefixes)


def _is_commercial_free_channel(name):
    """Channels we can reliably treat as commercial-free for movie recording."""
    return _is_premium_channel(name)

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
_ps_channel_cache = {'paths': (), 'loaded_at': 0, 'ids': set()}
_stream_quality_scan = {'running': False, 'completed': 0, 'total': 0}
_stream_quality_scan_lock = threading.Lock()

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
    cache_paths = (guide_db_path, movies_db_path)
    if (_ps_channel_cache['paths'] == cache_paths and
            time.time() - _ps_channel_cache['loaded_at'] < 300):
        return set(_ps_channel_cache['ids'])
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
        # Build lookup dicts
        id_to_norm = {cid: _channel_match_base(cname) for cid, cname in grows}
        id_to_name = {cid: cname or '' for cid, cname in grows}
        name_map = {}
        for cid, cname in grows:
            key = id_to_norm[cid]
            name_map.setdefault(key, set()).add(cid)
            if cid in ps_guide_channels:
                result.add(cid)   # direct match

        def _pick_best(cids, target_norm):
            """When multiple guide channels match one PS stream, pick the closest by name length."""
            return min(cids, key=lambda cid: abs(len(id_to_norm.get(cid, '')) - len(target_norm)))

        # Fallback: normalise Movies.db guide_channel and look up in name_map
        for gc in ps_guide_channels:
            base = _channel_match_base(gc)
            # Exact match on base — pick single best to avoid East/West duplicates
            if base in name_map:
                result.add(_pick_best(name_map[base], base))
                # Schedules Direct often carries a numeric HD/SD sibling while
                # PrimeStreams uses the unsuffixed canonical channel name.
                # _channel_match_base intentionally strips quality suffixes, so
                # also inspect the original display name for those siblings.
                result.update(
                    cid for cid in name_map[base]
                    if re.search(r'\b(?:UHD|HD|SD)\b', id_to_name.get(cid, ''), re.I)
                )
                for quality_variant in (base + 'hd', base + 'sd', base + 'uhd'):
                    if quality_variant in name_map:
                        result.add(_pick_best(name_map[quality_variant], quality_variant))
                continue
            # Prefix match
            for cname_norm, cids in name_map.items():
                if len(cname_norm) >= 3 and len(base) >= 3:
                    if base.startswith(cname_norm) or cname_norm.startswith(base):
                        result.add(_pick_best(cids, base))
        _ps_channel_cache.update({'paths': cache_paths, 'loaded_at': time.time(), 'ids': set(result)})
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
        CREATE TABLE IF NOT EXISTS channel_favorites (
            channel_id TEXT PRIMARY KEY,
            favorite INTEGER NOT NULL DEFAULT 1
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS channel_stream_quality (
            channel_id TEXT PRIMARY KEY,
            width INTEGER, height INTEGER, fps REAL,
            video_codec TEXT, audio_codec TEXT, audio_channels INTEGER,
            bitrate INTEGER, sampled_at REAL, error TEXT
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

_bootstrap()

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
    for row in conn.execute('SELECT title, channel_id, channel_name, start_utc, end_utc, desc, category, episode_title, season_num, episode_num, prog_type FROM guide ORDER BY start_utc'):
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
            'desc':          row['desc'] or '',
            'category':      row['category'] or '',
            'episode_title': row['episode_title'] or '',
            'season_num':    row['season_num'],
            'episode_num':   row['episode_num'],
            'prog_type':     row['prog_type'] or '',
        })

    conn.close()

    # Propagate prog_type + episode data from SD rows to XMLTV rows
    # SD rows have numeric channel_ids; XMLTV rows have domain-style channel_ids
    # Match by title (for prog_type) and title+start_ts±15min (for episode details)
    title_pt   = {}   # title → prog_type
    # title → list of (start_ts, season_num, episode_num, episode_title)
    title_eps  = {}
    for p in programmes:
        if p['prog_type']:
            title_pt[p['title']] = p['prog_type']
        if p.get('season_num') is not None or p.get('episode_title'):
            title_eps.setdefault(p['title'], []).append({
                'ts':  p['start_ts'],
                'sn':  p.get('season_num'),
                'en':  p.get('episode_num'),
                'et':  p.get('episode_title', ''),
            })
    filled_pt = 0; filled_ep = 0
    TOLERANCE = 900  # 15 minutes in seconds
    for p in programmes:
        if not p['prog_type'] and p['title'] in title_pt:
            p['prog_type'] = title_pt[p['title']]
            filled_pt += 1
        if p.get('season_num') is None and not p.get('episode_title'):
            for ep in title_eps.get(p['title'], []):
                if abs(ep['ts'] - p['start_ts']) <= TOLERANCE:
                    p['season_num']    = ep['sn']
                    p['episode_num']   = ep['en']
                    p['episode_title'] = ep['et']
                    filled_ep += 1
                    break
    print(f'[startup] Propagated prog_type={filled_pt}, episode info={filled_ep} XMLTV programmes')

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
_plex_info_cache = {}  # norm_title -> ffprobe result dict
_stream_info_cache = {}  # channel_id -> (timestamp, safe ffprobe result)
_plex_episode_cache = {'root': '', 'loaded_at': 0, 'episodes': set()}
_plex_title_cache = {
    'roots': (), 'loaded_at': 0, 'movies': set(), 'movie_versions': set(),
    'unyearred_movies': set(), 'shows': set(),
}

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
_rec_cancel_events = {}  # rec_id → threading.Event

def _rec_status_base(status):
    """Normalize verbose states such as 'scheduled (12m away)'."""
    return (status or '').split('(', 1)[0].strip().lower()

def _rec_is_active(rec):
    return _rec_status_base(rec.get('status')) in {
        'queued', 'scheduled', 'agent_claimed', 'preflight', 'waiting',
        'recording', 'converting', 'awaiting_transfer', 'transferring', 'copying'
    }

def _recording_backend():
    backend = str(load_config().get('recording_backend', 'local')).strip().lower()
    return backend if backend in ('local', 'agent') else 'local'

def _guide_db_path():
    cfg = load_config()
    return cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))

def _init_recordings_table():
    """Create recordings table in guide.db if it doesn't exist."""
    try:
        conn = sqlite3.connect(_guide_db_path())
        conn.execute('''CREATE TABLE IF NOT EXISTS recordings (
            rec_id      TEXT PRIMARY KEY,
            title       TEXT,
            channel     TEXT,
            channel_id  TEXT,
            start_ts    REAL,
            stop_ts     REAL,
            start_time  TEXT,
            status      TEXT DEFAULT "queued",
            failure_reason TEXT,
            file        TEXT,
            created_at  TEXT
        )''')
        migrations = [
            ('backend', "TEXT DEFAULT 'local'"),
            ('stream_id', 'TEXT'),
            ('agent_id', 'TEXT'),
            ('lease_until', 'REAL'),
            ('heartbeat_at', 'REAL'),
            ('updated_at', 'TEXT'),
            ('quality_decision', 'TEXT'),
            ('result_json', 'TEXT'),
            ('episode_title', 'TEXT'),
            ('season_num', 'INTEGER'),
            ('episode_num', 'INTEGER'),
            ('is_series', 'INTEGER DEFAULT 0'),
        ]
        for column, typedef in migrations:
            try:
                conn.execute(f'ALTER TABLE recordings ADD COLUMN {column} {typedef}')
            except sqlite3.OperationalError as e:
                if 'duplicate column name' not in str(e).lower():
                    raise
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[recdb] init error: {e}')

def _db_upsert_rec(rec_id, rec):
    """Insert or update a recording row in guide.db."""
    try:
        conn = sqlite3.connect(_guide_db_path(), timeout=30)
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('''INSERT INTO recordings
            (rec_id, title, channel, channel_id, start_ts, stop_ts, start_time,
             status, failure_reason, file, created_at, backend, stream_id, updated_at,
             episode_title, season_num, episode_num, is_series)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(rec_id) DO UPDATE SET
              status=excluded.status,
              failure_reason=excluded.failure_reason,
              file=excluded.file,
              backend=excluded.backend,
              stream_id=excluded.stream_id,
              updated_at=excluded.updated_at,
              episode_title=excluded.episode_title,
              season_num=excluded.season_num,
              episode_num=excluded.episode_num,
              is_series=excluded.is_series
        ''', (
            rec_id,
            rec.get('title',''),
            rec.get('channel', rec.get('channel_id','')),
            rec.get('channel_id',''),
            rec.get('start_ts', 0),
            rec.get('stop_ts', 0),
            datetime.fromtimestamp(rec.get('start_ts', 0)).strftime('%Y-%m-%d %H:%M:%S') if rec.get('start_ts') else '',
            rec.get('status','queued'),
            rec.get('failure_reason',''),
            rec.get('file',''),
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            rec.get('backend', 'local'),
            rec.get('stream_id', ''),
            datetime.now(timezone.utc).isoformat(),
            rec.get('episode_title', ''),
            rec.get('season_num'),
            rec.get('episode_num'),
            1 if rec.get('is_series') else 0,
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[recdb] upsert error: {e}')
        return False

def _db_update_rec_status(rec_id, status, failure_reason='', file=''):
    """Update just the status/file of a recording in guide.db."""
    try:
        conn = sqlite3.connect(_guide_db_path(), timeout=30)
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute(
            'UPDATE recordings SET status=?, failure_reason=?, file=? WHERE rec_id=?',
            (status, failure_reason, file, rec_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[recdb] status update error: {e}')
        return False

def _reconcile_stale_recordings():
    """Mark jobs that could not have survived a prior server stop."""
    try:
        now = time.time()
        conn = sqlite3.connect(_guide_db_path(), timeout=30)
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('''UPDATE recordings
                        SET status='queued', failure_reason='Resuming after server restart'
                        WHERE COALESCE(backend,'local')='local'
                        AND status='recording' AND stop_ts > ?''', (now,))
        conn.execute('''UPDATE recordings
                        SET status='failed', failure_reason='Recording interrupted by server stop'
                        WHERE COALESCE(backend,'local')='local'
                        AND status='recording' AND stop_ts <= ?''', (now,))
        conn.execute('''UPDATE recordings
                        SET status='skipped_too_short', failure_reason='Recording window passed while server was stopped'
                        WHERE COALESCE(backend,'local')='local'
                        AND status IN ('queued','scheduled') AND stop_ts <= ?''', (now,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[recdb] stale reconciliation error: {e}')

def _load_pending_recs():
    """On startup, reload queued/scheduled recs from guide.db that haven't aired yet."""
    _init_recordings_table()
    _reconcile_stale_recordings()
    try:
        conn = sqlite3.connect(_guide_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM recordings
               WHERE status IN ('queued','scheduled') AND stop_ts > ?
               AND COALESCE(backend,'local')='local'""",
            (time.time() + 60,)
        ).fetchall()
        agent_rows = conn.execute(
            """SELECT * FROM recordings
               WHERE COALESCE(backend,'local')='agent'
               AND status NOT IN ('done','done_ts','cancelled','failed','error',
                                  'skipped_existing_better','skipped_too_short')
               AND stop_ts > ?""", (time.time(),)
        ).fetchall()
        conn.close()
        for r in rows:
            rec_id = r['rec_id']
            rec = {
                'title':      r['title'],
                'channel_id': r['channel_id'],
                'channel':    r['channel'],
                'start_ts':   r['start_ts'],
                'stop_ts':    r['stop_ts'],
                'status':     'queued',
                'progress':   0,
                'log':        [],
                'pid':        None,
                'file':       r['file'] or None,
                'backend':    'local',
                'stream_id':  r['stream_id'] or '',
                'episode_title': r['episode_title'] or '',
                'season_num': r['season_num'], 'episode_num': r['episode_num'],
                'is_series': bool(r['is_series']),
            }
            with _rec_lock:
                _recs[rec_id] = rec
                _rec_cancel_events[rec_id] = threading.Event()
            t = threading.Thread(target=_run_recording, args=(rec_id,), daemon=True)
            t.start()
        for r in agent_rows:
            rec_id = r['rec_id']
            with _rec_lock:
                _recs[rec_id] = {
                    'title': r['title'], 'channel_id': r['channel_id'],
                    'channel': r['channel'], 'start_ts': r['start_ts'],
                    'stop_ts': r['stop_ts'], 'status': r['status'],
                    'progress': 0, 'log': [], 'pid': None,
                    'file': r['file'] or None, 'backend': 'agent',
                    'stream_id': r['stream_id'] or '',
                    'episode_title': r['episode_title'] or '',
                    'season_num': r['season_num'], 'episode_num': r['episode_num'],
                    'is_series': bool(r['is_series']),
                }
        if rows:
            print(f'[recdb] reloaded {len(rows)} pending recording(s)')
    except Exception as e:
        print(f'[recdb] load error: {e}')

def _stream_url(channel_id):
    """Look up stream_id from Movies.db and build the stream URL.
    Returns (url, error, debug_info) where debug_info is a dict."""
    import re as _re2
    cfg  = load_config()
    debug = {'channel_id': channel_id, 'matched_guide_channel': None, 'stream_id': None, 'method': None}
    rows = db_rows(
        'SELECT stream_id, guide_channel FROM channels WHERE guide_channel=? AND stream_id IS NOT NULL AND stream_id!="" LIMIT 1',
        (channel_id,)
    )
    if rows:
        debug['method'] = 'direct'
        debug['matched_guide_channel'] = rows[0]['guide_channel']
    else:
        # Fallback: look up ALL channel_names for this channel_id from guide.db,
        # then try prefix-matching each against Movies.db guide_channel values.
        # Using all names matters — e.g. channel_id 18086 has both "SHOWX" (short,
        # won't match) and "Showtime Extreme" (long, matches "showtimeextreme.us").
        try:
            gdb_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
            gconn = sqlite3.connect(gdb_path)
            gnames = [r[0] for r in gconn.execute(
                'SELECT DISTINCT channel_name FROM guide WHERE channel_id=?', (channel_id,)
            ).fetchall()]
            gconn.close()
            debug['guide_names'] = gnames
            if gnames:
                mrows = db_rows('SELECT guide_channel, stream_id FROM channels WHERE stream_id IS NOT NULL AND stream_id!=""')
                mc = [(mr, _channel_match_base(mr['guide_channel'])) for mr in mrows]
                best_row = None
                best_diff = float('inf')
                best_name = None
                for name in sorted(gnames, key=len, reverse=True):
                    ch_norm = _channel_match_base(name)
                    if len(ch_norm) < 3:
                        continue
                    for mr, base in mc:
                        if len(base) < 3:
                            continue
                        if base.startswith(ch_norm) or ch_norm.startswith(base):
                            diff = abs(len(base) - len(ch_norm))
                            if diff < best_diff:
                                best_diff = diff
                                best_row = mr
                                best_name = name
                if best_row:
                    rows = [best_row]
                    debug['method'] = f'fuzzy ({best_name})'
                    debug['matched_guide_channel'] = best_row['guide_channel']
        except Exception as ex:
            debug['fuzzy_error'] = str(ex)
    if not rows:
        return None, 'No stream_id found for channel', debug
    sid = rows[0]['stream_id']
    debug['stream_id'] = sid
    url = f"{cfg['epg_url'].rstrip('/')}/live/{cfg['epg_user']}/{cfg['epg_pass']}/{sid}.ts"
    return url, None, debug

def _saved_stream_quality(channel_id):
    """Read a safe cached channel-quality record from guide.db."""
    try:
        conn = sqlite3.connect(_guide_db_path())
        conn.row_factory = sqlite3.Row
        row = conn.execute('''SELECT width,height,fps,video_codec,audio_codec,
                              audio_channels,bitrate,sampled_at
                              FROM channel_stream_quality WHERE channel_id=?''',
                           (channel_id,)).fetchone()
        conn.close()
        return dict(row) if row and row['height'] else None
    except Exception:
        return None

def _refresh_stream_quality_cache():
    """Sample recordable channels in the background after a guide refresh."""
    with _stream_quality_scan_lock:
        if _stream_quality_scan['running']:
            return
        _stream_quality_scan.update({'running': True, 'completed': 0, 'total': 0})
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from recording_agent import probe_media
        cfg = load_config()
        guide_db = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
        movie_db = cfg.get('db_path', '/Volumes/EPG/Movies.db')
        channel_ids = list(get_ps_channel_ids(guide_db, movie_db))
        # Favorite channels are useful first; the rest follow in the same job.
        try:
            favorite_ids = {r['guide_channel'] for r in db_rows(
                'SELECT guide_channel FROM channels WHERE favorite=1 AND guide_channel IS NOT NULL')}
            channel_ids.sort(key=lambda cid: (cid not in favorite_ids, cid))
        except Exception:
            pass
        with _stream_quality_scan_lock:
            _stream_quality_scan['total'] = len(channel_ids)
        def sample(channel_id):
            url, error, _debug = _stream_url(channel_id)
            if error:
                return channel_id, None
            try:
                media = probe_media(url, ffprobe=cfg.get('ffprobe', 'ffprobe'), timeout=15)
                return channel_id, {
                    'width': media.get('width', 0), 'height': media.get('height', 0),
                    'fps': media.get('fps', 0), 'video_codec': (media.get('video_codec') or '').upper(),
                    'audio_codec': (media.get('audio_codec') or '').upper(),
                    'audio_channels': media.get('audio_channels', 0),
                    'bitrate': media.get('total_bitrate') or media.get('video_bitrate') or 0,
                }
            except Exception:
                return channel_id, None
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='epg-quality') as pool:
            futures = [pool.submit(sample, channel_id) for channel_id in channel_ids]
            for future in as_completed(futures):
                channel_id, quality = future.result()
                if quality and quality['height']:
                    try:
                        conn = sqlite3.connect(guide_db, timeout=30)
                        conn.execute('''INSERT INTO channel_stream_quality
                            (channel_id,width,height,fps,video_codec,audio_codec,audio_channels,bitrate,sampled_at,error)
                            VALUES (?,?,?,?,?,?,?,?,?,NULL)
                            ON CONFLICT(channel_id) DO UPDATE SET
                              width=excluded.width,height=excluded.height,fps=excluded.fps,
                              video_codec=excluded.video_codec,audio_codec=excluded.audio_codec,
                              audio_channels=excluded.audio_channels,bitrate=excluded.bitrate,
                              sampled_at=excluded.sampled_at,error=NULL''',
                            (channel_id, quality['width'], quality['height'], quality['fps'],
                             quality['video_codec'], quality['audio_codec'], quality['audio_channels'],
                             quality['bitrate'], time.time()))
                        conn.commit(); conn.close()
                    except Exception as exc:
                        print(f'[stream-quality] save error: {exc}')
                with _stream_quality_scan_lock:
                    _stream_quality_scan['completed'] += 1
        print(f'[stream-quality] refreshed {_stream_quality_scan["completed"]} channel(s)')
    except Exception as exc:
        print(f'[stream-quality] refresh error: {exc}')
    finally:
        with _stream_quality_scan_lock:
            _stream_quality_scan['running'] = False

def _safe_filename(title):
    return re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_')[:60]

def _run_recording(rec_id):
    with _rec_lock:
        rec = _recs.get(rec_id)
        if not rec:
            return
        cancel_event = _rec_cancel_events.setdefault(rec_id, threading.Event())
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
        _db_update_rec_status(rec_id, 'scheduled')
        if cancel_event.wait(wait):
            return

    if cancel_event.is_set():
        return

    # If stop_ts is already past (or less than 60s away), nothing useful to record
    if stop_ts - time.time() < 60:
        with _rec_lock:
            _recs[rec_id].update({'status': 'skipped_too_short',
                                  'log': ['Recording skipped — stop time already passed']})
        _db_update_rec_status(rec_id, 'skipped_too_short', 'Stop time already passed')
        return

    url, err, _dbg = _stream_url(ch_id)
    if err:
        with _rec_lock:
            _recs[rec_id].update({'status': 'error', 'log': [err]})
        _db_update_rec_status(rec_id, 'error', err)
        return

    duration = int(stop_ts - time.time()) + 30  # always relative to now, not scheduled start
    ts_file  = os.path.join(rec_dir, f'{_safe_filename(title)}_{int(start_ts)}.ts')
    mp4_file = ts_file.replace('.ts', '.mp4')

    with _rec_lock:
        _recs[rec_id].update({'status': 'recording', 'file': ts_file})
    _db_update_rec_status(rec_id, 'recording', file=ts_file)

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
        with _rec_lock:
            _recs[rec_id]['pid'] = None

        if cancel_event.is_set():
            _db_update_rec_status(rec_id, 'cancelled', 'Cancelled by user', file=ts_file)
            return

        if proc.returncode != 0:
            with _rec_lock:
                _recs[rec_id]['status'] = 'error'
            _db_update_rec_status(rec_id, 'error', 'ffmpeg non-zero exit')
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
            # Look up proper title + year via OMDB
            plex_title = title
            year = ''
            try:
                omdb_key = cfg.get('omdb_key', '')
                if omdb_key:
                    import urllib.request as _ur, urllib.parse as _up
                    q = _up.quote(title)
                    r = _ur.urlopen(f'http://www.omdbapi.com/?apikey={omdb_key}&t={q}&type=movie', timeout=8)
                    od = json.loads(r.read())
                    if od.get('Response') == 'True':
                        plex_title = od.get('Title', title)
                        year = od.get('Year', '')[:4]
            except Exception:
                pass
            folder_name = f'{plex_title} ({year})' if year else plex_title
            safe_folder = re.sub(r'[<>:"/\\|?*]', '', folder_name)
            # Copy to Plex with folder structure
            if os.path.isdir(plex_dir):
                import shutil
                movie_folder = os.path.join(plex_dir, safe_folder)
                os.makedirs(movie_folder, exist_ok=True)
                safe_title_only = re.sub(r'[<>:"/\\|?*]', '', plex_title)
                dest = os.path.join(movie_folder, f'{safe_title_only}.mp4')
                shutil.copy2(mp4_file, dest)
            with _rec_lock:
                _recs[rec_id].update({'status': 'done', 'file': mp4_file, 'plex_title': folder_name})
            _db_update_rec_status(rec_id, 'done', file=mp4_file)
        else:
            with _rec_lock:
                _recs[rec_id].update({'status': 'done_ts', 'file': ts_file})  # keep .ts if convert failed
            _db_update_rec_status(rec_id, 'done_ts', 'Convert failed — kept .ts', file=ts_file)

    except Exception as e:
        with _rec_lock:
            _recs[rec_id].update({'status': 'error', 'error': str(e)})
        _db_update_rec_status(rec_id, 'error', str(e))

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/epg-web', strict_slashes=False)
def index():
    from flask import make_response
    resp = make_response(render_template_string(HTML, VERSION=VERSION))
    resp.headers['Cache-Control'] = 'no-store'
    return resp

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

@app.route('/epg-web/api/disk')
def api_disk():
    cfg = load_config()
    # Built-in paths from config
    checks = [
        ('Mac (recordings)', cfg.get('rec_path',  os.path.expanduser('~/Movies/Recordings'))),
        ('NAS – Plex',       cfg.get('plex_path', '/Volumes/Plex/Movies')),
        ('NAS – EPG',        cfg.get('guide_path','/Volumes/EPG/guide/guide.xml')),
    ]
    # Add any custom monitored paths
    for cp in cfg.get('disk_custom_paths', []):
        checks.append((cp.get('label','Custom'), cp.get('path','')))
    warn_yellow = int(cfg.get('disk_warn_yellow', 75))
    warn_red    = int(cfg.get('disk_warn_red',    90))
    results = []
    seen = set()
    for label, path in checks:
        if not path:
            continue
        # For NAS paths (/Volumes/X/...) stop walking at /Volumes to avoid
        # falling back to the Mac's root drive when the NAS isn't mounted.
        # For local paths, walk all the way up normally.
        is_nas = path.startswith('/Volumes/')
        stop_at = {'/Volumes'} if is_nas else set()
        p = path
        while p and p not in ('/',) and p not in stop_at and not os.path.exists(p):
            p = os.path.dirname(p)
        if not p or p == '/' or p in stop_at or not os.path.exists(p):
            results.append({'label': label, 'error': 'Not mounted'})
            continue
        try:
            usage = shutil.disk_usage(p)
            df = subprocess.run(['df', p], capture_output=True, text=True)
            lines = df.stdout.strip().splitlines()
            mount = lines[-1].split()[-1] if len(lines) >= 2 else p
            if mount in seen:
                for r in results:
                    if r.get('mount') == mount:
                        r['label'] += f' / {label}'
                continue
            seen.add(mount)
            pct = round(usage.used / usage.total * 100, 1) if usage.total else 0
            results.append({
                'label': label, 'mount': mount,
                'total': usage.total, 'used': usage.used, 'free': usage.free,
                'pct': pct,
            })
        except Exception as e:
            results.append({'label': label, 'error': str(e)})
    return jsonify({'ok': True, 'disks': results,
                    'warn_yellow': warn_yellow, 'warn_red': warn_red})

@app.route('/epg-web/api/config', methods=['GET'])
def api_get_config():
    cfg = load_config()
    secret_keys = ('sd_pass', 'epg_pass', 'omdb_key', 'tmdb_key',
                   'recording_agent_token')
    safe = {key: value for key, value in cfg.items() if key not in secret_keys}
    safe['secrets_configured'] = {
        key: bool(cfg.get(key)) for key in secret_keys
    }
    return jsonify(safe)

@app.route('/epg-web/api/config', methods=['POST'])
def api_post_config():
    # Partial updates preserve credentials and settings not represented by the UI.
    cfg = load_config()
    updates = request.json or {}
    updates.pop('secrets_configured', None)
    cfg.update(updates)
    save_config(cfg)
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
            _schedule_active_series(db_path)
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
        _ps_channel_cache['loaded_at'] = 0
        _schedule_active_series(db_path)
        return jsonify({'ok': True, 'count': count, 'new_rows': new_rows, 'loaded': _epg['loaded']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/epg-web/api/fetch-guide', methods=['POST'])
def api_fetch_guide():
    """Fetch fresh XMLTV from PrimeStreams, save to NAS, then reimport."""
    from urllib import request as urlreq
    cfg      = load_config()
    epg_url  = cfg.get('epg_url',  'http://primestreams.tv:826/').rstrip('/')
    epg_user = cfg.get('epg_user', '')
    epg_pass = cfg.get('epg_pass', '')
    xml_path = cfg.get('guide_path', '/Volumes/EPG/guide/guide.xml')
    db_path  = cfg.get('guide_db_path', '/Volumes/EPG/guide/guide.db')
    tz_str   = cfg.get('timezone', 'America/New_York')
    if not epg_user or not epg_pass:
        return jsonify({'error': 'epg_user / epg_pass not configured'}), 400
    # Save locally (NAS mount may be read-only); use local guide dir
    local_xml = os.path.join(BASE_DIR, 'guide_fetched.xml')
    xmltv_url = f'{epg_url}/xmltv.php?username={epg_user}&password={epg_pass}'
    try:
        print(f'[fetch-guide] Fetching {xmltv_url}')
        req = urlreq.Request(xmltv_url, headers={'User-Agent': 'TiViMate/4.7.0 (Amazon AFTS; Android 9)'})
        with urlreq.urlopen(req, timeout=60) as resp:
            data = resp.read()
        print(f'[fetch-guide] Got {len(data):,} bytes, saving to {local_xml}')
        with open(local_xml, 'wb') as f:
            f.write(data)
        xml_path = local_xml  # import from local copy
        new_rows = import_xml_to_guide_db(xml_path, db_path)
        count    = load_epg_from_db(db_path, tz_str)
        _ps_channel_cache['loaded_at'] = 0
        _schedule_active_series(db_path)
        # The comparison probes real streams, so keep it off the guide-fetch
        # request.  Wanted Plex movies are considered for an automatic upgrade
        # only after the new guide is safely loaded.
        threading.Thread(target=_auto_schedule_wanted_movie_upgrades,
                         name='epg-auto-upgrades', daemon=True).start()
        threading.Thread(target=_refresh_stream_quality_cache,
                         name='epg-stream-quality', daemon=True).start()
        print(f'[fetch-guide] Done: {count} programmes, {new_rows} new rows')
        return jsonify({'ok': True, 'bytes': len(data), 'count': count, 'new_rows': new_rows})
    except Exception as e:
        print(f'[fetch-guide] Error: {e}')
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
    ch_id_filter = request.args.get('ch_id', '').lower()  # exact channel_id match from search
    fav_only   = request.args.get('fav', '0') == '1'
    movie_only = request.args.get('movie', '0') == '1'
    ps_only    = request.args.get('ps',  '0') == '1'
    ps_episode_only = request.args.get('ps_episode', '0') == '1'
    sd_only    = request.args.get('sd',  '0') == '1'
    ps_only = ps_only or ps_episode_only

    # Build allowed channel set from Movies.db if filtering
    allowed_ch_ids = None
    guide_db_path  = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    movies_db_path = cfg.get('db_path', '/Volumes/EPG/Movies.db')
    try:
        gconn = sqlite3.connect(guide_db_path)
        guide_favorites = {r[0] for r in gconn.execute(
            'SELECT channel_id FROM channel_favorites WHERE favorite=1'
        ).fetchall()}
        gconn.close()
    except Exception:
        guide_favorites = set()
    movie_favorites = set()
    if fav_only or movie_only or ps_only:
        if ps_only and not fav_only and not movie_only:
            allowed_ch_ids = get_ps_channel_ids(guide_db_path, movies_db_path)
        else:
            # Get the display names of favorite/movie channels from guide.db
            # by looking up what name each Movies.db guide_channel appears as
            where_parts = []
            if fav_only:   where_parts.append('favorite = 1')
            if movie_only: where_parts.append('is_movie_channel = 1')
            where = (' AND '.join(where_parts) + ' AND ' if where_parts else '') + \
                    'guide_channel IS NOT NULL AND guide_channel != ""'
            rows = db_rows(f'SELECT guide_channel FROM channels WHERE {where}')
            direct_ids = {r['guide_channel'] for r in rows}
            if fav_only:
                movie_favorites = set(direct_ids)
                allowed_ch_ids = set(direct_ids) | guide_favorites
            else:
                allowed_ch_ids = set(direct_ids)
    elif guide_favorites:
        # Needed only to annotate normal guide rows with their star state.
        movie_favorites = set()

    if not movie_favorites:
        movie_favorites = {r['guide_channel'] for r in db_rows(
            'SELECT guide_channel FROM channels WHERE favorite=1 AND guide_channel IS NOT NULL'
        )}

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
        if ps_episode_only and (p.get('season_num') is None or p.get('episode_num') is None):
            continue
        if excluded_ch_ids is not None and p['channel_id'] in excluded_ch_ids:
            continue
        if ch_id_filter and p['channel_id'].lower() != ch_id_filter:
            continue
        if not ch_id_filter and ch_filter and ch_filter not in p['channel'].lower():
            continue
        ch_set.add(p['channel_id'])
        progs_in_window.append({
            'title':         p['title'],
            'channel_id':    p['channel_id'],
            'channel':       p['channel'],
            'start_ts':      p['start_ts'],
            'stop_ts':       p['stop_ts'],
            'start_fmt':     p['start_fmt'],
            'stop_fmt':      p['stop_fmt'],
            'desc':          p['desc'],
            'category':      p['category'],
            'episode_title': p.get('episode_title', ''),
            'season_num':    p.get('season_num'),
            'episode_num':   p.get('episode_num'),
            'prog_type':     p.get('prog_type', ''),
        })

    # Deduplicate channels with the same name — merge SD + primestreams rows into one
    # Prefer the primestreams XML channel_id (non-numeric) as canonical
    import re as _re5
    name_to_canonical = {}   # normalised name → canonical channel_id
    id_to_canonical   = {}   # any channel_id → canonical channel_id

    ordered_channels_raw = [c for c in _epg['channels'] if c['id'] in ch_set]
    if ch_id_filter:
        ordered_channels_raw = [c for c in ordered_channels_raw if c['id'].lower() == ch_id_filter]
    elif ch_filter:
        ordered_channels_raw = [c for c in ordered_channels_raw if ch_filter in c['name'].lower()]

    # When a filter is active (fav/movie/ps), also include matching channels that
    # have NO current programming — they show as empty rows (guide data expired)
    if allowed_ch_ids is not None:
        present_ids = {c['id'] for c in ordered_channels_raw}
        for c in _epg['channels']:
            if c['id'] in allowed_ch_ids and c['id'] not in present_ids:
                if not ch_filter or ch_filter in c['name'].lower():
                    ordered_channels_raw.append(dict(c, no_data=True))

    for c in ordered_channels_raw:
        norm = _re5.sub(r'[^a-z0-9]', '', c['name'].lower())
        if norm not in name_to_canonical:
            name_to_canonical[norm] = c['id']   # first seen becomes canonical
        # Prefer domain-style id (non-numeric) as canonical
        if c['id'].replace('.','').replace('_','').isalpha() or '.' in c['id']:
            name_to_canonical[norm] = c['id']
        id_to_canonical[c['id']] = name_to_canonical[norm]

    # Remap programme channel_ids to canonical and dedupe channel list
    for prog in progs_in_window:
        canon = id_to_canonical.get(prog['channel_id'], prog['channel_id'])
        prog['channel_id'] = canon

    seen_ids = set()
    ordered_channels = []
    for c in ordered_channels_raw:
        canon = id_to_canonical.get(c['id'], c['id'])
        if canon not in seen_ids:
            seen_ids.add(canon)
            ordered_channels.append({
                'id': canon, 'name': c['name'], 'icon': c.get('icon',''),
                'no_data': c.get('no_data', False),
                'favorite': canon in guide_favorites or canon in movie_favorites,
            })

    if fav_only:
        ordered_channels.sort(key=lambda channel: (
            not _is_premium_channel(channel['name']), channel['name'].lower()
        ))
        for channel in ordered_channels:
            channel['favorite_group'] = (
                'Premium favorites' if _is_premium_channel(channel['name'])
                else 'Other favorites'
            )

    ch_offset = int(request.args.get('ch_offset', 0))
    ch_cap    = 200
    total_ch  = len(ordered_channels)
    page_chs  = ordered_channels[ch_offset:ch_offset + ch_cap]
    # Mark channels that can be recorded, without exposing their stream URLs.
    try:
        source_recordable = get_ps_channel_ids(guide_db_path, movies_db_path)
        recordable_ids = {id_to_canonical.get(channel_id, channel_id)
                          for channel_id in source_recordable}
        quality_by_id = {}
        try:
            qconn = sqlite3.connect(guide_db_path)
            qconn.row_factory = sqlite3.Row
            for row in qconn.execute('''SELECT channel_id,width,height,fps,video_codec,
                                        audio_codec,audio_channels,bitrate,sampled_at
                                        FROM channel_stream_quality WHERE height > 0'''):
                quality_by_id[id_to_canonical.get(row['channel_id'], row['channel_id'])] = dict(row)
            qconn.close()
        except Exception:
            pass
        for prog in progs_in_window:
            prog['can_record'] = prog['channel_id'] in recordable_ids
            if prog['can_record'] and prog['channel_id'] in quality_by_id:
                prog['stream_quality'] = quality_by_id[prog['channel_id']]
    except Exception:
        for prog in progs_in_window:
            prog['can_record'] = False

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
    q       = request.args.get('q', '').strip()
    ep_q    = request.args.get('episode', '').strip()  # optional episode title filter
    if len(q) < 2:
        return jsonify({'channels': [], 'programs': []})
    cfg      = load_config()
    db_path  = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    from zoneinfo import ZoneInfo
    local_tz = ZoneInfo(cfg.get('timezone', 'America/New_York'))
    now_utc  = datetime.now(timezone.utc)
    now_str  = now_utc.strftime('%Y%m%d%H%M%S')
    like      = f'%{q}%'          # substring match (for channels)
    like_word = [f'{q}%', f'% {q}%']  # word-boundary: starts title OR follows a space
    results  = {'channels': [], 'programs': []}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Get channel_ids that have a PrimeStreams stream (uses name-based fallback matching)
        playable_ids = get_ps_channel_ids(db_path, cfg.get('db_path', '/Volumes/EPG/Movies.db'))

        # Channel name matches (only channels with current/future programming AND a playable stream)
        ch_rows = conn.execute('''
            SELECT DISTINCT g.channel_id, g.channel_name
            FROM guide g
            WHERE g.channel_name LIKE ? AND g.end_utc > ?
            ORDER BY g.channel_name LIMIT 40
        ''', (like, now_str)).fetchall()
        ch_found = {r['channel_id']: {'id': r['channel_id'], 'name': r['channel_name'], 'fav': False}
                    for r in ch_rows if not playable_ids or r['channel_id'] in playable_ids}

        # Also search Movies.db guide_channel names and include favorite status
        try:
            mdb_path = cfg.get('db_path', '/Volumes/EPG/Movies.db')
            mconn = sqlite3.connect(mdb_path)
            mconn.row_factory = sqlite3.Row
            mrows = mconn.execute(
                '''SELECT guide_channel, nickname, favorite, type FROM channels
                   WHERE (guide_channel LIKE ? OR nickname LIKE ?)
                   AND guide_channel IS NOT NULL LIMIT 40''',
                (like, like)
            ).fetchall()
            for mr in mrows:
                gc = mr['guide_channel']
                fav = bool(mr['favorite'])
                nick = mr['nickname'] or ''
                ch_type = mr['type'] or ''
                if gc in ch_found:
                    ch_found[gc]['fav'] = fav
                elif ch_type == '247' and nick:
                    # 24/7 channels have no guide data — use nickname as display name
                    ch_found[gc] = {'id': gc, 'name': nick, 'fav': fav}
                else:
                    grows = conn.execute(
                        'SELECT DISTINCT channel_id, channel_name FROM guide WHERE channel_id=? AND end_utc > ? LIMIT 1',
                        (gc, now_str)
                    ).fetchall()
                    for gr in grows:
                        if gr['channel_id'] not in ch_found:
                            ch_found[gr['channel_id']] = {'id': gr['channel_id'], 'name': gr['channel_name'], 'fav': fav}
            mconn.close()
        except Exception:
            pass

        # Deduplicate by display name — keep favorite if available, else shortest channel_id
        deduped = {}
        for ch in ch_found.values():
            name = ch['name'].upper().strip()
            if name not in deduped or (ch['fav'] and not deduped[name]['fav']) or \
               (ch['fav'] == deduped[name]['fav'] and len(ch['id']) < len(deduped[name]['id'])):
                deduped[name] = ch
        results['channels'] = sorted(deduped.values(), key=lambda x: x['name'])[:20]

        # Program title matches — one row per channel airing it, current/upcoming only
        season_filter = request.args.get('season', '').strip()
        ep_filter     = request.args.get('ep', '').strip()

        extra_where = ''
        params = [like_word[0], like_word[1], now_str]
        if ep_q:
            extra_where += ' AND (episode_title LIKE ? OR episode_title IS NULL)'
            params.append(f'%{ep_q}%')
        if season_filter:
            extra_where += ' AND (season_num=? OR season_num IS NULL)'
            try: params.append(int(season_filter))
            except ValueError: pass
        if ep_filter:
            extra_where += ' AND (episode_num=? OR episode_num IS NULL)'
            try: params.append(int(ep_filter))
            except ValueError: pass

        prog_rows = conn.execute(f'''
            SELECT title, channel_id, channel_name, start_utc, end_utc, category,
                   episode_title, season_num, episode_num
            FROM guide
            WHERE (title LIKE ? OR title LIKE ?) AND end_utc > ?{extra_where}
            ORDER BY start_utc
            LIMIT 40
        ''', params).fetchall()

        programs = []
        for r in prog_rows:
            try:
                su = datetime.strptime(r['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                eu = datetime.strptime(r['end_utc'],   '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                sl = su.astimezone(local_tz)
                el = eu.astimezone(local_tz)
                on_now = su <= now_utc < eu
                programs.append({
                    'title':          r['title'],
                    'channel_id':     r['channel_id'],
                    'channel':        r['channel_name'],
                    'channel_name':   r['channel_name'],
                    'start_fmt':    ('ON NOW' if on_now else sl.strftime('%a %-I:%M %p')),
                    'stop_fmt':     el.strftime('%a %-I:%M %p'),
                    'start_ts':     su.timestamp(),
                    'stop_ts':      eu.timestamp(),
                    'category':     r['category'] or '',
                    'on_now':       on_now,
                    'episode_title': r['episode_title'] or '',
                    'season_num':   r['season_num'],
                    'episode_num':  r['episode_num'],
                    'has_stream':   r['channel_id'] in playable_ids,
                })
            except Exception:
                continue
        # Deduplicate: one result per (start_utc, channel_name) — prefer PS-streamable
        seen = {}
        for p in programs:
            key = (p['start_ts'], p['channel_name'].upper().strip())
            if key not in seen or (p['has_stream'] and not seen[key]['has_stream']):
                seen[key] = p
        results['programs'] = list(seen.values())
        conn.close()
    except Exception as e:
        print(f'[search] {e}')
    return jsonify(results)

@app.route('/epg-web/api/channel/favorite', methods=['POST'])
def api_toggle_favorite():
    data = request.json or {}
    channel_id = str(data.get('channel_id', '')).strip()
    if not channel_id:
        return jsonify({'error': 'no channel_id'}), 400
    cfg = load_config()
    guide_db_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    mdb_path = cfg.get('db_path', '/Volumes/EPG/Movies.db')
    try:
        ensure_guide_db(guide_db_path)
        gconn = sqlite3.connect(guide_db_path)
        row = gconn.execute(
            'SELECT favorite FROM channel_favorites WHERE channel_id=?', (channel_id,)
        ).fetchone()
        # Preserve the existing Movies.db favourite state when this is the
        # first time a legacy channel is toggled from the guide.
        legacy_fav = False
        try:
            mconn = sqlite3.connect(mdb_path)
            mrow = mconn.execute(
                'SELECT favorite FROM channels WHERE guide_channel=?', (channel_id,)
            ).fetchone()
            legacy_fav = bool(mrow and mrow[0])
            new_fav = 0 if (bool(row and row[0]) or (row is None and legacy_fav)) else 1
            if mrow is not None:
                mconn.execute('UPDATE channels SET favorite=? WHERE guide_channel=?', (new_fav, channel_id))
                mconn.commit()
            mconn.close()
        except Exception:
            new_fav = 0 if bool(row and row[0]) else 1
        gconn.execute(
            'INSERT INTO channel_favorites(channel_id, favorite) VALUES(?,?) '
            'ON CONFLICT(channel_id) DO UPDATE SET favorite=excluded.favorite',
            (channel_id, new_fav),
        )
        gconn.commit()
        gconn.close()
        return jsonify({'ok': True, 'favorite': bool(new_fav)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/epg-web/api/channel/hide', methods=['POST'])
def api_toggle_hide():
    data = request.json or {}
    channel_id = data.get('channel_id', '')
    hide = data.get('hide')  # True=hide, False=restore, None=toggle
    if not channel_id:
        return jsonify({'error': 'no channel_id'}), 400
    cfg = load_config()
    mdb_path = cfg.get('db_path', '/Volumes/EPG/Movies.db')
    try:
        mconn = sqlite3.connect(mdb_path)
        row = mconn.execute('SELECT is_bad FROM channels WHERE channel_id=?', (channel_id,)).fetchone()
        if row is None:
            mconn.close()
            return jsonify({'error': 'channel not found'}), 404
        new_bad = 1 if (hide if hide is not None else not bool(row[0])) else 0
        mconn.execute('UPDATE channels SET is_bad=? WHERE channel_id=?', (new_bad, channel_id))
        mconn.commit()
        mconn.close()
        return jsonify({'ok': True, 'hidden': bool(new_bad)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/epg-web/api/sync-streams', methods=['POST'])
def api_sync_streams():
    """Fetch current stream IDs from PrimeStreams and update Movies.db."""
    import re as _re
    from urllib import request as urlreq
    cfg      = load_config()
    base     = cfg['epg_url'].rstrip('/')
    user     = cfg.get('epg_user', '')
    passwd   = cfg.get('epg_pass', '')
    mdb_path = cfg.get('db_path', '/Volumes/EPG/Movies.db')

    def _norm(s):
        return _re.sub(r'[^a-z0-9]', '', s.lower())
    def _base(norm):
        for sfx in ('us','uk','za','ca','au','hd','sd','west','east','vip','uhd'):
            if norm.endswith(sfx):
                return norm[:-len(sfx)]
        return norm

    # 1. Fetch live streams from PrimeStreams
    try:
        api_url = f"{base}/player_api.php?username={user}&password={passwd}&action=get_live_streams"
        with urlreq.urlopen(api_url, timeout=15) as r:
            ps_streams = json.loads(r.read())
    except Exception as e:
        return jsonify({'error': f'PrimeStreams API error: {e}'}), 500

    # Build normalized-name → (stream_id, name) map; prefer exact over prefix
    ps_map = {}
    for s in ps_streams:
        sid  = str(s.get('stream_id',''))
        name = s.get('name','').strip()
        # Strip prefixes like "UK: ", "UHD: "
        name = _re.sub(r'^(UK|UHD|HD|SD):\s*', '', name, flags=_re.IGNORECASE).strip()
        # Strip suffixes like "| VIP", "UHD/4K", "4K"
        name = _re.sub(r'\s*[\|]\s*VIP.*$', '', name).strip()
        name = _re.sub(r'\s*(UHD/4K|4K|UHD)$', '', name).strip()
        key  = _base(_norm(name))
        if key and key not in ps_map:
            ps_map[key] = (sid, name)

    # 2. Load all channels from Movies.db
    try:
        mconn = sqlite3.connect(mdb_path)
        mconn.row_factory = sqlite3.Row
        ch_rows = mconn.execute('SELECT guide_channel, stream_id FROM channels WHERE guide_channel IS NOT NULL').fetchall()
    except Exception as e:
        return jsonify({'error': f'Movies.db error: {e}'}), 500

    updated, not_found, unchanged = [], [], []
    for row in ch_rows:
        gc   = row['guide_channel']
        old  = str(row['stream_id'] or '')
        key  = _base(_norm(gc))
        match = ps_map.get(key)
        if not match:
            # Try prefix match
            for ps_key, (sid, pname) in ps_map.items():
                if len(key) >= 4 and len(ps_key) >= 4:
                    if key.startswith(ps_key) or ps_key.startswith(key):
                        match = (sid, pname)
                        break
        if match:
            new_sid, ps_name = match
            if new_sid != old:
                mconn.execute('UPDATE channels SET stream_id=? WHERE guide_channel=?', (new_sid, gc))
                updated.append({'channel': gc, 'old': old, 'new': new_sid, 'ps_name': ps_name})
            else:
                unchanged.append(gc)
        else:
            not_found.append(gc)

    mconn.commit()
    mconn.close()
    return jsonify({'ok': True, 'updated': updated, 'unchanged': len(unchanged), 'not_found': len(not_found)})

@app.route('/epg-web/api/247channels')
def api_247channels():
    q = request.args.get('q', '').lower().strip()
    fav_only = request.args.get('fav', '') == '1'
    try:
        cfg = load_config()
        mdb_path = cfg.get('db_path', '/Volumes/EPG/Movies.db')
        mconn = sqlite3.connect(mdb_path)
        mconn.row_factory = sqlite3.Row
        show_hidden = request.args.get('show_hidden', '') == '1'
        where = "type='247'" if show_hidden else "type='247' AND (is_bad=0 OR is_bad IS NULL)"
        rows = mconn.execute(
            f"SELECT channel_id, nickname, stream_id, favorite, is_bad, sd_station_id FROM channels WHERE {where} ORDER BY nickname"
        ).fetchall()
        mconn.close()
    except Exception as e:
        return jsonify({'error': str(e), 'channels': []})
    channels = []
    for r in rows:
        name = r['nickname'] or r['channel_id']
        if q and q not in name.lower():
            continue
        fav = bool(r['favorite'])
        if fav_only and not fav:
            continue
        display = re.sub(r'^24/7\s+', '', name, flags=re.IGNORECASE)
        subtype = r['sd_station_id'] or 'tv'
        channels.append({'id': r['channel_id'], 'name': display, 'stream_id': r['stream_id'],
                         'fav': fav, 'hidden': bool(r['is_bad']), 'subtype': subtype})
    channels.sort(key=lambda c: (not c['fav'], c['name']))
    return jsonify({'channels': channels, 'total': len(channels)})

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
    # Legacy scheduled_recordings from Movies.db (NAS)
    if status_filter:
        legacy = db_rows('SELECT * FROM scheduled_recordings WHERE status=? ORDER BY start_time DESC LIMIT 500', (status_filter,))
    else:
        legacy = db_rows('SELECT * FROM scheduled_recordings ORDER BY start_time DESC LIMIT 500')
    # Local recordings from guide.db (survive restarts)
    try:
        conn = sqlite3.connect(_guide_db_path())
        conn.row_factory = sqlite3.Row
        if status_filter:
            local = [dict(r) for r in conn.execute(
                'SELECT * FROM recordings WHERE status=? ORDER BY start_ts DESC LIMIT 500', (status_filter,)
            ).fetchall()]
        else:
            local = [dict(r) for r in conn.execute(
                'SELECT * FROM recordings ORDER BY start_ts DESC LIMIT 500'
            ).fetchall()]
        conn.close()
    except Exception:
        local = []
    # Merge: local first (most recent/relevant), then legacy
    seen = {r.get('title','') + str(r.get('start_ts','')) for r in local}
    merged = local + [r for r in legacy if r.get('title','') + str(r.get('start_ts','')) not in seen]
    json_sched = load_schedule()
    return jsonify({'schedule': merged, 'pending': json_sched})

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

def _auto_schedule_wanted_movie_upgrades():
    """Queue objectively better clean-airing replacements for wanted movies.

    This runs after a guide refresh.  It never records a title absent from the
    user's Wanted list, never uses an ad-supported channel, and leaves the
    recording agent to make the same comparison again immediately before both
    recording and transfer.
    """
    try:
        from recording_agent import best_existing_copy, find_plex_candidates, probe_media, quality_decision
        cfg = load_config()
        guide_db = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
        movie_root = cfg.get('plex_path', '/Volumes/Plex/Movies')
        if not os.path.isdir(movie_root):
            print('[auto-upgrade] Plex Movies is not mounted; skipping')
            return
        wanted = [dict(r) for r in db_rows(
            "SELECT title, year FROM wanted_titles WHERE type='movie' AND lower(COALESCE(status, '')) NOT IN ('removed', 'disabled')"
        )]
        if not wanted:
            return
        conn = sqlite3.connect(guide_db)
        conn.row_factory = sqlite3.Row
        now_key = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        for item in wanted:
            title, year = item['title'], str(item.get('year') or '').strip()
            # Do not schedule a duplicate if this guide refresh is repeated.
            with _rec_lock:
                if any(_rec_is_active(r) and _norm_plex_show(r.get('title', '')) == _norm_plex_show(title)
                       for r in _recs.values()):
                    continue
            candidates = find_plex_candidates(movie_root, title)
            if re.fullmatch(r'\d{4}', year):
                candidates = [p for p in candidates if re.search(
                    rf'\({re.escape(year)}\)$', os.path.basename(os.path.dirname(p)))]
            _existing_path, existing = best_existing_copy(candidates, cfg.get('ffprobe', 'ffprobe'))
            if not existing:
                continue
            clean_title = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
            rows = conn.execute('''SELECT channel_id, channel_name, start_utc, end_utc
                FROM guide WHERE (lower(title)=lower(?) OR lower(title)=lower(?))
                AND start_utc>? ORDER BY start_utc LIMIT 60''',
                (title, clean_title, now_key)).fetchall()
            candidate = next((row for row in rows if _is_commercial_free_channel(row['channel_name'])), None)
            if not candidate:
                continue
            url, stream_error, _debug = _stream_url(candidate['channel_id'])
            if stream_error:
                continue
            incoming = probe_media(url, ffprobe=cfg.get('ffprobe', 'ffprobe'), timeout=45)
            better, decision = quality_decision(existing, incoming)
            if not better:
                continue
            start = datetime.strptime(candidate['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            stop = datetime.strptime(candidate['end_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            # Reuse the normal queueing path so stream mapping, persistence,
            # deduplication, and the recording backend all behave identically.
            with app.test_request_context('/epg-web/api/record', method='POST', json={
                'title': title, 'channel_id': candidate['channel_id'],
                'start_ts': start.timestamp(), 'stop_ts': stop.timestamp(),
            }):
                queued = api_record().get_json()
            if queued.get('ok') and not queued.get('dup'):
                print(f'[auto-upgrade] Scheduled {title}: {decision}')
        conn.close()
    except Exception as exc:
        print(f'[auto-upgrade] Error: {exc}')

@app.route('/epg-web/api/recommendations')
def api_recommendations():
    # Wanted titles from DB cross-referenced with guide
    wanted = db_rows('SELECT * FROM wanted_titles ORDER BY status, title')
    now_ts = datetime.now(timezone.utc).timestamp()

    # Build all future airings by title. Movies only surface a known
    # commercial-free channel; series keep every option for the user to choose.
    future_airings = {}
    if _epg['programmes']:
        for p in _epg['programmes']:
            if p['stop_ts'] <= now_ts:
                continue
            t = p['title'].lower()
            future_airings.setdefault(t, []).append(p)

    plex_titles = _plex_wanted_title_index()
    with _rec_lock:
        active_upgrade_titles = {
            _norm_plex_show(r.get('title', '')) for r in _recs.values()
            if _rec_is_active(r)
        }
    result = []
    for w in wanted:
        candidates = (future_airings.get(w['title'].lower()) or
                      future_airings.get(w['normalized_title'].lower() if w['normalized_title'] else '') or [])
        is_series_wanted = w['type'] == 'series'
        airing = next((p for p in candidates if is_series_wanted or
                       _is_commercial_free_channel(p.get('channel', ''))), None)
        title_key = _norm_plex_show(w['title'])
        wanted_year = str(w['year'] or '').strip()
        in_movies = (title_key in plex_titles['movies'] if not wanted_year else
                     ((title_key, wanted_year) in plex_titles.get('movie_versions', set()) or
                      title_key in plex_titles.get('unyearred_movies', set())))
        in_shows = title_key in plex_titles['shows']
        result.append({
            'id':         w['id'],
            'title':      w['title'],
            'year':       w['year'],
            'type':       w['type'],
            'commercial_free_only': not is_series_wanted,
            'status':     w['status'],
            'notes':      w['notes'],
            'source':     w['source'],
            'imdb_id':    w['imdb_id'],
            'updated_at': w['updated_at'],
            'next_airing': airing,
            'in_plex': in_movies or in_shows,
            'upgrade_scheduled': bool(in_movies and title_key in active_upgrade_titles),
            'plex_kind': ('Movie' if in_movies else '') + (' & TV' if in_movies and in_shows else 'TV' if in_shows else ''),
        })
    return jsonify({'recommendations': result})


@app.route('/epg-web/api/recommendations/movie-upgrade')
def api_recommendation_movie_upgrade():
    """Compare the next clean movie airing against its existing Plex copy."""
    title = request.args.get('title', '').strip()
    year = request.args.get('year', '').strip()
    if not title:
        return jsonify({'error': 'title is required'}), 400
    cfg = load_config()
    db_path = cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    movie_root = cfg.get('plex_path', '/Volumes/Plex/Movies')
    clean_title = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
    try:
        from recording_agent import (best_existing_copy, find_plex_candidates,
                                     probe_media, quality_decision)
        plex_candidates = find_plex_candidates(movie_root, title)
        if re.fullmatch(r'\d{4}', year):
            plex_candidates = [path for path in plex_candidates if re.search(
                rf'\({re.escape(year)}\)$', os.path.basename(os.path.dirname(path))
            )]
        existing_path, existing = best_existing_copy(plex_candidates, cfg.get('ffprobe', 'ffprobe'))
        if not existing:
            return jsonify({'error': 'No Plex movie copy found'}), 404
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''SELECT channel_id, channel_name, start_utc, end_utc
            FROM guide WHERE (lower(title)=lower(?) OR lower(title)=lower(?))
            AND end_utc>? ORDER BY start_utc LIMIT 60''', (
                title, clean_title, datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
            )).fetchall()
        conn.close()
        candidate = next((row for row in rows if _is_commercial_free_channel(
            row['channel_name'])), None)
        if not candidate:
            return jsonify({'better': False, 'decision': 'No future commercial-free airing found'})
        url, stream_error, _debug = _stream_url(candidate['channel_id'])
        if stream_error:
            return jsonify({'better': False, 'decision': 'The next clean airing is not recordable'})
        incoming = probe_media(url, ffprobe=cfg.get('ffprobe', 'ffprobe'), timeout=45)
        better, decision = quality_decision(existing, incoming)
        start = datetime.strptime(candidate['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        stop = datetime.strptime(candidate['end_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        return jsonify({
            'better': better, 'decision': decision,
            'airing': {
                'channel_id': candidate['channel_id'],
                'channel_name': candidate['channel_name'],
                'start_ts': start.timestamp(), 'stop_ts': stop.timestamp(),
                'episode_title': '', 'season_num': None, 'episode_num': None,
            },
        })
    except Exception as exc:
        return jsonify({'error': f'Unable to compare: {exc}'}), 502


def _plex_wanted_title_index():
    """Cache top-level Plex movie/show names for the Wanted Titles badges."""
    cfg = load_config()
    movie_root = cfg.get('plex_path', '/Volumes/Plex/Movies')
    tv_root = _plex_tv_path(cfg)
    roots = (movie_root, tv_root)
    now = time.time()
    if (_plex_title_cache['roots'] == roots and now - _plex_title_cache['loaded_at'] < 300):
        return _plex_title_cache
    movies, movie_versions, unyearred_movies, shows = set(), set(), set(), set()
    try:
        for entry in os.scandir(movie_root):
            if entry.name.startswith('.'):
                continue
            title = entry.name if entry.is_dir() else os.path.splitext(entry.name)[0]
            match = re.match(r'^(.*?)\s*\((\d{4})\)\s*$', title)
            year = match.group(2) if match else ''
            title = (match.group(1) if match else title).strip()
            if title:
                key = _norm_plex_show(title)
                movies.add(key)
                if year:
                    movie_versions.add((key, year))
                else:
                    unyearred_movies.add(key)
    except OSError:
        pass
    try:
        for entry in os.scandir(tv_root):
            if entry.is_dir() and not entry.name.startswith(('.', '_')):
                shows.add(_norm_plex_show(entry.name))
    except OSError:
        pass
    _plex_title_cache.update({
        'roots': roots, 'loaded_at': now, 'movies': movies,
        'movie_versions': movie_versions, 'unyearred_movies': unyearred_movies,
        'shows': shows,
    })
    return _plex_title_cache

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

@app.route('/epg-web/api/plex/titles')
def api_plex_titles():
    cfg = load_config()
    plex_dir = cfg.get('plex_path', '/Volumes/Plex-1/Movies')
    if not os.path.isdir(plex_dir):
        return jsonify({'titles': []})
    titles = []
    for name in os.listdir(plex_dir):
        if os.path.isdir(os.path.join(plex_dir, name)):
            clean = re.sub(r'\s*\(\d{4}\)\s*$', '', name).strip()
            if clean:
                titles.append(clean)
    return jsonify({'titles': titles})

def _plex_tv_path(cfg):
    return cfg.get('plex_tv_path') or os.path.join(
        os.path.dirname(cfg.get('plex_path', '/Volumes/Plex/Movies')), 'TV Shows'
    )

def _norm_plex_show(value):
    return re.sub(r'[^a-z0-9]', '', (value or '').lower())


def _plex_title_and_year(value):
    match = re.match(r'^(.*?)\s*\((\d{4})\)\s*$', (value or '').strip())
    return (match.group(1).strip(), match.group(2)) if match else ((value or '').strip(), '')

def _plex_episode_keys(tv_root):
    """Return cached show|season|episode keys from Plex's TV Shows layout."""
    now = time.time()
    if (_plex_episode_cache['root'] == tv_root and
            now - _plex_episode_cache['loaded_at'] < 300):
        return _plex_episode_cache['episodes']
    episodes = set()
    if os.path.isdir(tv_root):
        ep_re = re.compile(r'\bS(\d{1,2})E(\d{1,3})\b', re.I)
        try:
            for show in os.scandir(tv_root):
                if not show.is_dir() or show.name.startswith('.'):
                    continue
                show_key = _norm_plex_show(show.name)
                for root, _dirs, files in os.walk(show.path):
                    for filename in files:
                        if os.path.splitext(filename)[1].lower() not in {'.mp4', '.mkv', '.m4v'}:
                            continue
                        match = ep_re.search(filename)
                        if match:
                            episodes.add(f'{show_key}|{int(match.group(1))}|{int(match.group(2))}')
        except OSError:
            pass
    _plex_episode_cache.update({'root': tv_root, 'loaded_at': now, 'episodes': episodes})
    return episodes

@app.route('/epg-web/api/plex/episodes')
def api_plex_episodes():
    cfg = load_config()
    return jsonify({'episodes': sorted(_plex_episode_keys(_plex_tv_path(cfg)))})

@app.route('/epg-web/api/plex/info')
def api_plex_info():
    title = request.args.get('title', '').strip()
    if not title:
        return jsonify({'error': 'no title'}), 400
    def _norm(t):
        return re.sub(r'[^a-z0-9]', '', t.lower())
    clean_title, requested_year = _plex_title_and_year(title)
    cache_key = _norm(title)
    if cache_key in _plex_info_cache:
        return jsonify(_plex_info_cache[cache_key])
    cfg = load_config()
    plex_dir = cfg.get('plex_path', '/Volumes/Plex-1/Movies')
    if not os.path.isdir(plex_dir):
        return jsonify({'found': False, 'error': 'plex not mounted'})
    norm_title = _norm(clean_title)
    matched_folder = None
    for name in os.listdir(plex_dir):
        fp = os.path.join(plex_dir, name)
        if not os.path.isdir(fp):
            continue
        folder_title, folder_year = _plex_title_and_year(name)
        if (_norm(folder_title) == norm_title and
                (not requested_year or not folder_year or folder_year == requested_year)):
            matched_folder = fp
            break
    if not matched_folder:
        return jsonify({'found': False})
    mp4_file = next((os.path.join(matched_folder, f)
                     for f in os.listdir(matched_folder) if f.endswith('.mp4')), None)
    if not mp4_file:
        return jsonify({'found': True, 'error': 'no mp4'})
    size_gb = os.path.getsize(mp4_file) / (1024**3)
    result = {'found': True, 'size': f'{size_gb:.2f} GB', 'file': os.path.basename(mp4_file)}
    try:
        import subprocess as _sp
        probe = _sp.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', mp4_file],
            capture_output=True, text=True, timeout=10)
        if probe.returncode == 0:
            data = json.loads(probe.stdout)
            for s in data.get('streams', []):
                if s.get('codec_type') == 'video':
                    fps = ''
                    fs = s.get('avg_frame_rate') or s.get('r_frame_rate', '')
                    if fs and '/' in fs:
                        n, d = fs.split('/')
                        if int(d): fps = f'{int(n)/int(d):.3f}'.rstrip('0').rstrip('.')
                    result.update({'width': s.get('width'), 'height': s.get('height'),
                                   'video_codec': s.get('codec_name','').upper(), 'fps': fps})
                elif s.get('codec_type') == 'audio' and 'audio_codec' not in result:
                    result['audio_codec'] = s.get('codec_name','').upper()
                    result['channels'] = s.get('channels', '')
    except Exception as e:
        result['ffprobe_error'] = str(e)
    _plex_info_cache[cache_key] = result
    return jsonify(result)

@app.route('/epg-web/api/stream-info')
def api_stream_info():
    """Return safe technical details for a recordable incoming channel.

    The stream address contains credentials, so it is intentionally never
    returned to the browser.  Results are cached briefly: opening several
    programme windows on the same channel should not repeatedly probe it.
    """
    channel_id = request.args.get('channel_id', '').strip()
    if not channel_id:
        return jsonify({'error': 'no channel_id'}), 400
    cached = _stream_info_cache.get(channel_id)
    if cached and time.time() - cached[0] < 300:
        return jsonify(cached[1])
    saved = _saved_stream_quality(channel_id)
    if saved:
        _stream_info_cache[channel_id] = (time.time(), saved)
        return jsonify(saved)
    url, error, _debug = _stream_url(channel_id)
    if error:
        return jsonify({'error': error}), 404
    try:
        from recording_agent import probe_media
        cfg = load_config()
        media = probe_media(url, ffprobe=cfg.get('ffprobe', cfg.get('ffprobe_path', 'ffprobe')), timeout=15)
        result = {
            'width': media.get('width', 0), 'height': media.get('height', 0),
            'fps': media.get('fps', 0),
            'video_codec': (media.get('video_codec') or '').upper(),
            'audio_codec': (media.get('audio_codec') or '').upper(),
            'audio_channels': media.get('audio_channels', 0),
            'bitrate': media.get('total_bitrate') or media.get('video_bitrate') or 0,
        }
    except Exception as exc:
        return jsonify({'error': 'Unable to inspect the incoming stream', 'detail': str(exc)}), 502
    _stream_info_cache[channel_id] = (time.time(), result)
    return jsonify(result)

@app.route('/epg-web/api/plex/play', methods=['POST'])
def api_plex_play():
    title = (request.json or {}).get('title', '').strip()
    if not title:
        return jsonify({'error': 'no title'}), 400
    cfg = load_config()
    plex_dir = cfg.get('plex_path', '/Volumes/Plex/Movies')
    def _norm(t):
        return re.sub(r'[^a-z0-9 ]', '', t.lower()).strip()
    norm_title = _norm(title)
    mp4_file = None
    for name in os.listdir(plex_dir):
        fp = os.path.join(plex_dir, name)
        if not os.path.isdir(fp):
            continue
        folder_title = re.sub(r'\s*\(\d{4}\)\s*$', '', name).strip()
        if _norm(folder_title) == norm_title:
            for f in os.listdir(fp):
                if f.endswith('.mp4'):
                    mp4_file = os.path.join(fp, f)
                    break
            break
    if not mp4_file:
        return jsonify({'error': 'file not found in Plex'}), 404
    try:
        subprocess.Popen(['open', '-a', 'VLC', mp4_file])
        return jsonify({'ok': True, 'file': mp4_file})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
    title    = request.args.get('title', '').strip()
    year     = request.args.get('year', '').strip()
    desc     = request.args.get('desc', '').strip()
    category = request.args.get('category', '').strip().lower()
    content_type = request.args.get('content_type', '').strip().lower()
    if content_type not in ('movie', 'series'):
        content_type = ''
    if not title:
        return jsonify({'error': 'No title'}), 400

    # Skip OMDB/TMDB for non-enrichable categories
    _SKIP_CATS = {'news', 'sports', 'sport', 'sports event', 'sports talk',
                  'talk', 'talk show', 'game show', 'reality', 'live', 'music',
                  'weather', 'shopping', 'infomercial', 'documentary news'}
    _skip_enrichment = any(s in category for s in _SKIP_CATS)

    # Strip trailing (YYYY) from titles like "Batman Returns (1992)"
    import re as _re
    m = _re.match(r'^(.+?)\s*\((\d{4})\)\s*$', title)
    if m:
        title = m.group(1).strip()
        if not year:
            year = m.group(2)

    # Try to extract year from description (e.g. "Steve McQueen stars in this 1968 thriller")
    if not year and desc and content_type != 'series':
        ym = _re.search(r'\b(19[3-9]\d|20[0-2]\d)\b', desc)
        if ym:
            year = ym.group(1)

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

    # 2. OMDB lookup — if year known use direct ?t= ; otherwise search and score by actor match
    if omdb_key and not _skip_enrichment:
        try:
            q = quote(title)
            def _omdb_result(od):
                poster = od.get('Poster','')
                if poster == 'N/A': poster = ''
                return {
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
                    'imdb_id':     od.get('imdbID',''),
                    'media_type':  od.get('Type',''),
                }

            if content_type == 'series':
                url = f'http://www.omdbapi.com/?t={q}&type=series&apikey={omdb_key}'
                with urlreq.urlopen(url, timeout=5) as resp:
                    od = json.loads(resp.read())
                if od.get('Response') == 'True':
                    return jsonify(_omdb_result(od))
            elif year:
                # Year known — direct lookup is reliable
                url = f'http://www.omdbapi.com/?t={q}&y={year}&apikey={omdb_key}'
                with urlreq.urlopen(url, timeout=5) as resp:
                    od = json.loads(resp.read())
                if od.get('Response') == 'True':
                    return jsonify(_omdb_result(od))
            else:
                # No year — search for all versions, then pick best by description match
                kind = content_type or 'movie'
                url = f'http://www.omdbapi.com/?s={q}&type={kind}&apikey={omdb_key}'
                with urlreq.urlopen(url, timeout=5) as resp:
                    sr = json.loads(resp.read())
                hits = sr.get('Search', [])
                if not hits:
                    # No search results — fall back to direct title lookup
                    url = f'http://www.omdbapi.com/?t={q}&apikey={omdb_key}'
                    with urlreq.urlopen(url, timeout=5) as resp:
                        od = json.loads(resp.read())
                    if od.get('Response') == 'True':
                        return jsonify(_omdb_result(od))
                else:
                    # Score each candidate: fetch full details for top 4, pick best actor match
                    desc_words = set(_re.findall(r'[A-Z][a-z]+', desc)) if desc else set()
                    title_norm = title.lower().strip()
                    best_od, best_score = None, -1
                    for hit in hits[:4]:
                        iid = hit.get('imdbID','')
                        if not iid: continue
                        with urlreq.urlopen(f'http://www.omdbapi.com/?i={iid}&apikey={omdb_key}', timeout=5) as r2:
                            od2 = json.loads(r2.read())
                        if od2.get('Response') != 'True': continue
                        # Score: exact title match wins, then actor name words from desc
                        actor_words = set(_re.findall(r'[A-Z][a-z]+', od2.get('Actors','')))
                        exact_bonus = 20 if od2.get('Title','').lower().strip() == title_norm else 0
                        score = exact_bonus + len(desc_words & actor_words)
                        if score > best_score:
                            best_score, best_od = score, od2
                    if best_od:
                        return jsonify(_omdb_result(best_od))
        except Exception as e:
            print(f'[OMDB] {e}')

    # 3. TMDB fallback
    if tmdb_key and not _skip_enrichment:
        try:
            q   = quote(title)
            yr_param = f'&year={year}' if year else ''
            url = f'https://api.themoviedb.org/3/search/multi?api_key={tmdb_key}&query={q}{yr_param}'
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
            SELECT channel_id, channel_name, start_utc, end_utc, prog_type,
                   category, episode_title, season_num, episode_num
            FROM guide
            WHERE (lower(title) = lower(?) OR lower(title) = lower(?))
            AND end_utc > ?
            ORDER BY start_utc
            LIMIT 30
        ''', (title, clean_title, now_utc)).fetchall()
        # A PrimeStreams/XMLTV listing can omit episode data while its matching
        # Schedules Direct row has it.  Carry that detail across by title and
        # nearby start time so the visible, recordable airing is correctly
        # labelled and can be filed in Plex as a TV episode.
        enriched_rows = []
        for row in rows:
            item = dict(row)
            if item['season_num'] is None or item['episode_num'] is None:
                airing_time = datetime.strptime(
                    item['start_utc'], '%Y%m%d%H%M%S'
                ).replace(tzinfo=timezone.utc)
                window_start = (airing_time - timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')
                window_end = (airing_time + timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')
                details = conn.execute('''
                    SELECT episode_title, season_num, episode_num, start_utc, prog_type
                    FROM guide
                    WHERE lower(title)=lower(?)
                      AND season_num IS NOT NULL AND episode_num IS NOT NULL
                      AND start_utc BETWEEN ? AND ?
                ''', (clean_title, window_start, window_end)).fetchall()
                detail = min(
                    details,
                    key=lambda candidate: abs(
                        datetime.strptime(candidate['start_utc'], '%Y%m%d%H%M%S')
                        .replace(tzinfo=timezone.utc).timestamp() - airing_time.timestamp()
                    ),
                    default=None,
                )
                if detail:
                    item['episode_title'] = detail['episode_title'] or item['episode_title']
                    item['season_num'] = detail['season_num']
                    item['episode_num'] = detail['episode_num']
                    item['prog_type'] = item['prog_type'] or detail['prog_type'] or ''
            enriched_rows.append(item)
        rows = enriched_rows
        conn.close()
    except Exception:
        return jsonify({'airings': []})

    airings = []
    for r in rows:
        try:
            su = datetime.strptime(r['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            eu = datetime.strptime(r['end_utc'],   '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            sl = su.astimezone(local_tz)
            el = eu.astimezone(local_tz)
            now_ts = datetime.now(timezone.utc).timestamp()
            # Resolve each listing with the same lookup used when recording. This
            # catches SD numeric rows such as "MGM+ Hits HD" that map to a
            # PrimeStreams channel named simply "MGM+ HITS".
            _url, stream_error, stream_debug = _stream_url(r['channel_id'])
            airings.append({
                'channel_id':   r['channel_id'],
                'channel_name': r['channel_name'],
                'start_ts':     su.timestamp(),
                'stop_ts':      eu.timestamp(),
                'start_fmt':    sl.strftime('%a %b %-d, %-I:%M %p'),
                'stop_fmt':     el.strftime('%-I:%M %p'),
                'can_record':   not stream_error,
                'commercial_free': _is_commercial_free_channel(r['channel_name']),
                'stream_channel': stream_debug.get('matched_guide_channel') or '',
                'on_now':       su.timestamp() <= now_ts < eu.timestamp(),
                'prog_type':    r['prog_type'] or '',
                'category':     r['category'] or '',
                'episode_title': r['episode_title'] or '',
                'season_num':   r['season_num'],
                'episode_num':  r['episode_num'],
            })
        except Exception:
            continue

    # Collapse duplicate SD/XMLTV rows for the same channel family and time.
    # Prefer a playable row, then the HD-labelled guide row for display.
    def _family(name):
        return _channel_match_base(name)

    prog_type = next((a['prog_type'] for a in airings if a['prog_type']), '')
    is_series = (prog_type in ('EP', 'SH') or any(
        a['season_num'] is not None or a['episode_title'] for a in airings
    ))
    deduped = {}
    for airing in airings:
        key = (airing['start_ts'], airing['stop_ts'], _family(airing['channel_name']))
        score = (1 if airing['can_record'] else 0,
                 1 if re.search(r'\b(?:UHD|HD)\b', airing['channel_name'], re.I) else 0)
        current = deduped.get(key)
        current_score = current and (
            1 if current['can_record'] else 0,
            1 if re.search(r'\b(?:UHD|HD)\b', current['channel_name'], re.I) else 0,
        )
        if current is None or score > current_score:
            deduped[key] = airing
    airings = sorted(deduped.values(), key=lambda item: item['start_ts'])
    if not is_series:
        airings = [airing for airing in airings if airing['commercial_free']]
    return jsonify({
        'airings': airings, 'prog_type': prog_type, 'is_series': is_series,
        'commercial_free_only': not is_series,
    })

# ── VLC Play ──────────────────────────────────────────────────────────────────

MAX_STREAMS = 6
# _vlc_streams: { channel_id: {'pid': int, 'ch_name': str, 'title': str} }
_vlc_streams = {}
_vlc_lock    = threading.Lock()

def _proc_dead(pid):
    """Return True if the process with this PID is no longer running."""
    try:
        os.kill(pid, 0)
        return False
    except (ProcessLookupError, OSError):
        return True

@app.route('/epg-web/api/play', methods=['POST'])
def api_play():
    import signal as _sig
    data       = request.json or {}
    channel_id = data.get('channel_id', '')
    title      = data.get('title', '')
    ch_label   = data.get('ch_label', '')
    ch_name    = data.get('ch_name', channel_id)
    url, err, dbg = _stream_url(channel_id)
    if err:
        return jsonify({'error': err, 'debug': dbg}), 400
    with _vlc_lock:
        # Purge any VLC processes that have already exited
        dead = [cid for cid, v in _vlc_streams.items()
                if _proc_dead(v['pid'])]
        for cid in dead:
            del _vlc_streams[cid]
        # If already playing this channel, stop it first
        if channel_id in _vlc_streams:
            try: os.kill(_vlc_streams[channel_id]['pid'], _sig.SIGTERM)
            except Exception: pass
            del _vlc_streams[channel_id]
        # Enforce max streams
        if len(_vlc_streams) >= MAX_STREAMS:
            return jsonify({'error': f'Max {MAX_STREAMS} streams already playing'}), 400
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
                   '--meta-title', display_title,
                   '--video-title', display_title,
                   '--video-title-timeout', '5000']
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _vlc_streams[channel_id] = {'pid': proc.pid, 'ch_name': ch_name, 'title': title}
            streams_snapshot = dict(_vlc_streams)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'streams': [
        {'channel_id': cid, 'ch_name': v['ch_name'], 'title': v['title']}
        for cid, v in streams_snapshot.items()
    ]})

@app.route('/epg-web/api/play/stop', methods=['POST'])
def api_play_stop():
    import signal as _sig
    data       = request.json or {}
    channel_id = data.get('channel_id', '')
    with _vlc_lock:
        if channel_id and channel_id in _vlc_streams:
            try: os.kill(_vlc_streams[channel_id]['pid'], _sig.SIGTERM)
            except Exception: pass
            del _vlc_streams[channel_id]
        elif not channel_id:
            # Stop all
            for v in _vlc_streams.values():
                try: os.kill(v['pid'], _sig.SIGTERM)
                except Exception: pass
            _vlc_streams.clear()
        streams_snapshot = dict(_vlc_streams)
    return jsonify({'ok': True, 'streams': [
        {'channel_id': cid, 'ch_name': v['ch_name'], 'title': v['title']}
        for cid, v in streams_snapshot.items()
    ]})

@app.route('/epg-web/api/play/status')
def api_play_status():
    with _vlc_lock:
        dead = [cid for cid, v in _vlc_streams.items() if _proc_dead(v['pid'])]
        for cid in dead:
            del _vlc_streams[cid]
        return jsonify({'streams': [
            {'channel_id': cid, 'ch_name': v['ch_name'], 'title': v['title']}
            for cid, v in _vlc_streams.items()
        ]})

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
            SELECT channel_id, channel_name, start_utc, end_utc,
                   episode_title, season_num, episode_num
            FROM guide
            WHERE (lower(title)=lower(?) OR lower(title)=lower(?))
            AND start_utc > ? AND channel_id IN ({})
            ORDER BY start_utc LIMIT 500
        '''.format(','.join('?' * len(recordable))),
            [title, clean_title, now_str] + list(recordable)
        ).fetchall()
        # Some provider rows omit episode details while a duplicate guide row
        # for the same airing contains them.  Match the closest such row so a
        # series recording gets a Plex-ready season/episode filename.
        enriched = []
        for row in rows:
            item = dict(row)
            if item['season_num'] is None or item['episode_num'] is None:
                airing_time = datetime.strptime(
                    item['start_utc'], '%Y%m%d%H%M%S'
                ).replace(tzinfo=timezone.utc)
                window_start = (airing_time - timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')
                window_end = (airing_time + timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')
                details = conn.execute('''
                    SELECT episode_title, season_num, episode_num
                           ,start_utc
                    FROM guide
                    WHERE lower(title)=lower(?)
                      AND season_num IS NOT NULL AND episode_num IS NOT NULL
                      AND start_utc BETWEEN ? AND ?
                ''', (clean_title, window_start, window_end)).fetchall()
                detail = min(
                    details,
                    key=lambda candidate: abs(
                        datetime.strptime(candidate['start_utc'], '%Y%m%d%H%M%S')
                        .replace(tzinfo=timezone.utc).timestamp() - airing_time.timestamp()
                    ),
                    default=None,
                )
                if detail:
                    item['episode_title'] = detail['episode_title'] or item['episode_title']
                    item['season_num'] = detail['season_num']
                    item['episode_num'] = detail['episode_num']
            enriched.append(item)
        rows = enriched
        conn.close()
    except Exception as e:
        print(f'[series] query error: {e}')
        return 0

    # Deduplicate: one best airing per unique episode.
    # Group by (season_num, episode_num) when available; prefer HD channels.
    def _ch_quality(name):
        n = (name or '').upper()
        if 'UHD' in n or '4K' in n: return 3
        if 'HD' in n:                return 2
        return 1

    ep_best = {}   # (sn, en) → row with best channel quality
    unknown_best = {}  # (channel, time) → unknown episode airing
    for r in rows:
        sn, en = r['season_num'], r['episode_num']
        if sn is not None and en is not None:
            key = (sn, en)
            if key not in ep_best or _ch_quality(r['channel_name']) > _ch_quality(ep_best[key]['channel_name']):
                ep_best[key] = r
        else:
            # Keep unknown episodes distinct by the actual airing. They go to
            # the TV holding area on the recording Mac, never Movies.
            unknown_best[(r['channel_id'], r['start_utc'])] = r
    best_rows = list(ep_best.values()) + list(unknown_best.values())

    with _rec_lock:
        existing_keys = {(r['channel_id'], r['start_ts']) for r in _recs.values()
                         if _rec_is_active(r)}
    # The in-memory list is empty during early startup, so also consult the
    # durable queue before adding work from an active series rule.
    try:
        conn = sqlite3.connect(guide_db_path)
        db_existing = conn.execute('''SELECT channel_id, start_ts FROM recordings
            WHERE status NOT IN ('done','done_ts','cancelled','failed','error',
                                 'skipped_existing_better','skipped_too_short')''').fetchall()
        conn.close()
        existing_keys.update((row[0], row[1]) for row in db_existing)
    except Exception as exc:
        print(f'[series] queue dedupe lookup failed: {exc}')
    for r in best_rows:
        try:
            su = datetime.strptime(r['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            eu = datetime.strptime(r['end_utc'],   '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            key = (r['channel_id'], su.timestamp())
            if key in existing_keys:
                continue
            rec_id = f"rec_{int(time.time()*1000)}_{r['channel_id'][:8]}"
            _url, stream_error, stream_debug = _stream_url(r['channel_id'])
            if stream_error:
                print(f'[series] stream lookup failed for {r["channel_id"]}: {stream_error}')
                continue
            backend = _recording_backend()
            rec = {
                'title': title, 'channel_id': r['channel_id'],
                'channel': r['channel_name'],
                'start_ts': su.timestamp(), 'stop_ts': eu.timestamp(),
                'status': 'queued', 'progress': 0, 'log': [], 'pid': None, 'file': None,
                'backend': backend, 'stream_id': str(stream_debug.get('stream_id') or ''),
                'episode_title': r.get('episode_title') or '',
                'season_num': r.get('season_num'),
                'episode_num': r.get('episode_num'),
                'is_series': True,
            }
            if not _db_upsert_rec(rec_id, rec):
                print(f'[series] could not persist recording {rec_id}; not scheduling')
                continue
            with _rec_lock:
                _recs[rec_id] = rec
                _rec_cancel_events[rec_id] = threading.Event()
                existing_keys.add(key)
            if backend == 'local':
                t = threading.Thread(target=_run_recording, args=(rec_id,), daemon=True)
                t.start()
            scheduled += 1
        except Exception as e:
            print(f'[series] airing error: {e}')
    return scheduled


def _schedule_active_series(guide_db_path=None):
    """Queue newly available airings for every enabled series rule."""
    cfg = load_config()
    db_path = guide_db_path or cfg.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    try:
        conn = sqlite3.connect(db_path)
        titles = [row[0] for row in conn.execute(
            'SELECT title FROM series_recordings WHERE active=1'
        ).fetchall()]
        conn.close()
    except Exception as exc:
        print(f'[series] active rule lookup failed: {exc}')
        return 0
    total = sum(_schedule_series_airings(
        title, db_path, cfg.get('db_path', '/Volumes/EPG/Movies.db'),
        cfg.get('timezone', 'America/New_York')
    ) for title in titles)
    if total:
        print(f'[series] queued {total} new airing(s) from active rules')
    return total

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
    cancelled_ids = []
    with _rec_lock:
        for rec_id, rec in _recs.items():
            if (rec.get('title','').lower() == title.lower() and
                    _rec_status_base(rec.get('status')) in ('queued', 'scheduled')):
                _rec_cancel_events.setdefault(rec_id, threading.Event()).set()
                rec['status'] = 'cancelled'
                cancelled += 1
                cancelled_ids.append(rec_id)
    for rec_id in cancelled_ids:
        _db_update_rec_status(rec_id, 'cancelled', 'Series recording cancelled by user')
    return jsonify({'ok': True, 'cancelled': cancelled})

@app.route('/epg-web/api/record', methods=['POST'])
def api_record():
    data       = request.json or {}
    title      = data.get('title', 'Unknown')
    channel_id = data.get('channel_id', '')
    start_ts   = float(data.get('start_ts', time.time()))
    stop_ts    = float(data.get('stop_ts', time.time() + 3600))
    episode_title = str(data.get('episode_title', '') or '').strip()
    season_num = data.get('season_num')
    episode_num = data.get('episode_num')
    try:
        season_num = int(season_num) if season_num is not None else None
        episode_num = int(episode_num) if episode_num is not None else None
    except (TypeError, ValueError):
        season_num = episode_num = None
    rec_id     = str(uuid.uuid4())[:8]
    cfg2        = load_config()
    guide_db    = cfg2.get('guide_db_path', os.path.join(BASE_DIR, 'guide.db'))
    movies_db   = cfg2.get('db_path', '/Volumes/EPG/Movies.db')

    # Check whether the requested channel has a PrimeStreams stream
    has_stream = False
    try:
        mconn = sqlite3.connect(movies_db)
        row = mconn.execute(
            'SELECT stream_id FROM channels WHERE guide_channel=? AND stream_id IS NOT NULL AND stream_id!="" LIMIT 1',
            (channel_id,)
        ).fetchone()
        has_stream = bool(row)
        mconn.close()
    except Exception:
        pass

    # If no stream on requested channel, find the nearest upcoming PS airing of same title
    if not has_stream:
        ps_channel_id = None
        ps_channel_name = None
        ps_start_ts = None
        ps_stop_ts = None
        try:
            # Get all PS-streamable channel IDs
            ps_ids = get_ps_channel_ids(guide_db, movies_db)
            if ps_ids:
                now_str = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                gconn = sqlite3.connect(guide_db)
                gconn.row_factory = sqlite3.Row
                airing = gconn.execute(
                    '''SELECT channel_id, channel_name, start_utc, end_utc FROM guide
                       WHERE lower(title)=lower(?) AND start_utc > ?
                       AND channel_id IN ({})
                       ORDER BY start_utc LIMIT 1'''.format(','.join('?'*len(ps_ids))),
                    [title, now_str] + list(ps_ids)
                ).fetchone()
                gconn.close()
                if airing:
                    su = datetime.strptime(airing['start_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                    eu = datetime.strptime(airing['end_utc'],   '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                    ps_channel_id   = airing['channel_id']
                    ps_channel_name = airing['channel_name']
                    ps_start_ts     = su.timestamp()
                    ps_stop_ts      = eu.timestamp()
        except Exception as e:
            print(f'[record] PS fallback error: {e}')

        if ps_channel_id:
            channel_id   = ps_channel_id
            channel_name = ps_channel_name
            start_ts     = ps_start_ts
            stop_ts      = ps_stop_ts
            print(f'[record] Remapped "{title}" → PS channel {channel_id} at {start_ts}')
        else:
            return jsonify({'ok': False, 'error': f'"{title}" is not airing on any PrimeStreams channel soon'}), 200
    else:
        # Channel has a stream — just look up its name
        channel_name = channel_id
        try:
            gconn = sqlite3.connect(guide_db)
            row = gconn.execute('SELECT channel_name FROM guide WHERE channel_id=? LIMIT 1', (channel_id,)).fetchone()
            if row:
                channel_name = row[0]
            gconn.close()
        except Exception:
            pass

    # Dedup: reject if same channel+start_ts already queued/scheduled/recording
    with _rec_lock:
        for existing in _recs.values():
            if (abs(existing.get('start_ts', 0) - start_ts) < 5 and
                    existing.get('channel_id','') == channel_id and
                    _rec_is_active(existing)):
                return jsonify({'ok': True, 'id': 'dup', 'dup': True})

    rec = {
        'title':      title,
        'channel_id': channel_id,
        'channel':    channel_name,
        'start_ts':   start_ts,
        'stop_ts':    stop_ts,
        'status':     'queued',
        'progress':   0,
        'log':        [],
        'pid':        None,
        'file':       None,
        'backend':    _recording_backend(),
        'stream_id':  '',
        'episode_title': episode_title,
        'season_num': season_num,
        'episode_num': episode_num,
        'is_series': bool(data.get('is_series', False)),
    }
    _url, stream_error, stream_debug = _stream_url(channel_id)
    if stream_error:
        return jsonify({'ok': False, 'error': stream_error}), 400
    rec['stream_id'] = str(stream_debug.get('stream_id') or '')
    if not _db_upsert_rec(rec_id, rec):
        return jsonify({'ok': False, 'error': 'Could not save recording to guide database'}), 500
    with _rec_lock:
        _recs[rec_id] = rec
        _rec_cancel_events[rec_id] = threading.Event()
    if rec['backend'] == 'local':
        t = threading.Thread(target=_run_recording, args=(rec_id,), daemon=True)
        t.start()
    return jsonify({'ok': True, 'id': rec_id, 'channel': channel_name, 'start_ts': start_ts})

@app.route('/epg-web/api/record/status')
def api_rec_status():
    with _rec_lock:
        return jsonify({'recordings': dict(_recs)})

@app.route('/epg-web/api/recordings/files')
def api_recordings_files():
    cfg = load_config()
    rec_dir = cfg.get('rec_path', os.path.expanduser('~/Movies/Recordings'))
    files = []
    if os.path.isdir(rec_dir):
        for fn in sorted(os.listdir(rec_dir)):
            fp = os.path.join(rec_dir, fn)
            if os.path.isfile(fp):
                stat = os.stat(fp)
                files.append({
                    'name':     fn,
                    'size':     stat.st_size,
                    'mtime':    stat.st_mtime,
                    'mtime_fmt': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                })
    files.sort(key=lambda x: x['mtime'], reverse=True)
    total = sum(f['size'] for f in files)
    return jsonify({'ok': True, 'dir': rec_dir, 'files': files, 'total': total})

@app.route('/epg-web/api/recordings/delete', methods=['POST'])
def api_recordings_delete():
    cfg = load_config()
    rec_dir = cfg.get('rec_path', os.path.expanduser('~/Movies/Recordings'))
    names = (request.json or {}).get('files', [])
    deleted = []
    errors  = []
    for fn in names:
        # Safety: no path traversal
        fn = os.path.basename(fn)
        fp = os.path.join(rec_dir, fn)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
                deleted.append(fn)
            except Exception as e:
                errors.append(f'{fn}: {e}')
        else:
            errors.append(f'{fn}: not found')
    return jsonify({'ok': not errors, 'deleted': deleted, 'errors': errors})

@app.route('/epg-web/api/record/cancel', methods=['POST'])
def api_rec_cancel():
    rec_id = (request.json or {}).get('id','')
    pid = None
    file_path = ''
    with _rec_lock:
        r = _recs.get(rec_id)
        if not r:
            return jsonify({'ok': False, 'error': 'Recording not found'}), 404
        status = _rec_status_base(r.get('status'))
        if status not in ('queued', 'scheduled', 'agent_claimed', 'preflight',
                          'waiting', 'recording', 'awaiting_transfer', 'transferring'):
            return jsonify({'ok': False, 'error': f'Recording cannot be cancelled while {status}'}), 409
        cancel_event = _rec_cancel_events.setdefault(rec_id, threading.Event())
        cancel_event.set()
        pid = r.get('pid')
        file_path = r.get('file') or ''
        r['status'] = 'cancelled'
        r.setdefault('log', []).append('Cancelled by user')

    if pid:
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Could not stop FFmpeg: {e}'}), 500

    _db_update_rec_status(rec_id, 'cancelled', 'Cancelled by user', file=file_path)
    return jsonify({'ok': True, 'cancelled': True, 'id': rec_id})

# ── Recording Agent API ──────────────────────────────────────────────────────

_AGENT_ACTIVE_STATES = {
    'agent_claimed', 'preflight', 'waiting', 'recording', 'converting',
    'awaiting_transfer', 'transferring'
}
_AGENT_TERMINAL_STATES = {
    'done', 'done_ts', 'cancelled', 'failed', 'error',
    'skipped_existing_better', 'skipped_too_short'
}

def _agent_authorized():
    cfg = load_config()
    expected = os.environ.get('EPG_AGENT_TOKEN') or cfg.get('recording_agent_token', '')
    supplied = request.headers.get('Authorization', '')
    if supplied.lower().startswith('bearer '):
        supplied = supplied[7:].strip()
    else:
        supplied = request.headers.get('X-EPG-Agent-Token', '')
    return bool(expected and supplied and hmac.compare_digest(str(expected), str(supplied)))

def _agent_auth_failure():
    return jsonify({'ok': False, 'error': 'Unauthorized recording agent'}), 401

def _agent_job_dict(row):
    return {
        'id': row['rec_id'], 'title': row['title'], 'channel': row['channel'],
        'channel_id': row['channel_id'], 'stream_id': row['stream_id'] or '',
        'start_ts': row['start_ts'], 'stop_ts': row['stop_ts'],
        'status': row['status'], 'agent_id': row['agent_id'],
        'lease_until': row['lease_until'],
        'episode_title': row['episode_title'] or '',
        'season_num': row['season_num'], 'episode_num': row['episode_num'],
        'is_series': bool(row['is_series']),
    }

@app.route('/epg-web/api/agent/health')
def api_agent_health():
    if not _agent_authorized():
        return _agent_auth_failure()
    return jsonify({'ok': True, 'server_time': time.time(),
                    'recording_backend': _recording_backend()})

@app.route('/epg-web/api/agent/jobs/claim', methods=['POST'])
def api_agent_claim():
    if not _agent_authorized():
        return _agent_auth_failure()
    data = request.json or {}
    agent_id = str(data.get('agent_id', '')).strip()[:100]
    if not agent_id:
        return jsonify({'ok': False, 'error': 'agent_id is required'}), 400
    lease_seconds = max(30, min(int(data.get('lease_seconds', 90)), 300))
    claim_ahead = max(30, min(int(data.get('claim_ahead_seconds', 300)), 1800))
    now = time.time()
    conn = sqlite3.connect(_guide_db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''UPDATE recordings
                        SET status='queued', agent_id=NULL, lease_until=NULL,
                            failure_reason='Agent lease expired; job requeued'
                        WHERE COALESCE(backend,'local')='agent'
                        AND status IN ('agent_claimed','preflight','waiting')
                        AND lease_until IS NOT NULL AND lease_until < ?''', (now,))
        conn.execute('''UPDATE recordings
                        SET status='failed', lease_until=NULL,
                            failure_reason='Recording agent heartbeat expired'
                        WHERE COALESCE(backend,'local')='agent'
                        AND status IN ('recording','converting','awaiting_transfer','transferring')
                        AND lease_until IS NOT NULL AND lease_until < ?''', (now,))
        row = conn.execute('''SELECT * FROM recordings
                              WHERE COALESCE(backend,'local')='agent'
                              AND status IN ('queued','scheduled')
                              AND stop_ts > ? AND start_ts <= ?
                              ORDER BY start_ts LIMIT 1''',
                           (now + 30, now + claim_ahead)).fetchone()
        if row:
            lease_until = now + lease_seconds
            conn.execute('''UPDATE recordings
                            SET status='agent_claimed', agent_id=?, lease_until=?,
                                heartbeat_at=?, updated_at=? WHERE rec_id=?''',
                         (agent_id, lease_until, now, datetime.now(timezone.utc).isoformat(),
                          row['rec_id']))
            row = conn.execute('SELECT * FROM recordings WHERE rec_id=?',
                               (row['rec_id'],)).fetchone()
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise
    finally:
        conn.close()
    if not row:
        return jsonify({'ok': True, 'job': None, 'server_time': now})
    job = _agent_job_dict(row)
    with _rec_lock:
        rec = _recs.setdefault(row['rec_id'], {})
        rec.update({'title': row['title'], 'channel': row['channel'],
                    'channel_id': row['channel_id'], 'stream_id': row['stream_id'] or '',
                    'start_ts': row['start_ts'], 'stop_ts': row['stop_ts'],
                    'status': 'agent_claimed', 'backend': 'agent', 'pid': None,
                    'file': row['file'] or None, 'progress': 0, 'log': []})
    return jsonify({'ok': True, 'job': job, 'server_time': now})

@app.route('/epg-web/api/agent/jobs/<rec_id>/heartbeat', methods=['POST'])
def api_agent_heartbeat(rec_id):
    if not _agent_authorized():
        return _agent_auth_failure()
    data = request.json or {}
    agent_id = str(data.get('agent_id', '')).strip()[:100]
    status = str(data.get('status', 'agent_claimed')).strip().lower()
    if status not in _AGENT_ACTIVE_STATES | _AGENT_TERMINAL_STATES:
        return jsonify({'ok': False, 'error': f'Invalid agent status: {status}'}), 400
    lease_seconds = max(30, min(int(data.get('lease_seconds', 90)), 300))
    now = time.time()
    conn = sqlite3.connect(_guide_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    row = conn.execute('SELECT * FROM recordings WHERE rec_id=?', (rec_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'Recording job not found'}), 404
    if row['status'] == 'cancelled':
        conn.close()
        return jsonify({'ok': True, 'cancel_requested': True, 'status': 'cancelled'})
    if row['agent_id'] and row['agent_id'] != agent_id:
        conn.close()
        return jsonify({'ok': False, 'error': 'Job is leased to another agent'}), 409
    terminal = status in _AGENT_TERMINAL_STATES
    result = data.get('result') if isinstance(data.get('result'), dict) else {}
    file_path = str(data.get('file', row['file'] or ''))
    failure_reason = str(data.get('message', ''))[:1000]
    quality_decision = str(data.get('quality_decision', ''))[:1000]
    conn.execute('''UPDATE recordings SET status=?, agent_id=?, lease_until=?,
                    heartbeat_at=?, updated_at=?, file=?, failure_reason=?,
                    quality_decision=?, result_json=? WHERE rec_id=?''',
                 (status, agent_id, None if terminal else now + lease_seconds,
                  now, datetime.now(timezone.utc).isoformat(), file_path,
                  failure_reason, quality_decision, json.dumps(result), rec_id))
    conn.commit()
    conn.close()
    with _rec_lock:
        rec = _recs.setdefault(rec_id, {})
        rec.update({'status': status, 'file': file_path or None,
                    'progress': data.get('progress', rec.get('progress', 0)),
                    'backend': 'agent'})
        if failure_reason:
            rec.setdefault('log', []).append(failure_reason)
    return jsonify({'ok': True, 'cancel_requested': False, 'status': status,
                    'lease_until': None if terminal else now + lease_seconds})

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
.ch-name-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;flex:1;}
.ch-name-label:hover{color:#60a5fa;text-decoration:underline;}
.guide-ch-star{background:none;border:0;color:#475569;cursor:pointer;font-size:15px;line-height:1;padding:0 5px 0 0;}
.guide-ch-star.is-fav{color:#f59e0b;}
.prog-row{display:flex;flex:1;position:relative;height:42px;}
.prog-block{position:absolute;top:2px;bottom:2px;border-radius:4px;
            background:#1a2744;border:1px solid #243460;border-left:3px solid #243460;overflow:hidden;
            cursor:pointer;transition:background .1s;padding:0 6px;
            display:flex;flex-direction:column;justify-content:center;min-width:4px;}
.prog-block .prog-row-top{display:flex;align-items:center;width:100%;overflow:hidden;}
.prog-ep{font-size:9px;color:#64748b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;width:100%;line-height:1.2;margin-top:1px;}
.prog-block:hover{background:#243460;border-color:#3b5bdb;}
.prog-block.now{background:#1a3a2a;border-color:#2d5a3d;}
.prog-block.in-plex{border-top:3px solid #a78bfa !important;box-shadow:inset 0 2px 0 #7c3aed44;}
/* Category colour bands — left border + subtle tint (use .prog-block.cat-* for specificity over .prog-block.now) */
.prog-block.cat-sports  {background:#0e1c35;border-left-color:#3b82f6;}
.prog-block.cat-news    {background:#2a1212;border-left-color:#ef4444;}
.prog-block.cat-kids    {background:#231f08;border-left-color:#f59e0b;}
.prog-block.cat-doc     {background:#170f2e;border-left-color:#8b5cf6;}
.prog-block.cat-reality {background:#25102a;border-left-color:#ec4899;}
.prog-block.cat-talk    {background:#231808;border-left-color:#f97316;}
.prog-block.cat-scripted{background:#0e2418;border-left-color:#22c55e;}
.cat-badge{font-size:8px;font-weight:700;letter-spacing:.02em;line-height:1.2;padding:1px 3px;border-radius:2px;
           margin-right:4px;flex-shrink:0;opacity:1;}
.prog-block.cat-sports  .cat-badge{background:#1d4ed8;color:#bfdbfe;}
.prog-block.cat-news    .cat-badge{background:#b91c1c;color:#fecaca;}
.prog-block.cat-kids    .cat-badge{background:#b45309;color:#fef3c7;}
.prog-block.cat-doc     .cat-badge{background:#6d28d9;color:#ede9fe;}
.prog-block.cat-reality .cat-badge{background:#be185d;color:#fce7f3;}
.prog-block.cat-talk    .cat-badge{background:#c2410c;color:#ffedd5;}
.prog-block.cat-scripted .cat-badge{background:#15803d;color:#dcfce7;}
.prog-block.cat-movie  {background:#1a1510;border-left-color:#f59e0b;}
.prog-block.cat-movie  .cat-badge{background:#b45309;color:#fef3c7;}
.prog-block.cat-series {background:#0f1e2e;border-left-color:#38bdf8;}
.prog-block.cat-series .cat-badge{background:#0369a1;color:#e0f2fe;}
.plex-play-btn{font-size:9px;color:#a78bfa;background:#2d1f5e;border:1px solid #7c3aed;border-radius:3px;padding:0 4px;margin-right:3px;flex-shrink:0;cursor:pointer;line-height:14px;}
.plex-play-btn:hover{background:#7c3aed;color:#fff;}
.rec-dot{font-size:9px;color:#ef4444;margin-right:3px;flex-shrink:0;animation:pulse-rec 1s infinite;}
.sched-dot{font-size:9px;color:#f59e0b;margin-right:3px;flex-shrink:0;}
.rec-btn{font-size:9px;color:#ef4444;background:rgba(239,68,68,.15);border:1px solid #ef4444;border-radius:3px;padding:0 4px;margin-right:3px;flex-shrink:0;cursor:pointer;line-height:14px;}
.rec-btn:hover{background:#ef4444;color:#fff;}
.rec-btn.pending{color:#f59e0b;border-color:#f59e0b;background:rgba(245,158,11,.15);cursor:default;}
.plex-qual{font-size:9px;color:#7c3aed;margin-right:3px;flex-shrink:0;opacity:.8;}
.stream-qual{font-size:8px;color:#67e8f9;background:#083344;border:1px solid #155e75;border-radius:3px;
             width:76px;box-sizing:border-box;padding:0 3px;margin-right:4px;flex-shrink:0;line-height:13px;
             font-family:monospace;opacity:0;transition:opacity .12s;}
.stream-qual.ready{opacity:1;}
@keyframes pulse-rec{0%,100%{opacity:1;}50%{opacity:.3;}}
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
  <span style="font-size:11px;color:#888;">{{ VERSION }}</span>
  <span class="badge-live" id="live-badge">● Server live</span>
  <span id="clock">--:-- --</span>
  <div class="spacer"></div>
  <button class="btn btn-ghost btn-sm" id="btn-refresh" onclick="loadGuide()">↻ Refresh</button>
  <button class="btn btn-ghost btn-sm" id="btn-fetch-guide" onclick="fetchGuide()">⬇ Fetch Guide</button>
  <button class="btn btn-ghost btn-sm" onclick="openSettings()">⚙ Settings</button>
</header>

<nav>
  <div class="tab active" onclick="switchTab('guide')">📺 Guide</div>
  <div class="tab" onclick="switchTab('recommendations')">⭐ Recommendations</div>
  <div class="tab" onclick="switchTab('channels')">📡 Channels</div>
  <div class="tab" onclick="switchTab('247')">🔁 24/7</div>
  <div class="tab" onclick="switchTab('schedule')">📅 Schedule</div>
  <div class="tab" onclick="switchTab('conversions')">🔄 Conversions</div>
  <div class="tab" onclick="switchTab('storage')">💾 Storage</div>
</nav>

<!-- GUIDE -->
<div id="pane-guide" class="pane active">
  <div class="guide-toolbar">
    <button class="btn btn-ghost btn-sm" onclick="guideNav(-4)">◀ Earlier</button>
    <span id="guide-window" style="font-size:13px;color:#555;"></span>
    <button class="btn btn-ghost btn-sm" onclick="guideNav(4)">Later ▶</button>
    <button class="btn btn-ghost btn-sm" onclick="guideJumpNow()" style="color:#22c55e;">⬤ Now</button>
    <select id="guide-ch-mode" onchange="localStorage.setItem('epg_guide_mode',this.value);fetchAndRenderGuide()" style="background:#1a1a1a;border:1px solid #2d2d2d;border-radius:6px;color:#94a3b8;padding:5px 10px;font-size:13px;">
      <option value="all">All Channels</option>
      <option value="fav">★ Favorites</option>
      <option value="movie">🎬 Movie Channels</option>
      <option value="ps">📡 PrimeStreams Only</option>
      <option value="ps_episode">📺 PS · S/E Ready</option>
      <option value="sd">📺 SD Only</option>
    </select>
    <div style="position:relative;display:inline-block;">
      <input id="ch-filter" placeholder="🔍 Search channels & shows…" oninput="onSearchInput(this.value)" onkeydown="if(event.key==='Escape')clearSearch()" autocomplete="off" style="width:220px;">
      <div id="search-dropdown" style="display:none;position:absolute;top:100%;left:0;width:320px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;z-index:500;max-height:320px;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);margin-top:4px;"></div>
    </div>
    <button id="ch-page-prev" class="btn btn-ghost btn-sm" onclick="chPagePrev()" style="display:none;">◀ Prev 200</button>
    <span id="ch-page-info" style="font-size:12px;color:#64748b;"></span>
    <button id="ch-page-next" class="btn btn-ghost btn-sm" onclick="chPageNext()" style="display:none;">Next 200 ▶</button>
    <button class="btn btn-ghost btn-sm" onclick="fetchSD()" id="btn-sd" title="Pull 14 days from Schedules Direct">📡 Fetch SD</button>
  </div>
  <!-- Compact storage bar -->
  <div id="storage-bar" style="display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:6px 4px;font-size:12px;color:#64748b;border-bottom:1px solid #1e293b;margin-bottom:6px;"></div>
  <div id="now-playing-bar" style="display:none;gap:8px;align-items:center;flex-wrap:wrap;padding:6px 4px;border-bottom:1px solid #1e293b;margin-bottom:4px;"></div>
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
    <h3 style="font-size:13px;color:#64748b;margin-bottom:8px;">📺 Recurring Recordings</h3>
    <div id="series-list" style="max-height:300px;overflow-y:auto;"></div>
  </div>
</div>

<!-- Programme detail modal -->
<div id="prog-modal-overlay" onclick="if(event.target===this)closeProg()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:1000;align-items:center;justify-content:center;">
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
            <div id="pm-air" style="font-size:12px;color:#3b82f6;margin-bottom:4px;font-weight:500;"></div>
            <div id="pm-ep"  style="font-size:12px;color:#94a3b8;margin-bottom:8px;display:none;"></div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
              <span id="pm-year"  style="font-size:12px;color:#94a3b8;"></span>
              <span id="pm-rated" style="font-size:11px;background:#1e293b;color:#94a3b8;padding:1px 6px;border-radius:4px;"></span>
              <span id="pm-genre" style="font-size:12px;color:#94a3b8;"></span>
              <span id="pm-imdb"  style="font-size:12px;color:#fbbf24;font-weight:600;"></span>
              <a id="pm-imdb-link" href="#" target="_blank" style="font-size:11px;color:#3b82f6;display:none;">IMDb ↗</a>
            </div>
            <div id="pm-actors" style="font-size:12px;color:#94a3b8;margin-bottom:3px;"></div>
            <div id="pm-director" style="font-size:12px;color:#94a3b8;margin-bottom:10px;"></div>
            <p id="pm-plot" style="font-size:13px;color:#94a3b8;line-height:1.6;margin:0;"></p>
        </div>
      </div>
      <!-- Plex file info -->
      <div id="pm-plex-wrap" style="display:none;border-top:1px solid #2d1f5e;padding:9px 20px;background:#0d0d1f;">
        <span style="font-size:10px;font-weight:700;color:#7c3aed;text-transform:uppercase;letter-spacing:.07em;margin-right:10px;">▶ PLEX</span>
        <span id="pm-plex-info" style="font-size:12px;color:#a78bfa;font-family:monospace;"></span>
      </div>
      <!-- Actual incoming stream quality, sampled from the recordable channel -->
      <div id="pm-stream-wrap" style="display:none;border-top:1px solid #17365d;padding:9px 20px;background:#0b1726;">
        <span style="font-size:10px;font-weight:700;color:#60a5fa;text-transform:uppercase;letter-spacing:.07em;margin-right:10px;">📡 RECORDING STREAM</span>
        <span id="pm-stream-info" style="font-size:12px;color:#93c5fd;font-family:monospace;"></span>
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
          <span style="font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.05em;">📡 PrimeStreams Airings</span>
          <button id="pm-series-btn" class="btn btn-ghost btn-sm" onclick="recordSeries()" style="font-size:11px;padding:3px 10px;">📺 Record Identified Episodes</button>
          <button id="pm-unrecorded-btn" class="btn btn-ghost btn-sm" onclick="toggleUnrecorded()" style="font-size:11px;padding:3px 10px;display:none;">🔲 Unscheduled Only</button>
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
        <button class="btn btn-primary btn-sm" onclick="addWanted('movie')">+ Movie</button>
        <button class="btn btn-primary btn-sm" onclick="addWanted('series')">+ Series</button>
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
    <h2 style="display:flex;align-items:center;justify-content:space-between;">All Channels
      <button class="btn btn-ghost btn-sm" onclick="syncStreams()" id="btn-sync-streams" style="font-size:12px;">🔄 Sync Streams</button>
    </h2>
    <div id="sync-status" style="display:none;font-size:12px;padding:6px 0;"></div>
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

<!-- 24/7 CHANNELS -->
<div id="pane-247" class="pane">
  <div class="card">
    <h2>🔁 24/7 Channels</h2>
    <div class="search-row">
      <input id="c247-search" placeholder="Search 24/7 channels…" oninput="load247()">
      <label style="display:flex;align-items:center;gap:4px;font-size:13px;color:#f59e0b;white-space:nowrap;cursor:pointer;">
        <input type="checkbox" id="c247-show-fav" checked onchange="load247()"> ★ Favorites
      </label>
      <label style="display:flex;align-items:center;gap:4px;font-size:13px;color:#60a5fa;white-space:nowrap;cursor:pointer;">
        <input type="checkbox" id="c247-show-tv" checked onchange="load247()"> 📺 TV
      </label>
      <label style="display:flex;align-items:center;gap:4px;font-size:13px;color:#c084fc;white-space:nowrap;cursor:pointer;">
        <input type="checkbox" id="c247-show-movies" checked onchange="load247()"> 🎬 Movies
      </label>
      <label style="display:flex;align-items:center;gap:4px;font-size:13px;color:#34d399;white-space:nowrap;cursor:pointer;">
        <input type="checkbox" id="c247-show-kids" checked onchange="load247()"> 🧒 Kids
      </label>
      <label style="display:flex;align-items:center;gap:4px;font-size:13px;color:#fb923c;white-space:nowrap;cursor:pointer;">
        <input type="checkbox" id="c247-show-sports" checked onchange="load247()"> 🏆 Sports
      </label>
      <label style="display:flex;align-items:center;gap:4px;font-size:13px;color:#475569;white-space:nowrap;cursor:pointer;">
        <input type="checkbox" id="c247-show-hidden" onchange="load247()"> ✕ Hidden
      </label>
    </div>
    <div id="c247-status" class="status-msg"></div>
    <div id="c247-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin-top:10px;"></div>
  </div>
</div>

<!-- SCHEDULE -->
<div id="pane-schedule" class="pane">
  <div class="card">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;">
      <h2 style="margin:0;">Recording Schedule</h2>
      <label style="font-size:12px;color:#94a3b8;display:flex;align-items:center;gap:4px;cursor:pointer;"><input type="checkbox" id="sf-scheduled" checked onchange="loadSchedule()"> Scheduled</label>
      <label style="font-size:12px;color:#22c55e;display:flex;align-items:center;gap:4px;cursor:pointer;"><input type="checkbox" id="sf-completed" checked onchange="loadSchedule()"> Completed</label>
      <label style="font-size:12px;color:#ef4444;display:flex;align-items:center;gap:4px;cursor:pointer;"><input type="checkbox" id="sf-failed" checked onchange="loadSchedule()"> Failed</label>
      <label style="font-size:12px;color:#64748b;display:flex;align-items:center;gap:4px;cursor:pointer;"><input type="checkbox" id="sf-skipped" onchange="loadSchedule()"> Skipped</label>
      <label style="font-size:12px;color:#64748b;display:flex;align-items:center;gap:4px;cursor:pointer;"><input type="checkbox" id="sf-all-time" onchange="loadSchedule()"> All time</label>
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
  <div class="card" style="margin-top:12px;">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">
      <h2 style="margin:0;">🗑 Recordings Cleanup</h2>
      <span id="rec-files-total" style="font-size:12px;color:#64748b;"></span>
      <button class="btn btn-ghost btn-sm" style="margin-left:auto;" onclick="loadRecFiles()">↻ Refresh</button>
      <button class="btn btn-sm" id="rec-delete-btn" style="background:#7f1d1d;color:#fca5a5;display:none;" onclick="deleteSelectedRecordings()">🗑 Delete Selected</button>
    </div>
    <div id="rec-files-empty" style="display:none;color:#64748b;font-size:13px;">No recording files found.</div>
    <div id="rec-files-list" style="max-height:320px;overflow-y:auto;"></div>
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
  <div class="tt-imdb" id="tt-imdb" style="display:none;margin-top:4px;font-size:11px;color:#fbbf24;"></div>
  <div class="tt-plex" id="tt-plex" style="display:none;margin-top:5px;padding-top:5px;border-top:1px solid #2d1f5e;font-size:10px;color:#a78bfa;font-family:monospace;"></div>
</div>

<!-- STORAGE -->
<div id="pane-storage" class="pane">
  <div class="card">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">
      <h2 style="margin:0;">💾 Storage</h2>
      <button class="btn btn-ghost btn-sm" style="margin-left:auto;" onclick="loadStorageTab()">↻ Refresh</button>
    </div>
    <div id="storage-tab-list"></div>
  </div>
  <div class="card" style="margin-top:12px;">
    <h2 style="margin:0 0 14px;">⚠ Warning Thresholds</h2>
    <p style="font-size:13px;color:#64748b;margin:0 0 16px;">Set the % used at which the storage bar turns yellow or red.</p>
    <div style="display:flex;gap:24px;align-items:flex-end;flex-wrap:wrap;">
      <div>
        <label style="display:block;font-size:12px;color:#94a3b8;margin-bottom:4px;">🟡 Yellow warning at</label>
        <div style="display:flex;align-items:center;gap:6px;">
          <input id="thresh-yellow" type="number" min="1" max="99" style="width:70px;padding:6px 8px;background:#1a1a1a;border:1px solid #2d2d2d;border-radius:6px;color:#e2e8f0;font-size:14px;">
          <span style="color:#64748b;font-size:13px;">%</span>
        </div>
      </div>
      <div>
        <label style="display:block;font-size:12px;color:#94a3b8;margin-bottom:4px;">🔴 Red warning at</label>
        <div style="display:flex;align-items:center;gap:6px;">
          <input id="thresh-red" type="number" min="1" max="99" style="width:70px;padding:6px 8px;background:#1a1a1a;border:1px solid #2d2d2d;border-radius:6px;color:#e2e8f0;font-size:14px;">
          <span style="color:#64748b;font-size:13px;">%</span>
        </div>
      </div>
      <button class="btn btn-primary" onclick="saveThresholds()">Save Thresholds</button>
    </div>
    <div id="thresh-status" class="status-msg" style="margin-top:10px;"></div>
  </div>
  <div class="card" style="margin-top:12px;">
    <h2 style="margin:0 0 14px;">📁 Monitored Paths</h2>
    <p style="font-size:13px;color:#64748b;margin:0 0 16px;">These paths are read from Settings. Change them there to monitor different volumes.</p>
    <div id="storage-paths-list"></div>
    <div style="margin-top:14px;padding-top:14px;border-top:1px solid #1e293b;">
      <label style="display:block;font-size:12px;color:#94a3b8;margin-bottom:8px;">Add custom path to monitor</label>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <input id="custom-path-label" placeholder="Label (e.g. Downloads)" style="width:160px;padding:6px 8px;background:#1a1a1a;border:1px solid #2d2d2d;border-radius:6px;color:#e2e8f0;font-size:13px;">
        <input id="custom-path-val" placeholder="/path/to/folder" style="flex:1;min-width:200px;padding:6px 8px;background:#1a1a1a;border:1px solid #2d2d2d;border-radius:6px;color:#e2e8f0;font-size:13px;">
        <button class="btn btn-ghost btn-sm" onclick="addCustomPath()">+ Add</button>
      </div>
    </div>
  </div>
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
const CH_NAME_W  = 160;         // channel label column width in px

function _calcGuideHours() {
  // Fill available width at PX_PER_MIN; minimum 4h, maximum 12h, snap to whole hours
  const avail = window.innerWidth - CH_NAME_W - 20; // 20 for scrollbar/padding
  return Math.min(12, Math.max(4, Math.floor(avail / (PX_PER_MIN * 60))));
}
_guideHours = _calcGuideHours();

// Refetch when window is resized to a different hour count
let _lastGuideHours = _guideHours;
window.addEventListener('resize', () => {
  const h = _calcGuideHours();
  if (h !== _lastGuideHours) { _lastGuideHours = h; _guideHours = h; fetchAndRenderGuide(); }
});

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
  try {
    const s = await (await fetch('/epg-web/api/status')).json();
    if (s.programmes > 0) {
      await fetchAndRenderGuide();
    }
  } catch(e) { console.warn('[autoLoad] status fetch failed:', e.message); return; }
  let sd;
  try {
    sd = await (await fetch('/epg-web/api/fetch-sd/status')).json();
  } catch(e) { console.warn('[autoLoad] fetch-sd/status failed:', e.message); return; }
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
        sdEl.innerHTML = `✅ SD done — ${r.inserted} new, ${r.total_loaded.toLocaleString()} total &nbsp;<button class="btn btn-ghost btn-sm" onclick="fetchAndRenderGuide()">↻ Reload Guide</button>`;
        sdEl.className = 'status-msg ok';
        clearInterval(_sdPoll);
      }
    }, 2000);
  }
}
// Restore saved guide mode before first render
(function() {
  const saved = localStorage.getItem('epg_guide_mode');
  if (saved) { const el = document.getElementById('guide-ch-mode'); if (el) el.value = saved; }
})();
autoLoad();
loadStorageBar();
setInterval(loadStorageBar, 5 * 60 * 1000); // refresh every 5 min

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name) {
  const names = ['guide','recommendations','channels','247','schedule','conversions','storage'];
  document.querySelectorAll('.tab').forEach((t,i) =>
    t.classList.toggle('active', names[i] === name));
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-'+name).classList.add('active');
  if (name === 'recommendations') loadRecs();
  if (name === 'channels') loadChannels();
  if (name === '247') load247();
  if (name === 'schedule') { loadSchedule(); loadSeriesRecordings(); loadRecFiles(); }
  if (name === 'conversions') { loadTsFiles(); pollConversions(); }
  if (name === 'storage') loadStorageTab();
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
async function fetchGuide() {
  const btn = document.getElementById('btn-fetch-guide');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Fetching…';
  setGS('Fetching XMLTV from PrimeStreams — this may take 30-60s…');
  try {
    const r = await fetch('/epg-web/api/fetch-guide', {method:'POST'});
    const d = await r.json();
    if (d.error) { setGS('Fetch error: '+d.error, 'err'); return; }
    const newInfo = d.new_rows > 0 ? ` (+${d.new_rows.toLocaleString()} new)` : ' (no new rows)';
    setGS(`Fetched ${(d.bytes/1024).toFixed(0)} KB · ${d.count.toLocaleString()} programmes${newInfo}`, 'ok');
    await fetchAndRenderGuide();
  } catch(e) { setGS('Fetch failed: '+e.message,'err'); }
  finally { btn.disabled=false; btn.textContent='\\u2B07 Fetch Guide'; }
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
      sdEl.innerHTML = `✅ SD done — ${r.inserted} new, ${r.total_loaded.toLocaleString()} total &nbsp;<button class="btn btn-ghost btn-sm" onclick="fetchAndRenderGuide()">↻ Reload Guide</button>`;
      sdEl.className = 'status-msg ok';
      clearInterval(_sdPoll); btn.disabled = false;
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
function guideJumpNow() {
  _guideWindowStart = new Date().toISOString();
  fetchAndRenderGuide();
}
let _searchTimer = null, _searchSeq = 0;
function onSearchInput(val) {
  clearTimeout(_searchTimer);
  _chIdFilter = '';  // clear exact-id filter when user is typing a new search
  const dd = document.getElementById('search-dropdown');
  if (val.length < 2) { dd.style.display = 'none'; fetchAndRenderGuide(); return; }
  const seq = ++_searchSeq;
  _searchTimer = setTimeout(async () => {
    const r = await fetch('/epg-web/api/search?q=' + encodeURIComponent(val));
    const d = await r.json();
    if (seq !== _searchSeq) return; // stale response — a newer search is in flight
    let html = '';
    if (d.channels && d.channels.length) {
      html += '<div style="padding:6px 12px;font-size:11px;color:#3b82f6;font-weight:600;text-transform:uppercase;letter-spacing:.05em;">📺 Channels</div>';
      html += d.channels.map(c =>
        `<div class="sr" style="padding:8px 14px;cursor:pointer;font-size:13px;color:#e2e8f0;border-bottom:1px solid #1e293b;display:flex;align-items:center;justify-content:space-between;">
          <span onclick='jumpToChannel(${JSON.stringify(c.id).replace(/'/g,"\\'")},"${c.name.replace(/"/g,'&quot;')}")'style="flex:1">${esc(c.name)}</span>
          <span onclick='toggleFav(${JSON.stringify(c.id).replace(/'/g,"\\'")},this)' title="Toggle favorite" style="padding:0 4px;color:${c.fav?'#f59e0b':'#475569'};font-size:16px;">${c.fav?'★':'☆'}</span>
        </div>`
      ).join('');
    }
    if (d.programs && d.programs.length) {
      html += '<div style="padding:6px 12px;font-size:11px;color:#f59e0b;font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-top:4px;">🎬 On Now / Upcoming</div>';
      html += d.programs.map(p =>
        `<div class="sr" onclick="searchOpenProg(${JSON.stringify(p.title).replace(/"/g,'&quot;')})" style="padding:8px 14px;cursor:pointer;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:10px;">
          <span style="font-size:12px;min-width:70px;color:${p.on_now?'#22c55e':'#94a3b8'};font-weight:${p.on_now?'600':'400'};">${esc(p.start_fmt)}</span>
          <span style="flex:1;font-size:13px;color:#e2e8f0;">${esc(p.title)}</span>
          <span style="font-size:11px;color:#64748b;text-align:right;">${p.has_stream ? '📡 ' : ''}${esc(p.channel_name)}</span>
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
  _chIdFilter = '';
  _chOffset = 0; fetchAndRenderGuide();
}
function jumpToChannel(id, name) {
  document.getElementById('search-dropdown').style.display = 'none';
  document.getElementById('ch-filter').value = name;
  _chIdFilter = id;
  _chOffset = 0; fetchAndRenderGuide();
}
async function syncStreams() {
  const btn = document.getElementById('btn-sync-streams');
  const status = document.getElementById('sync-status');
  btn.disabled = true; btn.textContent = '⏳ Syncing…';
  status.style.display = ''; status.style.color = '#94a3b8';
  status.textContent = 'Fetching latest stream IDs from PrimeStreams…';
  try {
    const r = await fetch('/epg-web/api/sync-streams', {method:'POST'});
    const d = await r.json();
    if (d.error) { status.style.color='#ef4444'; status.textContent = '❌ ' + d.error; }
    else {
      const lines = d.updated.map(u => `${u.channel}: ${u.old} → ${u.new} (${u.ps_name})`);
      status.style.color = '#22c55e';
      status.innerHTML = `✅ Updated ${d.updated.length} stream IDs, ${d.unchanged} unchanged, ${d.not_found} not found in PrimeStreams.` +
        (lines.length ? '<br><small style="color:#94a3b8">' + lines.join('<br>') + '</small>' : '');
    }
  } catch(e) { status.style.color='#ef4444'; status.textContent = '❌ ' + e.message; }
  btn.disabled = false; btn.textContent = '🔄 Sync Streams';
}
async function toggleFav(channelId, starEl) {
  const r = await fetch('/epg-web/api/channel/favorite', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({channel_id: channelId})});
  const d = await r.json();
  if (d.ok) {
    starEl.textContent = d.favorite ? '★' : '☆';
    starEl.style.color = d.favorite ? '#f59e0b' : '#475569';
  }
}
async function toggleGuideFav(event, channelId) {
  event.stopPropagation();
  const r = await fetch('/epg-web/api/channel/favorite', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({channel_id: channelId})
  });
  const d = await r.json();
  if (d.ok && _guideData) {
    const ch = (_guideData.channels || []).find(c => c.id === channelId);
    if (ch) ch.favorite = d.favorite;
    renderGuide();
  }
}
function focusGuideChannel(channelId, channelName) {
  _chIdFilter = channelId;
  document.getElementById('ch-filter').value = channelName;
  _chOffset = 0;
  fetchAndRenderGuide();
}
async function searchOpenProg(title, episodeTitle, seasonNum, episodeNum) {
  // Strip year suffix e.g. "Minority Report (2002)" → "Minority Report"
  const baseTitle = title.replace(/\s*\(\d{4}\)\s*$/, '').trim();
  // Build query — include episode title if available
  let url = '/epg-web/api/search?q=' + encodeURIComponent(baseTitle);
  if (episodeTitle) url += '&episode=' + encodeURIComponent(episodeTitle);
  if (seasonNum)    url += '&season='  + encodeURIComponent(seasonNum);
  if (episodeNum)   url += '&ep='      + encodeURIComponent(episodeNum);
  try {
    const r = await fetch(url);
    const d = await r.json();
    const progs = (d.programs || []).filter(p => p.start_ts && p.stop_ts);
    if (progs.length) {
      // If episode info given, prefer exact episode match; fall back to first result
      let match = progs[0];
      if (episodeTitle) {
        const epNorm = episodeTitle.toLowerCase();
        const exact = progs.find(p => (p.episode_title||'').toLowerCase() === epNorm);
        if (exact) match = exact;
      }
      openProg(match);
      if (episodeTitle && !(progs.find(p => (p.episode_title||'').toLowerCase() === episodeTitle.toLowerCase()))) {
        // Warn that exact episode wasn't found — showing next available airing instead
        document.getElementById('pm-status').textContent =
          `⚠ Exact episode not found — showing next airing of "${baseTitle}"`;
        document.getElementById('pm-status').className = 'status-msg';
        document.getElementById('pm-status').style.display = '';
      }
      return;
    }
  } catch(e) {}
  // Fallback: switch to Guide tab, trigger the search dropdown
  switchTab('guide');
  const el = document.getElementById('ch-filter');
  if (el) { el.value = baseTitle; el.focus(); onSearchInput(baseTitle); }
}
// Close dropdown when clicking outside (but not when interacting with the prog modal)
document.addEventListener('click', e => {
  if (!e.target.closest('#ch-filter') &&
      !e.target.closest('#search-dropdown') &&
      !e.target.closest('#prog-modal-overlay'))
    document.getElementById('search-dropdown').style.display = 'none';
});

let _plexTitles = new Set();
let _plexEpisodes = new Set();
function _normTitle(t) {
  return (t||'').toLowerCase().replace(/[^a-z0-9 ]/g, '').replace(/\s+/g, ' ').trim();
}
// Strip trailing (YYYY) before normalizing — Plex folders have year stripped already
function _plexNorm(t) { return _normTitle((t||'').replace(/\s*\(\d{4}\)\s*$/,'')); }
let _plexTitlesReady = false;
let _plexTitlesPromise = null;
async function loadPlexTitles() {
  try {
    const r = await fetch('/epg-web/api/plex/titles');
    const d = await r.json();
    _plexTitles = new Set((d.titles||[]).map(_normTitle));
    _plexTitlesReady = true;
  } catch(e) { _plexTitlesReady = true; }
}
_plexTitlesPromise = loadPlexTitles();
async function loadPlexEpisodes() {
  try {
    const r = await fetch('/epg-web/api/plex/episodes');
    const d = await r.json();
    _plexEpisodes = new Set(d.episodes || []);
  } catch(e) {}
}
const _plexEpisodesPromise = loadPlexEpisodes();

// key: channel_id+'|'+start_ts → 'recording'|'queued'|'scheduled'|...
let _guideRecMap = {};
async function refreshGuideRecMap() {
  try {
    const d = await (await fetch('/epg-web/api/record/status')).json();
    const m = {};
    for (const r of Object.values(d.recordings || {})) {
      const k = (r.channel_id||'') + '|' + Math.round(r.start_ts);
      m[k] = (r.status||'').toLowerCase();
    }
    _guideRecMap = m;
  } catch(e) {}
}

let _progMap = {};

async function quickRecord(key, btnEl) {
  const p = _progMap[key];
  if (!p) return;
  btnEl.textContent = '⏱';
  btnEl.className = 'rec-btn pending';
  btnEl.onclick = null;  // prevent double-click immediately
  try {
    const r = await fetch('/epg-web/api/record', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({title: p.title, channel_id: p.channel_id,
                            start_ts: p.start_ts, stop_ts: p.stop_ts,
                            episode_title: p.episode_title || '',
                            season_num: p.season_num, episode_num: p.episode_num})
    });
    const d = await r.json();
    if (d.ok && !d.error) {
      btnEl.textContent = '⏱';
      _guideRecMap[key] = 'queued';
    } else {
      btnEl.textContent = '⏺';
      btnEl.className = 'rec-btn';
      btnEl.onclick = e => { e.stopPropagation(); quickRecord(key, btnEl); };
      if (d.error) alert(d.error);
    }
  } catch(e) {
    btnEl.textContent = '⏺';
    btnEl.className = 'rec-btn';
    btnEl.onclick = ev => { ev.stopPropagation(); quickRecord(key, btnEl); };
  }
}

async function playPlex(title) {
  const r = await fetch('/epg-web/api/plex/play', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({title})
  });
  const d = await r.json();
  if (!d.ok) alert('Could not open in VLC: ' + (d.error||'unknown error'));
}

let _chIdFilter = '';
async function fetchAndRenderGuide() {
  if (!_plexTitlesReady && _plexTitlesPromise) await _plexTitlesPromise;
  if (_plexEpisodesPromise) await _plexEpisodesPromise;
  const params = new URLSearchParams();
  if (_guideWindowStart) params.set('start', _guideWindowStart);
  params.set('hours', _guideHours);
  const ch = document.getElementById('ch-filter').value.trim();
  if (_chIdFilter) params.set('ch_id', _chIdFilter);
  else if (ch) params.set('ch', ch);
  const mode = document.getElementById('guide-ch-mode').value;
  if (mode === 'fav')   params.set('fav',   '1');
  if (mode === 'movie') params.set('movie', '1');
  if (mode === 'ps')    params.set('ps',    '1');
  if (mode === 'ps_episode') params.set('ps_episode', '1');
  if (mode === 'sd')    params.set('sd',    '1');
  params.set('ch_offset', _chOffset);
  try {
    const [r] = await Promise.all([
      fetch('/epg-web/api/guide?' + params),
      refreshGuideRecMap()
    ]);
    const d = await r.json();
    if (d.error) { setGS(d.error,'err'); return; }
    _guideData = d;
    if (!_guideWindowStart) _guideWindowStart = d.window_start;
    renderGuide();
    updatePlexQuality();
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
  _progMap = {};
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
  let favoriteGroup = '';
  for (const ch of channels) {
    if (ch.favorite_group && ch.favorite_group !== favoriteGroup) {
      favoriteGroup = ch.favorite_group;
      rowsHTML += `<div class="guide-row" style="background:#111827;border-top:1px solid #334155;">
        <div class="ch-name" style="width:160px;color:#fbbf24;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;">${esc(favoriteGroup)}</div>
        <div style="width:${totalPx}px;"></div>
      </div>`;
    }
    const chProgs = d.programmes.filter(p => p.channel_id === ch.id);
    let progHTML = `<div class="prog-row" style="width:${totalPx}px;">`;
    // now line
    if (nowTs > wsTs && nowTs < weTs) {
      progHTML += `<div class="now-line" style="left:${nowOffPx}px;"></div>`;
    }
    if (ch.no_data && chProgs.length === 0) {
      progHTML += `<div class="prog-block" style="left:0;width:${totalPx - 2}px;opacity:0.35;font-style:italic;cursor:default;background:#555;">No guide data</div>`;
    }
    for (const p of chProgs) {
      const pStart = Math.max(p.start_ts, wsTs);
      const pEnd   = Math.min(p.stop_ts,  weTs);
      const left   = (pStart - wsTs) / 60 * PX_PER_MIN;
      const width  = Math.max(2, (pEnd - pStart) / 60 * PX_PER_MIN - 2);
      const isNow  = p.start_ts <= nowTs && p.stop_ts > nowTs;
      const pd = JSON.stringify(p).replace(/'/g, "\\'");
      const hasPlex   = _plexTitles.has(_plexNorm(p.title));
      const plexEpisodeKey = (p.season_num != null && p.episode_num != null)
        ? `${_normTitle(p.title).replace(/\s/g,'')}|${p.season_num}|${p.episode_num}` : '';
      const hasPlexEpisode = !!plexEpisodeKey && _plexEpisodes.has(plexEpisodeKey);
      const recKey    = (p.channel_id||'') + '|' + Math.round(p.start_ts);
      const recSt     = _guideRecMap[recKey] || '';
      const isRecording = recSt === 'recording';
      const isScheduled = recSt === 'queued' || recSt === 'scheduled' || recSt === 'to_record';
      const recKey2   = (p.channel_id||'') + '|' + Math.round(p.start_ts);
      _progMap[recKey2] = p;
      const normKey   = _normTitle(p.title);
      const cachedQ   = hasPlex && _plexInfoCache[normKey] ? _resLabel(_plexInfoCache[normKey]) : '';
      const plexBtn   = hasPlex ? `<span class="plex-play-btn" title="Play in VLC" data-ptitle="${esc(p.title)}" onclick="event.stopPropagation();playPlex(this.dataset.ptitle)">▶</span><span class="plex-qual" data-qtitle="${esc(p.title)}" id="pq-${normKey.replace(/[^a-z0-9]/g,'')}">${cachedQ}</span>` : '';
      const recBtnEl  = (!hasPlex && !isRecording && !isScheduled)
                        ? `<span class="rec-btn" title="${hasPlexEpisode?'Re-record this Plex episode':'Record'}" data-rkey="${esc(recKey2)}" onclick="event.stopPropagation();quickRecord(this.dataset.rkey,this)">${hasPlexEpisode?'↻':'⏺'}</span>` : '';
      const isMovie  = p.prog_type === 'MV' || (!p.prog_type && /\(\d{4}\)\s*$/.test(p.title));
      const isSeries = !isMovie && (p.prog_type === 'EP' || p.prog_type === 'SH' || p.season_num != null || (p.episode_title && p.episode_title.length > 0));
      const catI    = isMovie  ? {cls:'cat-movie',  badge:'MOV', title:'Movie'}
                    : isSeries ? {cls:'cat-series', badge:'TV', title:'Series'}
                    : _catInfo(p.category || '');
      const catBadge = catI.badge ? `<span class="cat-badge" title="${catI.title || catI.badge}">${catI.badge}</span>` : '';
      const sq = p.stream_quality || null;
      const streamBadge = sq && sq.height
        ? `<span class="stream-qual ready" title="Incoming recording stream: ${sq.width}×${sq.height}${sq.fps ? ' at '+sq.fps+' fps' : ''}">${sq.height >= 2160 ? '4K' : sq.height+'p'}${sq.fps ? ' · '+sq.fps+'fps' : ''}</span>`
        : '';
      const badges    = (hasPlexEpisode ? '<span class="plex-qual" title="Episode already in Plex" style="color:#a78bfa;">IN PLEX</span>' : '')
                      + (isRecording ? '<span class="rec-dot" title="Recording now">⏺</span>' : '')
                      + (isScheduled ? '<span class="sched-dot" title="Scheduled to record">⏱</span>' : '')
                      + plexBtn + recBtnEl;
      const epParts = [];
      if (p.season_num != null) epParts.push(`S${p.season_num}${p.episode_num != null ? 'E'+p.episode_num : ''}`);
      if (p.episode_title) epParts.push(p.episode_title);
      const epLine = epParts.join(' · ');
      progHTML += `<div class="prog-block${isNow?' now':''}${(hasPlex || hasPlexEpisode)?' in-plex':''}${catI.cls?' '+catI.cls:''}"
        style="left:${left}px;width:${width}px;"
        onmouseenter="showTip(event,${pd.replace(/"/g,'&quot;')})"
        onmouseleave="hideTip()"
        onclick="openProg(${pd.replace(/"/g,'&quot;')})">
        <div class="prog-row-top">${badges}${catBadge}${streamBadge}<span class="prog-title">${esc(p.title)}</span></div>
        ${epLine ? `<span class="prog-ep">${esc(epLine)}</span>` : ''}
      </div>`;
    }
    progHTML += '</div>';
    rowsHTML += `<div class="guide-row">
      <div class="ch-name" title="${esc(ch.name)}" style="display:flex;align-items:center;">
        <button class="guide-ch-star${ch.favorite?' is-fav':''}" title="${ch.favorite?'Remove from favorites':'Add to favorites'}" onclick="toggleGuideFav(event,${JSON.stringify(ch.id).replace(/"/g,'&quot;')})">${ch.favorite?'★':'☆'}</button>
        <span class="ch-name-label" onclick="focusGuideChannel(${JSON.stringify(ch.id).replace(/"/g,'&quot;')},${JSON.stringify(ch.name).replace(/"/g,'&quot;')})">${esc(ch.name)}</span>
      </div>
      ${progHTML}
    </div>`;
  }

  document.getElementById('guide-inner').innerHTML = timeHTML + rowsHTML;
  document.getElementById('guide-wrap').style.display = 'block';
}

function _catInfo(cat) {
  if (!cat) return {cls:'', badge:''};
  const c = cat.toLowerCase();
  const sports = ['baseball','basketball','football','soccer','hockey','golf','tennis','boxing','wrestling','motor','cycling','swimming','track','racing','volleyball','martial','lacrosse','rugby','cricket','skiing','curling','softball','sport'];
  const news   = ['news','newsmagazine','public affairs','weather'];
  const kids   = ['animated','children','kids'];
  const talk   = ['talk','game show','variety','cooking','consumer','home shopping','infomercial'];
  const scripted = ['drama','sitcom','comedy','crime','thriller','mystery','romance','sci-fi','horror','adventure','fantasy','western','action'];
  if (sports.some(s => c.includes(s)))   return {cls:'cat-sports',   badge:'SPORT'};
  if (news.some(s => c.includes(s)))     return {cls:'cat-news',     badge:'NEWS'};
  if (kids.some(s => c.includes(s)))     return {cls:'cat-kids',     badge:'KIDS'};
  if (c.includes('documentary'))         return {cls:'cat-doc',      badge:'DOC'};
  if (c.includes('reality'))             return {cls:'cat-reality',  badge:'REAL'};
  if (talk.some(s => c.includes(s)))     return {cls:'cat-talk',     badge:'TALK'};
  if (scripted.some(s => c.includes(s))) return {cls:'cat-scripted', badge:'SERIES'};
  return {cls:'', badge:''};
}

function _resLabel(infoStr) {
  if (!infoStr) return '';
  const m = (infoStr||'').match(/(\d+)×(\d+)/);
  if (!m) return '';
  const h = parseInt(m[2]);
  if (h >= 2160) return '4K';
  if (h >= 1080) return '1080p';
  if (h >= 720)  return '720p';
  return h + 'p';
}

async function updatePlexQuality() {
  const spans = document.querySelectorAll('.plex-qual[data-qtitle]');
  const seen = new Set();
  for (const span of spans) {
    const title = span.dataset.qtitle;
    if (!title || seen.has(title)) { if (seen.has(title) && _plexInfoCache[_normTitle(title)]) span.textContent = _resLabel(_plexInfoCache[_normTitle(title)]); continue; }
    seen.add(title);
    const key = _normTitle(title);
    if (_plexInfoCache[key]) { span.textContent = _resLabel(_plexInfoCache[key]); continue; }
    fetch(`/epg-web/api/plex/info?title=${encodeURIComponent(title)}`)
      .then(r => r.json()).then(pi => {
        if (!pi.found) return;
        const parts = [];
        if (pi.width && pi.height) parts.push(`${pi.width}×${pi.height}`);
        if (pi.fps)         parts.push(`${pi.fps} fps`);
        if (pi.video_codec) parts.push(pi.video_codec);
        if (pi.audio_codec) parts.push(pi.audio_codec + (pi.channels ? ` ${pi.channels}ch` : ''));
        if (pi.size)        parts.push(pi.size);
        _plexInfoCache[key] = parts.join(' · ');
        document.querySelectorAll(`.plex-qual[data-qtitle="${title.replace(/"/g,'\\"')}"]`)
          .forEach(el => el.textContent = _resLabel(_plexInfoCache[key]));
      }).catch(()=>{});
  }
}

async function updateGuideStreamQuality() {
  // Probe a modest number of distinct channels at a time.  The result is the
  // actual incoming stream quality, not a provider label, and is cached by
  // channel for the rest of the browser session.
  const spans = [...document.querySelectorAll('.stream-qual[data-stream-channel]')];
  const channels = [...new Set(spans.map(s => s.dataset.streamChannel).filter(Boolean))].slice(0, 12);
  async function probeChannel(channelId) {
    let info = _streamInfoCache[channelId];
    if (info === undefined) {
      try {
        const response = await fetch(`/epg-web/api/stream-info?channel_id=${encodeURIComponent(channelId)}`);
        info = response.ok ? await response.json() : null;
      } catch (e) { info = null; }
      _streamInfoCache[channelId] = info;
    }
    if (!info || !info.height) return;
    const label = `${info.height >= 2160 ? '4K' : info.height + 'p'}${info.fps ? ' · ' + info.fps + 'fps' : ''}`;
    document.querySelectorAll('.stream-qual[data-stream-channel]').forEach(el => {
      if (el.dataset.streamChannel === channelId) {
        el.textContent = label;
        el.classList.add('ready');
        el.title = `Incoming recording stream: ${info.width}×${info.height}${info.fps ? ' at ' + info.fps + ' fps' : ''}`;
      }
    });
  }
  // Four parallel probes make the badges appear promptly, while keeping the
  // incoming-stream checks gentle on PrimeStreams and on this Flask process.
  for (let offset = 0; offset < channels.length; offset += 4) {
    await Promise.all(channels.slice(offset, offset + 4).map(probeChannel));
  }
}

// Tooltip
const _plexInfoCache = {};
const _streamInfoCache = {};
const _imdbCache     = {};
function showTip(e, p) {
  const tt      = document.getElementById('tooltip');
  const ttPlex  = document.getElementById('tt-plex');
  const ttImdb  = document.getElementById('tt-imdb');
  document.getElementById('tt-title').textContent = p.title;
  document.getElementById('tt-time').textContent  = p.start_fmt + ' – ' + p.stop_fmt;
  document.getElementById('tt-desc').textContent  = p.desc || p.category || '';
  ttPlex.style.display = 'none';
  ttImdb.style.display = 'none';
  tt.style.display = 'block';
  tt.style.left    = Math.min(e.clientX + 12, window.innerWidth - 320) + 'px';
  tt.style.top     = Math.min(e.clientY + 12, window.innerHeight - 150) + 'px';

  const key = _normTitle(p.title);

  // IMDb info (all titles)
  if (_imdbCache[key] !== undefined) {
    if (_imdbCache[key]) { ttImdb.textContent = _imdbCache[key]; ttImdb.style.display = 'block'; }
  } else {
    _imdbCache[key] = '';
    fetch(`/epg-web/api/prog-info?title=${encodeURIComponent(p.title)}&desc=${encodeURIComponent(p.desc||'')}`)
      .then(r => r.json()).then(info => {
        const parts = [];
        if (info.imdb_rating) parts.push(`★ ${info.imdb_rating}`);
        if (info.year)        parts.push(info.year);
        if (info.genre)       parts.push(info.genre.split(',')[0].trim());
        if (info.rated && info.rated !== 'N/A') parts.push(info.rated);
        const txt = parts.join(' · ');
        _imdbCache[key] = txt;
        if (tt.style.display !== 'none' && txt) {
          ttImdb.textContent = txt; ttImdb.style.display = 'block';
        }
      }).catch(()=>{});
  }

  // Plex file specs (Plex titles only)
  const plexKey = _plexNorm(p.title);
  if (_plexTitles.has(plexKey)) {
    if (_plexInfoCache[plexKey]) {
      ttPlex.textContent = _plexInfoCache[plexKey]; ttPlex.style.display = 'block';
    } else {
      fetch(`/epg-web/api/plex/info?title=${encodeURIComponent(p.title)}`)
        .then(r => r.json()).then(pi => {
          if (!pi.found) return;
          const parts = [];
          if (pi.width && pi.height) parts.push(`${pi.width}×${pi.height}`);
          if (pi.fps)         parts.push(`${pi.fps} fps`);
          if (pi.video_codec) parts.push(pi.video_codec);
          if (pi.audio_codec) parts.push(pi.audio_codec + (pi.channels ? ` ${pi.channels}ch` : ''));
          if (pi.size)        parts.push(pi.size);
          const txt = parts.join(' · ');
          _plexInfoCache[plexKey] = txt;
          if (tt.style.display !== 'none' && txt) {
            ttPlex.textContent = txt; ttPlex.style.display = 'block';
          }
        }).catch(()=>{});
    }
  }
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

  // Fetch enriched info (skip OMDB/TMDB for news/sports/talk/live)
  let info = {};
  try {
    const params = new URLSearchParams({title: p.title});
    if (p.desc)     params.set('desc', p.desc);
    if (p.year)     params.set('year', p.year);
    if (p.category) params.set('category', p.category);
    const r  = await fetch(`/epg-web/api/prog-info?${params}`);
    if (r.ok) info = await r.json();
  } catch(e) {}

  // Populate modal
  document.getElementById('pm-title').textContent = info.title || p.title;
  document.getElementById('pm-air').textContent   = (p.channel || p.channel_id) + '  ·  ' + p.start_fmt + ' – ' + p.stop_fmt;
  const epEl = document.getElementById('pm-ep');
  const epParts = [];
  if (p.season_num != null) epParts.push(`S${p.season_num}${p.episode_num != null ? 'E'+p.episode_num : ''}`);
  if (p.episode_title) epParts.push(p.episode_title);
  if (epParts.length) { epEl.textContent = epParts.join(' · '); epEl.style.display = ''; }
  else { epEl.style.display = 'none'; }
  document.getElementById('pm-plot').textContent  = info.plot || p.desc || p.category || '';
  document.getElementById('pm-year').textContent  = info.year || '';
  document.getElementById('pm-rated').textContent = info.rated || '';
  document.getElementById('pm-genre').textContent = info.genre || '';
  document.getElementById('pm-imdb').textContent  = info.imdb_rating ? '★ ' + info.imdb_rating : '';
  document.getElementById('pm-actors').textContent   = info.actors  ? '🎭 ' + info.actors  : '';
  document.getElementById('pm-director').textContent = info.director ? '🎬 ' + info.director : '';
  const imdbLink = document.getElementById('pm-imdb-link');
  if (info.imdb_id) {
    imdbLink.href = 'https://www.imdb.com/title/' + info.imdb_id + '/';
    imdbLink.style.display = '';
  } else { imdbLink.style.display = 'none'; }

  const libBadge = document.getElementById('pm-library-badge');
  if (_plexTitles.has(_plexNorm(p.title))) {
    libBadge.textContent = '▶ IN PLEX';
    libBadge.style.background = '#2d1f5e'; libBadge.style.color = '#a78bfa';
    libBadge.style.display = '';
  } else { libBadge.style.display = 'none'; }

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

  // Plex file info
  const plexWrap = document.getElementById('pm-plex-wrap');
  plexWrap.style.display = 'none';
  if (_plexTitles.has(_plexNorm(p.title))) {
    try {
      const pr = await fetch(`/epg-web/api/plex/info?title=${encodeURIComponent(p.title)}`);
      const pi = await pr.json();
      if (pi.found) {
        const parts = [];
        if (pi.width && pi.height) parts.push(`${pi.width}×${pi.height}`);
        if (pi.fps)         parts.push(`${pi.fps} fps`);
        if (pi.video_codec) parts.push(pi.video_codec);
        if (pi.audio_codec) parts.push(pi.audio_codec + (pi.channels ? ` ${pi.channels}ch` : ''));
        if (pi.size)        parts.push(pi.size);
        document.getElementById('pm-plex-info').textContent = parts.join(' · ') || pi.file;
        plexWrap.style.display = 'block';
      }
    } catch(e) {}
  }

  // This is not the guide's quality hint: it is a brief ffprobe of the exact
  // PrimeStreams channel which FFmpeg will record.  It is deliberately
  // asynchronous so opening programme details remains instant.
  const streamWrap = document.getElementById('pm-stream-wrap');
  streamWrap.style.display = 'none';
  const modalTitle = p.title;
  async function showIncomingStreamInfo(channelId) {
    try {
      const response = await fetch(`/epg-web/api/stream-info?channel_id=${encodeURIComponent(channelId)}`);
      if (!response.ok || _currentProg?.title !== modalTitle) return;
      const si = await response.json();
      const parts = [];
      if (si.width && si.height) parts.push(`${si.width}×${si.height}`);
      if (si.fps) parts.push(`${si.fps} fps`);
      if (si.video_codec) parts.push(si.video_codec);
      if (si.audio_codec) parts.push(si.audio_codec + (si.audio_channels ? ` ${si.audio_channels}ch` : ''));
      if (si.bitrate) parts.push(`${(si.bitrate / 1000000).toFixed(1)} Mbps`);
      if (parts.length) {
        document.getElementById('pm-stream-info').textContent = parts.join(' · ');
        streamWrap.style.display = 'block';
      }
    } catch (e) { /* Quality information is optional; recording still works. */ }
  }

  // Fetch future airings
  document.getElementById('pm-next-wrap').style.display    = 'none';
  document.getElementById('pm-airings-wrap').style.display = 'none';
  _nextAiring = null;
  try {
    const ar = await (await fetch(`/epg-web/api/airings?title=${encodeURIComponent(p.title)}`)).json();
    if (ar.airings && ar.airings.length > 0) {
      const isSeries = !!ar.is_series;
      // A title can be both a movie and a TV series (for example Bewitched).
      // Once the guide confirms this is a series, refresh the artwork/details
      // explicitly as a series instead of leaving a same-named movie selected.
      if (isSeries && info.media_type !== 'series') {
        try {
          const seriesParams = new URLSearchParams({title: p.title, content_type: 'series'});
          const seriesResponse = await fetch(`/epg-web/api/prog-info?${seriesParams}`);
          if (seriesResponse.ok) {
            info = await seriesResponse.json();
            document.getElementById('pm-title').textContent = info.title || p.title;
            document.getElementById('pm-plot').textContent = info.plot || p.desc || p.category || '';
            document.getElementById('pm-year').textContent = info.year || '';
            document.getElementById('pm-rated').textContent = info.rated || '';
            document.getElementById('pm-genre').textContent = info.genre || '';
            document.getElementById('pm-imdb').textContent = info.imdb_rating ? '★ ' + info.imdb_rating : '';
            document.getElementById('pm-actors').textContent = info.actors ? '🎭 ' + info.actors : '';
            document.getElementById('pm-director').textContent = info.director ? '🎬 ' + info.director : '';
            if (info.imdb_id) { imdbLink.href = 'https://www.imdb.com/title/' + info.imdb_id + '/'; imdbLink.style.display = ''; }
            if (info.poster) { posterEl.src = info.poster; posterEl.style.display = 'block'; posterWrap.style.display = 'block'; }
          }
        } catch(e) {}
      }
      if (isSeries && !epParts.length) {
        epEl.textContent = 'Episode detail not provided for this airing';
        epEl.style.display = '';
      }
      const batchBtn = document.getElementById('pm-series-btn');
      batchBtn.style.display = isSeries ? '' : 'none';
      batchBtn.disabled = false;
      batchBtn.textContent = '📺 Record Identified Episodes';
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
        showIncomingStreamInfo(featPS.channel_id);
        const label = featPS.on_now
          ? `ON NOW  ·  ${featPS.channel_name}  (until ${featPS.stop_fmt})`
          : `${featPS.start_fmt} – ${featPS.stop_fmt}  ·  ${featPS.channel_name}`;
        document.getElementById('pm-next-info').textContent = label;

        // Play button: only when currently airing; reflect current playing state
        const pBtn = document.getElementById('pm-play-btn');
        pBtn.style.display = featPS.on_now ? '' : 'none';
        if (featPS.on_now) {
          const alreadyPlaying = !!_activeStreams[featPS.channel_id];
          pBtn.textContent = alreadyPlaying ? '■ Stop' : '▶ Play';
          pBtn.onclick     = alreadyPlaying ? () => stopStream(featPS.channel_id) : playStream;
        }

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
      // The modal is for recording, so omit guide-only channels with no
      // PrimeStreams stream instead of showing unusable rows.
      window._allAirings = ar.airings.filter(a => a.can_record);
      window._airingsRecMap = recMap;
      window._showUnrecordedOnly = false;
      const unrecBtn = document.getElementById('pm-unrecorded-btn');
      const hasUnrecorded = window._allAirings.some(a => !recMap[a.channel_id+'|'+a.start_ts] && !a.on_now);
      unrecBtn.style.display = hasUnrecorded ? '' : 'none';
      function renderAiringsList() {
        const list = window._showUnrecordedOnly
          ? window._allAirings.filter(a => !window._airingsRecMap[a.channel_id+'|'+a.start_ts] && !a.on_now)
          : window._allAirings;
        const renderRows = (items, unidentified=false) => items.map(a => {
          const key = a.channel_id + '|' + a.start_ts;
          const scheduled = window._airingsRecMap[key];
          const epInfo = (a.season_num != null ? `S${a.season_num}${a.episode_num != null ? 'E'+a.episode_num : ''}` : '') +
                         (a.episode_title ? (a.season_num != null ? ' · ' : '') + a.episode_title : '');
          const episodeKey = (a.season_num != null && a.episode_num != null)
            ? `${_normTitle(p.title).replace(/\s/g,'')}|${a.season_num}|${a.episode_num}` : '';
          const inPlex = !!episodeKey && _plexEpisodes.has(episodeKey);
          return `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #1a2332;font-size:12px;">
            <span style="color:#94a3b8;min-width:170px;">${esc(a.start_fmt)} – ${esc(a.stop_fmt)}</span>
            <span style="color:#64748b;flex:1;">${esc(a.channel_name)}${epInfo ? '<br><span style="color:#475569;font-size:11px;">'+esc(epInfo)+'</span>' : (unidentified ? '<br><span style="color:#64748b;font-size:11px;">No S/E data · one-off only</span>' : '')}</span>
            ${scheduled
              ? `<span style="color:#22c55e;font-size:11px;">✅</span>`
              : (a.can_record && !a.on_now)
                ? `<button class="btn btn-primary btn-sm" title="${inPlex?'Re-record this Plex episode':'Record'}" onclick="recordAiring(${JSON.stringify(a).replace(/"/g,'&quot;')},${JSON.stringify(p.title).replace(/"/g,'&quot;')})">${inPlex?'↻ Re-record':'⏱'}</button>`
                : ``
            }
          </div>`;
        }).join('');
        if (!list.length) {
          document.getElementById('pm-airings-list').innerHTML = '<div style="color:#64748b;font-size:12px;padding:8px 0;">No airings match</div>';
          return;
        }
        if (!isSeries) {
          document.getElementById('pm-airings-list').innerHTML = renderRows(list);
          return;
        }
        const identified = list.filter(a => a.season_num != null && a.episode_num != null);
        const unidentified = list.filter(a => !(a.season_num != null && a.episode_num != null));
        const heading = label => `<div style="padding:10px 0 4px;color:#94a3b8;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">${label}</div>`;
        document.getElementById('pm-airings-list').innerHTML =
          (identified.length ? heading('Episodes with S/E data') + renderRows(identified) : '') +
          (unidentified.length ? heading('Airings without S/E data') + renderRows(unidentified, true) : '');
      }
      window.toggleUnrecorded = function() {
        window._showUnrecordedOnly = !window._showUnrecordedOnly;
        unrecBtn.textContent = window._showUnrecordedOnly ? '📋 Show All' : '🔲 Unscheduled Only';
        renderAiringsList();
      };
      renderAiringsList();
      document.getElementById('pm-airings-wrap').style.display = 'block';
    }
  } catch(e) {}
}

let _nextAiring = null;
let _activeStreams = {};  // { channel_id: {ch_name, title} }

function _updateNowPlaying(streams) {
  _activeStreams = {};
  (streams || []).forEach(s => { _activeStreams[s.channel_id] = s; });
  const bar = document.getElementById('now-playing-bar');
  if (!streams || !streams.length) {
    bar.style.display = 'none'; bar.innerHTML = ''; return;
  }
  bar.style.display = 'flex';
  bar.innerHTML = '<span style="font-size:11px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:.05em;">▶ Now Playing:</span>'
    + streams.map(s => `
    <span style="display:inline-flex;align-items:center;gap:6px;background:#0f2037;border:1px solid #22c55e44;border-radius:20px;padding:3px 10px;font-size:12px;color:#e2e8f0;">
      <span style="color:#22c55e;">▶</span>
      <span>${esc(s.ch_name)}${s.title ? " &middot; " + esc(s.title) : ""}</span>
      <button onclick="stopStream('${s.channel_id}')" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:13px;padding:0 0 0 4px;line-height:1;" title="Stop">✕</button>
    </span>`).join('');
  // Update play button state in open modal
  const pBtn = document.getElementById('pm-play-btn');
  if (pBtn && _nextAiring) {
    const playing = !!_activeStreams[_nextAiring.channel_id];
    pBtn.textContent = playing ? '■ Stop' : '▶ Play';
    pBtn.onclick     = playing ? () => stopStream(_nextAiring.channel_id) : playStream;
  }
}

async function playStream() {
  if (!_nextAiring) return;
  const btn = document.getElementById('pm-play-btn');
  if (Object.keys(_activeStreams).length >= 6) {
    document.getElementById('pm-status').textContent = '❌ Max 6 streams already playing';
    document.getElementById('pm-status').className = 'status-msg err';
    return;
  }
  btn.disabled = true; btn.textContent = '▶ Playing…';
  try {
    const chLabel  = document.getElementById('pm-next-info').textContent || '';
    const progTitle = (_currentProg && _currentProg.title) || '';
    const r = await post('/epg-web/api/play', {
      channel_id: _nextAiring.channel_id,
      ch_name:    _nextAiring.channel_name || _nextAiring.channel_id,
      title:      progTitle,
      ch_label:   chLabel
    });
    btn.disabled = false;
    if (r.ok) {
      _updateNowPlaying(r.streams);
      btn.textContent = '■ Stop';
      btn.onclick = () => stopStream(_nextAiring.channel_id);
    } else {
      btn.textContent = '▶ Play';
      document.getElementById('pm-status').textContent = '❌ ' + (r.error || 'VLC failed');
      document.getElementById('pm-status').className = 'status-msg err';
    }
  } catch(e) {
    btn.disabled = false; btn.textContent = '▶ Play';
    document.getElementById('pm-status').textContent = '❌ ' + e.message;
    document.getElementById('pm-status').className = 'status-msg err';
  }
}

async function stopStream(channelId) {
  const cid = channelId || (_nextAiring && _nextAiring.channel_id) || '';
  const r = await post('/epg-web/api/play/stop', {channel_id: cid});
  if (r.ok) _updateNowPlaying(r.streams);
  // Reset modal play button if stopped channel matches open modal
  if (_nextAiring && cid === _nextAiring.channel_id) {
    const btn = document.getElementById('pm-play-btn');
    if (btn) { btn.textContent = '▶ Play'; btn.disabled = false; btn.onclick = playStream; }
  }
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
    btn.textContent = `✅ Episodes (${r.scheduled})`;
    document.getElementById('pm-status').textContent = `📺 Recurring recording set for "${title}" — ${r.scheduled} identified episodes queued`;
    document.getElementById('pm-status').className = 'status-msg ok';
    loadSeriesRecordings();
  } else {
    btn.disabled = false; btn.textContent = '📺 Record Identified Episodes';
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
      el.innerHTML = '<div style="color:#64748b;font-size:13px;">No recurring recordings set up.</div>';
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

function fmtSize(bytes) {
  if (bytes >= 1e9) return (bytes/1e9).toFixed(2) + ' GB';
  if (bytes >= 1e6) return (bytes/1e6).toFixed(1) + ' MB';
  return (bytes/1e3).toFixed(0) + ' KB';
}

async function loadRecFiles() {
  const list  = document.getElementById('rec-files-list');
  const empty = document.getElementById('rec-files-empty');
  const total = document.getElementById('rec-files-total');
  const delbtn = document.getElementById('rec-delete-btn');
  if (!list) return;
  list.innerHTML = '<div style="color:#64748b;font-size:13px;">Loading…</div>';
  try {
    const d = await (await fetch('/epg-web/api/recordings/files')).json();
    if (!d.ok || !d.files.length) {
      list.innerHTML = ''; empty.style.display = '';
      total.textContent = ''; delbtn.style.display = 'none';
      return;
    }
    empty.style.display = 'none';
    total.textContent = `${d.files.length} files · ${fmtSize(d.total)}`;
    list.innerHTML = d.files.map(f => `
      <div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #1e293b;">
        <input type="checkbox" class="rec-file-chk" data-name="${esc(f.name)}"
               onchange="updateRecDeleteBtn()" style="flex-shrink:0;">
        <span style="flex:1;font-size:12px;color:#e2e8f0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(f.name)}">${esc(f.name)}</span>
        <span style="font-size:11px;color:#64748b;white-space:nowrap;">${fmtSize(f.size)}</span>
        <span style="font-size:11px;color:#475569;white-space:nowrap;">${esc(f.mtime_fmt)}</span>
      </div>`).join('');
    delbtn.style.display = 'none';
  } catch(e) { list.innerHTML = '<div style="color:#ef4444;">Error loading files.</div>'; }
}

function updateRecDeleteBtn() {
  const checked = document.querySelectorAll('.rec-file-chk:checked').length;
  const btn = document.getElementById('rec-delete-btn');
  if (!btn) return;
  btn.style.display = checked > 0 ? '' : 'none';
  btn.textContent = `\\u{1F5D1} Delete Selected (${checked})`;
}

async function deleteSelectedRecordings() {
  const checked = [...document.querySelectorAll('.rec-file-chk:checked')].map(c => c.dataset.name);
  if (!checked.length) return;
  if (!confirm(`Delete ${checked.length} file(s)? This cannot be undone.`)) return;
  const btn = document.getElementById('rec-delete-btn');
  btn.disabled = true; btn.textContent = 'Deleting...';
  const r = await post('/epg-web/api/recordings/delete', {files: checked});
  if (r.errors && r.errors.length) alert('Errors:\\n' + r.errors.join('\\n'));
  await loadRecFiles();
  loadDiskUsage();
}

let _diskWarnYellow = 75;
let _diskWarnRed    = 90;

function _diskColor(pct) {
  return pct >= _diskWarnRed ? '#ef4444' : pct >= _diskWarnYellow ? '#f59e0b' : '#22c55e';
}

async function loadStorageBar() {
  const bar = document.getElementById('storage-bar');
  if (!bar) return;
  try {
    const d = await (await fetch('/epg-web/api/disk')).json();
    if (!d.ok || !d.disks.length) { bar.innerHTML = ''; return; }
    _diskWarnYellow = d.warn_yellow || 75;
    _diskWarnRed    = d.warn_red    || 90;
    bar.innerHTML = d.disks.map(disk => {
      if (disk.error) return `<span style="color:#64748b;">💾 ${esc(disk.label)}: <span style="color:#ef4444;">not mounted</span></span>`;
      const color = _diskColor(disk.pct);
      const freeGB = (disk.free / 1e9).toFixed(1);
      return `<span title="${esc(disk.mount)}" style="display:flex;align-items:center;gap:6px;">
        <span style="color:#94a3b8;">💾 ${esc(disk.label)}</span>
        <span style="display:inline-block;width:60px;height:5px;background:#1e293b;border-radius:3px;overflow:hidden;">
          <span style="display:block;height:100%;width:${disk.pct}%;background:${color};border-radius:3px;"></span>
        </span>
        <span style="color:${color};font-weight:600;">${freeGB} GB free</span>
        <span style="color:#475569;">(${disk.pct}% used)</span>
      </span>`;
    }).join('<span style="color:#1e293b;">│</span>');
  } catch(e) { bar.innerHTML = ''; }
}

function loadDiskUsage() { loadStorageTab(); }  // legacy alias

async function loadStorageTab() {
  const el = document.getElementById('storage-tab-list');
  if (!el) return;
  el.innerHTML = '<div style="color:#64748b;font-size:13px;">Checking…</div>';
  try {
    const d = await (await fetch('/epg-web/api/disk')).json();
    _diskWarnYellow = d.warn_yellow || 75;
    _diskWarnRed    = d.warn_red    || 90;
    // Populate threshold inputs
    const yi = document.getElementById('thresh-yellow');
    const ri = document.getElementById('thresh-red');
    if (yi) yi.value = _diskWarnYellow;
    if (ri) ri.value = _diskWarnRed;
    // Populate paths list
    const cfg2 = await (await fetch('/epg-web/api/config')).json();
    const pl = document.getElementById('storage-paths-list');
    if (pl) {
      const builtIn = [
        {label: 'Mac (recordings)', key: 'rec_path',  val: cfg2.rec_path  || ''},
        {label: 'NAS – Plex',       key: 'plex_path', val: cfg2.plex_path || ''},
        {label: 'NAS – EPG',        key: 'guide_path',val: cfg2.guide_path|| ''},
      ];
      const custom = cfg2.disk_custom_paths || [];
      pl.innerHTML = builtIn.map(b => `
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #1e293b;font-size:13px;">
          <span style="min-width:140px;color:#94a3b8;">${esc(b.label)}</span>
          <span style="color:#64748b;flex:1;">${esc(b.val)}</span>
          <span style="font-size:11px;color:#475569;">from Settings</span>
        </div>`).join('')
        + custom.map((c,i) => `
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #1e293b;font-size:13px;">
          <span style="min-width:140px;color:#e2e8f0;">${esc(c.label)}</span>
          <span style="color:#64748b;flex:1;">${esc(c.path)}</span>
          <button class="btn btn-ghost btn-sm" style="color:#ef4444;" onclick="removeCustomPath(${i})">✕ Remove</button>
        </div>`).join('');
    }
    if (!d.ok || !d.disks.length) { el.innerHTML = '<div style="color:#64748b;">No volumes found.</div>'; return; }
    el.innerHTML = d.disks.map(disk => {
      if (disk.error) return `
        <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #1e293b;">
          <span style="min-width:140px;font-size:13px;color:#94a3b8;">${esc(disk.label)}</span>
          <span style="font-size:12px;color:#ef4444;">⚠ ${esc(disk.error)}</span>
        </div>`;
      const color = _diskColor(disk.pct);
      const freeGB  = (disk.free  / 1e9).toFixed(1);
      const totalGB = (disk.total / 1e9).toFixed(1);
      return `
        <div style="padding:10px 0;border-bottom:1px solid #1e293b;">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
            <span style="min-width:140px;font-size:13px;color:#e2e8f0;font-weight:500;">${esc(disk.label)}</span>
            <span style="font-size:12px;color:#64748b;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(disk.mount)}</span>
            <span style="font-size:13px;font-weight:600;color:${color};">${disk.pct}%</span>
            <span style="font-size:12px;color:#64748b;">${freeGB} GB free of ${totalGB} GB</span>
          </div>
          <div style="height:8px;background:#1e293b;border-radius:4px;overflow:hidden;">
            <div style="height:100%;width:${disk.pct}%;background:${color};border-radius:4px;transition:width .4s;"></div>
          </div>
        </div>`;
    }).join('');
  } catch(e) { el.innerHTML = '<div style="color:#ef4444;">Error loading disk info.</div>'; }
}

async function saveThresholds() {
  const y = parseInt(document.getElementById('thresh-yellow').value);
  const r = parseInt(document.getElementById('thresh-red').value);
  const st = document.getElementById('thresh-status');
  if (isNaN(y) || isNaN(r) || y <= 0 || r <= 0 || y >= r) {
    st.textContent = '❌ Yellow must be less than Red, both between 1-99';
    st.className = 'status-msg err'; return;
  }
  const cfg = await (await fetch('/epg-web/api/config')).json();
  cfg.disk_warn_yellow = y;
  cfg.disk_warn_red    = r;
  await post('/epg-web/api/config', cfg);
  _diskWarnYellow = y; _diskWarnRed = r;
  st.textContent = '✅ Saved — storage bar will update on next refresh';
  st.className = 'status-msg ok';
  loadStorageBar();
}

async function addCustomPath() {
  const label = document.getElementById('custom-path-label').value.trim();
  const path  = document.getElementById('custom-path-val').value.trim();
  if (!label || !path) return;
  const cfg = await (await fetch('/epg-web/api/config')).json();
  cfg.disk_custom_paths = cfg.disk_custom_paths || [];
  cfg.disk_custom_paths.push({label, path});
  await post('/epg-web/api/config', cfg);
  document.getElementById('custom-path-label').value = '';
  document.getElementById('custom-path-val').value   = '';
  loadStorageTab();
}

async function removeCustomPath(idx) {
  const cfg = await (await fetch('/epg-web/api/config')).json();
  (cfg.disk_custom_paths || []).splice(idx, 1);
  await post('/epg-web/api/config', cfg);
  loadStorageTab();
}

function closeProg() {
  document.getElementById('prog-modal-overlay').style.display = 'none';
  // reset play button for next open (VLC keeps running)
  const btn = document.getElementById('pm-play-btn');
  btn.textContent = '▶ Play'; btn.disabled = false;
  btn.onclick = playStream;
}
async function recordAiring(airing, title, button) {
  const btn = button || event.target;
  btn.disabled = true; btn.textContent = '…';
  const r = await post('/epg-web/api/record', {
    title:      title,
    channel_id: airing.channel_id,
    start_ts:   airing.start_ts,
    stop_ts:    airing.stop_ts,
    episode_title: airing.episode_title || '',
    season_num: airing.season_num,
    episode_num: airing.episode_num,
  });
  if (r.ok) {
    btn.textContent = '✅ Scheduled';
    btn.style.background = '#166534';
    document.getElementById('pm-status').textContent = `✅ "${title}" queued`;
    document.getElementById('pm-status').className = 'status-msg ok';
    startRecPoll();
    refreshGuideRecMap().then(() => renderGuide());
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
    agent_claimed:'🤝', preflight:'🔎', waiting:'⏳',
    awaiting_transfer:'💾', transferring:'📤', copying:'📤',
    done:'✅', done_ts:'✅', skipped_existing_better:'⏭️',
    error:'❌', failed:'❌', cancelled:'🚫'
  };
  document.getElementById('rec-list').innerHTML = recs.map(([id, r]) => {
    const baseStatus = (r.status||'').split('(')[0].trim();
    const icon = statusIcons[baseStatus] || '•';
    const active = ['queued','scheduled','agent_claimed','preflight','waiting',
                    'recording','awaiting_transfer','transferring','copying'].includes(baseStatus);
    return `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #1e1e1e;font-size:13px;">
      <span style="font-size:16px;">${icon}</span>
      <span style="flex:1;color:#c7d2e7;">${esc(r.title)}</span>
      <span style="color:#64748b;font-size:11px;">${r.status}</span>
      ${active ? `<button class="btn btn-danger btn-sm" onclick="cancelRec('${id}')">■</button>` : ''}
    </div>`;
  }).join('');
  // Stop polling when nothing active
  const anyActive = recs.some(([,r]) => {
    const s = (r.status||'').split('(')[0].trim();
    return ['queued','scheduled','agent_claimed','preflight','waiting','recording',
            'converting','awaiting_transfer','transferring','copying'].includes(s);
  });
  if (!anyActive) { clearInterval(_recPoll); _recPoll = null; }
}
async function cancelRec(id, refreshSchedule=false) {
  const result = await post('/epg-web/api/record/cancel', {id});
  await updateRecPanel();
  if (refreshSchedule) await loadSchedule();
  if (!result.ok && result.error) setGS(result.error, 'err');
  return result;
}

// ── Recommendations ───────────────────────────────────────────────────────────
async function loadRecs() {
  document.getElementById('rec-status').textContent = 'Loading…';
  try {
    const d = await (await fetch('/epg-web/api/recommendations')).json();
    if (d.error) { setEl('rec-status',d.error,'err'); return; }
    const recs = d.recommendations || [];
    setEl('rec-status', recs.length + ' wanted titles','');
    const tbody = document.getElementById('rec-body');
    const renderRow = r => {
      const a = r.next_airing;
      const isSeries = r.type === 'series';
      const titleArg = JSON.stringify(r.title).replace(/"/g,'&quot;');
      const airingArg = a ? JSON.stringify(a).replace(/"/g,'&quot;') : '';
      const yearArg = JSON.stringify(r.year || '').replace(/"/g,'&quot;');
      const action = isSeries
        ? (a ? `<button class="btn btn-primary btn-sm" onclick="openWantedSeries(${titleArg},${airingArg})">📺 Episodes</button>` : '')
        : r.in_plex
          ? (r.upgrade_scheduled
              ? `<span class="badge badge-record" title="A better commercial-free airing was found and scheduled automatically">✓ Upgrade scheduled</span>`
              : `<button class="btn btn-ghost btn-sm" onclick="checkWantedMovieUpgrade(this,${titleArg},${yearArg})">Check upgrade</button>`)
          : (a ? `<button class="btn btn-success btn-sm" onclick="recordAiring(${airingArg},${titleArg})">⏱ Record</button>` : '');
      return `<tr>
        <td class="title-cell">${esc(r.title)} ${r.year?'<span style="color:#555;font-size:11px;">('+r.year+')</span>':''}
          ${r.in_plex?`<span class="badge badge-recorded" title="Found in Plex (${esc(r.plex_kind)})" style="margin-left:5px;">▶ IN PLEX</span>`:''}
          <span style="color:#64748b;font-size:10px;margin-left:5px;text-transform:uppercase;">${isSeries ? 'Series · episode tracking' : 'Movie'}</span>
        </td>
        <td class="ch-cell">${a ? esc(a.channel) : '<span style="color:#333">Not in guide</span>'}</td>
        <td class="time-cell">${a ? esc(a.start_fmt) : ''}</td>
        <td class="act-cell">${action}<button class="btn btn-danger btn-sm" onclick='removeWanted(${r.id})'>✕</button></td>
      </tr>`;
    };
    const groups = [
      ['MOVIES', recs.filter(r => r.type !== 'series' && !r.in_plex)],
      ['MOVIES — IN PLEX', recs.filter(r => r.type !== 'series' && r.in_plex)],
      ['SERIES — EPISODE TRACKING', recs.filter(r => r.type === 'series')],
    ];
    tbody.innerHTML = groups.filter(([, rows]) => rows.length).map(([label, rows]) =>
      `<tr><td colspan="4" style="padding:14px 0 5px;color:#94a3b8;font-size:11px;font-weight:700;letter-spacing:.1em;">${label}</td></tr>` +
      rows.map(renderRow).join('')
    ).join('');
  } catch(e) { setEl('rec-status','Failed: '+e.message,'err'); }
}
async function checkWantedMovieUpgrade(btn, title, year) {
  btn.disabled = true; btn.textContent = 'Checking…';
  try {
    const params = new URLSearchParams({title, year: year || ''});
    const d = await (await fetch(`/epg-web/api/recommendations/movie-upgrade?${params}`)).json();
    if (d.better && d.airing) {
      btn.disabled = false; btn.textContent = '⏱ Record upgrade';
      btn.title = d.decision || 'A better commercial-free copy is available';
      btn.onclick = () => recordAiring(d.airing, title, btn);
    } else {
      btn.textContent = d.decision || d.error || 'Plex copy is best';
      btn.title = btn.textContent;
    }
  } catch (e) { btn.textContent = 'Could not compare'; btn.title = e.message; }
}
function openWantedSeries(title, airing) {
  openProg({...airing, title, desc:'', year:'', category:''});
}
async function removeWanted(id) {
  if (!confirm('Remove from wanted list?')) return;
  await post('/epg-web/api/wanted', {action:'remove', id});
  loadRecs();
}
async function addWanted(type) {
  const title = prompt(type === 'series' ? 'Series title:' : 'Movie title:');
  if (!title) return;
  await post('/epg-web/api/wanted', {action:'add', title, type});
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

// ── 24/7 Channels ─────────────────────────────────────────────────────────────
async function load247() {
  const q          = document.getElementById('c247-search').value.trim();
  const showFav    = document.getElementById('c247-show-fav').checked;
  const showTV     = document.getElementById('c247-show-tv').checked;
  const showMovies = document.getElementById('c247-show-movies').checked;
  const showKids   = document.getElementById('c247-show-kids').checked;
  const showSports = document.getElementById('c247-show-sports').checked;
  const showHidden = document.getElementById('c247-show-hidden').checked;
  try {
    const d = await (await fetch(`/epg-web/api/247channels?q=${encodeURIComponent(q)}&show_hidden=${showHidden?'1':'0'}`)).json();
    if (d.error) { setEl('c247-status', d.error, 'err'); return; }
    const visible = d.channels.filter(c => {
      if (c.hidden) return showHidden;
      if (c.fav)    return showFav;
      if (c.subtype === 'movies')  return showMovies;
      if (c.subtype === 'kids')    return showKids;
      if (c.subtype === 'sports')  return showSports;
      return showTV;
    });
    setEl('c247-status', `${visible.length} of ${d.total} channels`, '');
    document.getElementById('c247-grid').innerHTML = visible.map(c => `
      <div style="background:${c.hidden?'#0f172a':'#1e293b'};border-radius:8px;padding:10px 12px;display:flex;align-items:center;gap:8px;${c.hidden?'opacity:0.45;':''}cursor:pointer;"
           onclick='${c.hidden ? '' : `play247(${JSON.stringify(c.id)},${JSON.stringify(c.name)})`}'>
        <span style="flex:1;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
              title="${esc(c.name)}">${esc(c.name)}</span>
        ${c.hidden
          ? `<span onclick='event.stopPropagation();hide247(${JSON.stringify(c.id)},false,this)' title="Restore" style="font-size:13px;cursor:pointer;color:#22c55e;flex-shrink:0;">↩</span>`
          : `<span onclick='event.stopPropagation();toggle247Fav(${JSON.stringify(c.id)},this)' title="Toggle favorite" style="font-size:16px;cursor:pointer;color:${c.fav?'#f59e0b':'#475569'};flex-shrink:0;">${c.fav?'★':'☆'}</span>
             <span onclick='event.stopPropagation();hide247(${JSON.stringify(c.id)},true,this)' title="Hide" style="font-size:13px;cursor:pointer;color:#475569;flex-shrink:0;">✕</span>
             <span style="font-size:18px;flex-shrink:0;" title="Play">▶</span>`
        }
      </div>`).join('');
  } catch(e) { setEl('c247-status', 'Failed', 'err'); }
}
async function play247(channelId, name) {
  const r = await fetch('/epg-web/api/play', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({channel_id: channelId, title: name, ch_name: name})});
  const d = await r.json();
  if (d.error) setEl('c247-status', '❌ ' + d.error, 'err');
  else setEl('c247-status', `▶ Playing: ${name}`, 'ok');
}
async function hide247(channelId, hide, el) {
  const r = await fetch('/epg-web/api/channel/hide', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({channel_id: channelId, hide})});
  const d = await r.json();
  if (d.ok) load247();
}
async function toggle247Fav(channelId, starEl) {
  const r = await fetch('/epg-web/api/channel/favorite', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({channel_id: channelId})});
  const d = await r.json();
  if (d.ok) {
    starEl.textContent = d.favorite ? '★' : '☆';
    starEl.style.color = d.favorite ? '#f59e0b' : '#475569';
  }
}

// ── Schedule ──────────────────────────────────────────────────────────────────
async function addToSchedule(prog) {
  await post('/epg-web/api/schedule', {action:'add', programme:prog});
  const msg = `"${prog.title}" added to schedule.`;
  setGS(msg,'ok');
}
async function loadSchedule() {
  const showScheduled = document.getElementById('sf-scheduled')?.checked ?? true;
  const showCompleted = document.getElementById('sf-completed')?.checked ?? true;
  const showFailed    = document.getElementById('sf-failed')?.checked ?? true;
  const showSkipped   = document.getElementById('sf-skipped')?.checked ?? false;

  const showAllTime = document.getElementById('sf-all-time')?.checked ?? false;
  const cutoffMs   = showAllTime ? 0 : Date.now() - 30 * 24 * 60 * 60 * 1000;

  const [d, recD] = await Promise.all([
    (await fetch('/epg-web/api/schedule')).json(),
    (await fetch('/epg-web/api/record/status')).json(),
  ]);
  // Convert in-memory _recs to schedule row format and prepend
  const memRecs = Object.entries(recD.recordings || {}).map(([id, r]) => ({
    title:      r.title || '',
    channel:    r.channel || r.channel_id || '',
    start_time: r.start_ts ? new Date(r.start_ts * 1000).toLocaleString() : '',
    status:     (r.status||'queued').split('(')[0].trim(),  // strip "(Xm away)" verbose part
    _mem:       true,
    _id:        id,
  }));
  const memIds = new Set(memRecs.map(r => r._id));
  // Filter DB rows: only hide old MISSED/STALE noise; always show completed, failed, and active
  const dbRows = (d.schedule || []).filter(r => !r.rec_id || !memIds.has(r.rec_id)).filter(r => {
    if (!cutoffMs) return true;
    const s = (r.status||'').toLowerCase();
    const alwaysShow = s === 'completed' || s === 'recorded' || s === 'complete' ||
                       s === 'done' || s === 'done_ts' ||
                       s === 'failed' || s === 'timeout' || s === 'error' ||
                       ['queued','scheduled','agent_claimed','preflight','waiting',
                        'recording','converting','awaiting_transfer','transferring',
                        'copying'].includes(s);
    if (alwaysShow) return true;
    // Only apply 30-day cutoff to missed/stale/skipped/cancelled
    const t = r.start_time ? new Date(r.start_time).getTime() : 0;
    return !t || t >= cutoffMs;
  });
  const all = [...memRecs, ...dbRows];
  const tbl   = document.getElementById('sched-table');
  const emp   = document.getElementById('sched-empty');

  const SB = {
    scheduled:'badge-record',  to_record:'badge-record',   queued:'badge-record',
    agent_claimed:'badge-record', preflight:'badge-record', waiting:'badge-record',
    recording:'badge-wl', converting:'badge-wl', awaiting_transfer:'badge-wl',
    transferring:'badge-wl', copying:'badge-wl',
    completed:'badge-recorded', recorded:'badge-recorded', complete:'badge-recorded',
    done:'badge-recorded', done_ts:'badge-recorded',
    failed:'badge-skipped',    timeout:'badge-skipped',
    cancelled:'badge-skipped', skipped:'badge-skipped', skipped_existing_better:'badge-skipped'
  };

  const now = Date.now();
  const sched = all.filter(r => {
    const s = (r.status||'').toLowerCase();
    const startMs = r.start_time ? new Date(r.start_time).getTime() : 0;
    const isPast  = startMs > 0 && startMs < now;
    if (['scheduled','to_record','queued','agent_claimed','preflight','waiting',
         'recording','converting','awaiting_transfer','transferring','copying'].includes(s)) return showScheduled;
    if (['completed','recorded','complete','done','done_ts'].includes(s)) return showCompleted;
    if (s === 'failed'    || s === 'timeout' || s === 'error')     return showFailed;
    if (s === 'skipped' || s === 'cancelled' || s.startsWith('skipped')) return showSkipped;
    return true;  // unknown statuses always show
  });

  if (!sched.length) { tbl.style.display='none'; emp.style.display='block'; return; }
  tbl.style.display='table'; emp.style.display='none';

  document.getElementById('sched-body').innerHTML = sched.map((r,i) => {
    const s = (r.status||'').toLowerCase();
    const isFailed  = s === 'failed' || s === 'timeout' || s === 'error';
    const startMs   = r.start_time ? new Date(r.start_time).getTime() : 0;
    const isPast    = startMs > 0 && startMs < now;
    const isMissed  = (s === 'scheduled' || s === 'to_record' || s === 'queued') && isPast;
    // A live in-memory recording naturally has a start time in the past.
    // Only a database-only recording can be stale after a server restart.
    const isStale   = !r._mem && s === 'recording' && isPast;
    const isSkipped = !isMissed && !isStale && s.startsWith('skipped');
    const badge     = (isMissed || isStale || isSkipped) ? 'badge-skipped' : (SB[s] || SB[r.status] || '');
    const rawLabel  = (r.status||'').replace(/_/g,' ').toUpperCase();
    const label     = isMissed ? 'MISSED' : isStale ? 'STALE' : isSkipped ? rawLabel : rawLabel;
    return `<tr>
      <td class="title-cell">${esc(r.title)}
        ${r.episode_title?`<br><span style="font-size:11px;color:#555;">S${r.season_number||'?'}E${r.episode_number||'?'} ${esc(r.episode_title)}</span>`:''}
      </td>
      <td class="ch-cell">${esc(r.channel)}</td>
      <td class="time-cell">${esc(r.start_time||r.start_fmt||'')}</td>
      <td><span class="badge ${badge}">${esc(label)}</span></td>
      <td style="font-size:11px;color:#64748b;max-width:200px;">${esc(r.failure_reason||'')}
        ${(isFailed || isMissed || isStale || isSkipped) ? `<button class="btn btn-ghost btn-sm" style="margin-left:6px;font-size:11px;" onclick='searchOpenProg(${JSON.stringify(r.title)},${JSON.stringify(r.episode_title||"")},${JSON.stringify(r.season_number||"")},${JSON.stringify(r.episode_number||"")})'>🔄 Re-record</button>` : ''}
        ${(r._mem && r._id && ['queued','scheduled','agent_claimed','preflight','waiting',
          'recording','awaiting_transfer','transferring'].includes(s)) ? `<button class="btn btn-danger btn-sm" style="margin-left:6px;font-size:11px;" onclick="cancelRec('${r._id}',true)">✕ Cancel</button>` : ''}
      </td>
    </tr>`;
  }).join('');
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

// ── Init handled by autoLoad() above ──────────────────────────────────────────
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
            _schedule_active_series(db_path)
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
    _load_pending_recs()
    print(f'\n  EPG Manager Web {VERSION}')
    print(  '  ──────────────────────')
    print(  '  Open: http://localhost:5001/epg-web\n')
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
