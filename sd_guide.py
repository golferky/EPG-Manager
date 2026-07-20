#!/usr/bin/env python3
"""
Schedules Direct guide fetcher.
Pulls up to 14 days of TV listings and stores them in guide.db.
"""

import hashlib, json, os, sqlite3, time
from datetime import datetime, timezone, timedelta
from urllib import request as urlreq, error as urlerr

SD_BASE  = 'https://json.schedulesdirect.org/20141201'
SD_TOKEN_FILE = os.path.join(os.path.dirname(__file__), 'sd_token.json')

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _req(method, path, body=None, token=None):
    url = SD_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req  = urlreq.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent',   'EPGManagerWeb/1.0')
    if token:
        req.add_header('token', token)
    try:
        with urlreq.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urlerr.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'SD HTTP {e.code}: {body}')

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_token(username, password):
    """Return cached token or fetch a new one."""
    # Cache token for up to 23 hours
    if os.path.exists(SD_TOKEN_FILE):
        with open(SD_TOKEN_FILE) as f:
            cached = json.load(f)
        if cached.get('expires', 0) > time.time():
            return cached['token']

    pw_hash = hashlib.sha1(password.encode()).hexdigest()
    resp    = _req('POST', '/token', {'username': username, 'password': pw_hash})
    if resp.get('code', 0) != 0:
        raise RuntimeError(f'SD token error: {resp}')
    token = resp['token']

    with open(SD_TOKEN_FILE, 'w') as f:
        json.dump({'token': token, 'expires': time.time() + 82800}, f)
    return token

# ── Guide DB helpers ──────────────────────────────────────────────────────────

def ensure_guide_db(db_path):
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
    conn.commit()
    conn.close()

# ── Main fetch ────────────────────────────────────────────────────────────────

def fetch_sd_guide(username, password, db_path, days=14, log=print):
    """
    Authenticate with Schedules Direct, pull <days> days of listings,
    and store them in guide.db via INSERT OR IGNORE.
    Returns dict with counts.
    """
    ensure_guide_db(db_path)

    log('Authenticating with Schedules Direct…')
    token = get_token(username, password)

    # 1. Get user's lineups
    log('Fetching lineups…')
    status   = _req('GET', '/status', token=token)
    lineups  = [l['lineup'] for l in status.get('lineups', [])]
    if not lineups:
        raise RuntimeError('No lineups found on this SD account. Add a lineup at schedulesdirect.org.')
    log(f'Found {len(lineups)} lineup(s): {lineups}')

    # 2. Get stations for each lineup
    station_map = {}   # stationID → {name, callsign, icon}
    channel_map = {}   # stationID → channel number/name for display

    for lineup_id in lineups:
        log(f'Getting stations for {lineup_id}…')
        lu = _req('GET', f'/lineups/{lineup_id}', token=token)
        stations = {s['stationID']: s for s in lu.get('stations', [])}
        # Map is list of {stationID, channel, ...}
        for mapping in lu.get('map', []):
            sid = mapping['stationID']
            if sid in stations:
                st = stations[sid]
                name = st.get('name') or st.get('callsign') or sid
                icon = ''
                if st.get('stationLogo'):
                    icon = st['stationLogo'][0].get('URL', '')
                elif st.get('logo'):
                    icon = st['logo'].get('URL', '')
                station_map[sid] = {
                    'name': name,
                    'icon': icon,
                }
                ch_num = mapping.get('channel', '')
                channel_map[sid] = f'{ch_num} {name}'.strip()

    log(f'Total stations: {len(station_map)}')

    # 3. Upsert channels into guide.db
    conn = sqlite3.connect(db_path)
    for sid, info in station_map.items():
        conn.execute(
            'INSERT OR REPLACE INTO guide_channels(channel_id, channel_name, icon) VALUES (?,?,?)',
            (sid, info['name'], info['icon'])
        )
    conn.commit()

    # 4. Build date list
    today = datetime.now(timezone.utc).date()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(days)]

    # 5. Fetch schedules in batches of 5000 station-days
    station_ids = list(station_map.keys())
    BATCH = 500  # stations per request
    all_program_ids = set()
    schedule_data   = {}  # programID → {stationID, airDateTime, duration}

    for i in range(0, len(station_ids), BATCH):
        batch_sids = station_ids[i:i+BATCH]
        body = [{'stationID': sid, 'date': dates} for sid in batch_sids]
        log(f'Fetching schedules batch {i//BATCH + 1}…')
        results = _req('POST', '/schedules', body, token=token)

        for result in results:
            if result.get('code', 0) != 0:
                continue
            sid = result['stationID']
            for prog in result.get('programs', []):
                pid   = prog['programID']
                start = prog['airDateTime']     # ISO 8601 UTC: "2026-07-19T14:00:00Z"
                dur   = prog.get('duration', 3600)  # seconds
                all_program_ids.add(pid)
                if pid not in schedule_data:
                    schedule_data[pid] = []
                schedule_data[pid].append({
                    'stationID': sid,
                    'start':     start,
                    'duration':  dur,
                })

    log(f'Unique programs to fetch: {len(all_program_ids)}')

    # 6. Fetch program details in batches of 5000
    prog_details = {}
    pid_list = list(all_program_ids)
    PROG_BATCH = 500
    total_prog_batches = (len(pid_list) + PROG_BATCH - 1) // PROG_BATCH
    for i in range(0, len(pid_list), PROG_BATCH):
        batch = pid_list[i:i+PROG_BATCH]
        log(f'Fetching program details {i//PROG_BATCH + 1}/{total_prog_batches}…')
        results = _req('POST', '/programs', batch, token=token)
        for p in results:
            pid = p.get('programID', '')
            titles = p.get('titles', [])
            title  = titles[0]['title120'] if titles else pid
            genres = p.get('genres', [])
            cat    = genres[0] if genres else ''
            descs  = p.get('descriptions', {})
            desc   = ''
            for key in ('description1000', 'description100'):
                for lang_block in descs.get(key, []):
                    if lang_block.get('descriptionLanguage', 'en') == 'en':
                        desc = lang_block.get('description', '')[:300]
                        break
                if desc:
                    break
            prog_details[pid] = {'title': title, 'category': cat, 'desc': desc}

    # 7. Insert into guide.db
    log('Writing to guide.db…')
    inserted = 0
    for pid, airings in schedule_data.items():
        detail = prog_details.get(pid, {'title': pid, 'category': '', 'desc': ''})
        for airing in airings:
            sid = airing['stationID']
            ch_name = station_map.get(sid, {}).get('name', sid)
            # Parse start ISO → yyyyMMddHHmmss UTC
            try:
                dt = datetime.fromisoformat(airing['start'].replace('Z', '+00:00'))
                start_utc = dt.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')
                end_dt    = dt + timedelta(seconds=airing['duration'])
                end_utc   = end_dt.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')
            except Exception:
                continue
            cur = conn.execute('''
                INSERT OR IGNORE INTO guide(title, channel_id, channel_name, start_utc, end_utc, desc, category)
                VALUES (?,?,?,?,?,?,?)
            ''', (
                detail['title'], sid, ch_name,
                start_utc, end_utc,
                detail['desc'], detail['category']
            ))
            inserted += cur.rowcount

    conn.commit()
    conn.close()
    log(f'Done — {inserted} new rows inserted.')

    return {
        'stations':  len(station_map),
        'programs':  len(all_program_ids),
        'inserted':  inserted,
    }


if __name__ == '__main__':
    import sys
    cfg_path = os.path.join(os.path.dirname(__file__), 'config.json.bak_20260619_054214_before_localize')
    with open(cfg_path) as f:
        cfg = json.load(f)
    db = os.path.join(os.path.dirname(__file__), 'guide.db')
    result = fetch_sd_guide(cfg['SD_USER'], cfg['SD_PASS'], db, days=14)
    print(result)
