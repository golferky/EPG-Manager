#!/usr/bin/env python3
"""Work out which episode each recording in "_Needs Episode Info" is, and file it in Plex.

Run on the Mac mini:
    python3 ~/EPG-Manager/mac/name_episodes.py --show "I Dream of Jeannie"            show the matches, change nothing
    python3 ~/EPG-Manager/mac/name_episodes.py --show "I Dream of Jeannie" --apply    move the confident ones

The recorder files an airing there when the guide gave no season or episode number.  The guide did keep
a one-line plot summary for each airing, and TVMaze (free, no key) lists every episode of the show with
its summary.  For each file this takes the air time from its name, finds the guide's plot line for that
airing (asked of the dashboard on the QNAP, so guide.db never leaves it), and matches it to the episode
whose summary reads most like it.

What it will NOT move:
  - a match that is not clearly better than the next best (listed as "unsure" so you can look)
  - a file much smaller than the others (a partial episode; partials never go into Plex)
  - two files that match the same episode (listed, so you can pick one)
  - a file whose name has no air time
  - anything whose destination already exists (it never overwrites)
Without --apply it only reads.  Nothing is deleted.
"""
import argparse
import difflib
import html
import json
import os
import re
import shutil
import statistics
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NAME_TIME = re.compile(r' - (\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})\.\w+$')
STOP_WORDS = set('a an and the of to in on at for with his her their he she it is are was be by as that this '
                 'from but or who when while after before into out up down over'.split())
CONFIDENT_SCORE = 0.55      # how alike the plot lines must read
CONFIDENT_MARGIN = 0.08     # how much better than the second-best episode
PARTIAL_RATIO = 0.85        # smaller than this share of the typical file size = a partial


def words(text):
    found = re.findall(r"[a-z0-9']+", str(text or '').lower())
    return [w.strip("'") for w in found if w.strip("'") not in STOP_WORDS and len(w.strip("'")) > 1]


def clean_summary(text):
    return html.unescape(re.sub(r'<[^>]+>', ' ', str(text or ''))).strip()


def similarity(guide_text, summary):
    """0 to 1: how much a short guide plot line reads like an episode summary."""
    guide_words, summary_words = set(words(guide_text)), set(words(summary))
    if not guide_words or not summary_words:
        return 0.0
    overlap = len(guide_words & summary_words) / len(guide_words)
    guide_clean = ' '.join(words(guide_text))
    head = ' '.join(words(summary))[:len(guide_clean) + 40]
    ratio = difflib.SequenceMatcher(None, guide_clean, head).ratio()
    return max(overlap * 0.9, ratio)


def rank_episodes(guide_text, episodes):
    """[(score, episode)] best first."""
    scored = [(similarity(guide_text, clean_summary(ep.get('summary'))), ep) for ep in episodes]
    return sorted(scored, key=lambda pair: pair[0], reverse=True)


def pick(guide_text, episodes):
    """(episode, score, confident) for a plot line."""
    ranked = rank_episodes(guide_text, episodes)
    if not ranked or ranked[0][0] <= 0:
        return None, 0.0, False
    best_score, best = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    return best, best_score, (best_score >= CONFIDENT_SCORE and best_score - second >= CONFIDENT_MARGIN)


def file_start_ts(filename):
    match = NAME_TIME.search(filename)
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups())
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def nearest_airing(airings, start_ts, window=600):
    close = [a for a in airings if abs(a['start_ts'] - start_ts) <= window]
    return min(close, key=lambda a: abs(a['start_ts'] - start_ts)) if close else None


def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'epg-manager-name-episodes'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode('utf-8'))


def load_tvmaze(show):
    data = fetch_json('https://api.tvmaze.com/singlesearch/shows?embed=episodes&q=' + urllib.parse.quote(show))
    episodes = [ep for ep in data.get('_embedded', {}).get('episodes', []) if ep.get('season') and ep.get('number')]
    return str(data.get('premiered') or '')[:4], episodes


def load_guide(server, show):
    data = fetch_json(server.rstrip('/') + '/epg-web/api/guide-descriptions?title=' + urllib.parse.quote(show))
    return data.get('airings', [])


def plan(files, airings, episodes):
    """Decide each file.  files: [(name, size)].  Returns a list of dicts."""
    sizes = [size for _name, size in files]
    typical = statistics.median(sizes) if sizes else 0
    rows = []
    for name, size in files:
        row = {'name': name, 'size': size, 'status': '', 'episode': None, 'score': 0.0, 'plot': ''}
        start = file_start_ts(name)
        if start is None:
            row['status'] = 'no air time in the name: check by hand'
        elif typical and size < typical * PARTIAL_RATIO:
            row['status'] = 'partial episode (much smaller than the rest): not for Plex'
        else:
            airing = nearest_airing(airings, start)
            if not airing:
                row['status'] = 'no guide plot line for that air time'
            else:
                row['plot'] = airing['desc']
                episode, score, confident = pick(airing['desc'], episodes)
                row['episode'], row['score'] = episode, score
                row['status'] = 'ok' if confident else 'unsure'
        rows.append(row)
    taken = {}
    for row in rows:
        if row['status'] == 'ok':
            taken.setdefault((row['episode']['season'], row['episode']['number']), []).append(row)
    for group in taken.values():
        if len(group) > 1:
            for row in group:
                row['status'] = 'duplicate: another file matches the same episode'
    return rows


def target_name(show, episode):
    from recording_agent import safe_filename
    title = safe_filename(episode.get('name') or '')
    suffix = f' - {title}' if title else ''
    return f"{safe_filename(show)} - S{int(episode['season']):02d}E{int(episode['number']):02d}{suffix}.mp4"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--show', required=True, help='the show, as named in _Needs Episode Info')
    parser.add_argument('--server', default='http://192.168.1.176:5001', help='the dashboard on the QNAP')
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--apply', action='store_true', help='move the confident matches into Plex')
    args = parser.parse_args()

    from recording_agent import load_config, resolve_show_dir
    cfg = load_config(os.path.expanduser(args.config))
    tv_root = cfg.get('plex_tv_path', '/Volumes/Plex/TV Shows')
    holding = os.path.join(tv_root, '_Needs Episode Info')
    folders = [d for d in os.listdir(holding) if os.path.isdir(os.path.join(holding, d))
               and re.sub(r'\s*\(\d{4}\)$', '', d).lower() == args.show.lower()] if os.path.isdir(holding) else []
    if not folders:
        print(f'No "{args.show}" folder in {holding}')
        return 1
    folder = os.path.join(holding, folders[0])
    files = [(n, os.path.getsize(os.path.join(folder, n))) for n in sorted(os.listdir(folder))
             if n.lower().endswith(('.mp4', '.mkv', '.m4v')) and not n.startswith('.')]
    print(f'{len(files)} file(s) in {folder}')

    year, episodes = load_tvmaze(args.show)
    airings = load_guide(args.server, args.show)
    print(f'{len(episodes)} episodes known to TVMaze, {len(airings)} guide plot lines.\n')
    rows = plan(files, airings, episodes)

    for row in rows:
        ep = row['episode']
        label = f"S{ep['season']:02d}E{ep['number']:02d} {ep.get('name', '')}" if ep else ''
        print(f"{row['status'].upper()[:9]:9}  {row['name']}  {label}  (match {row['score']:.2f})")
        if row['plot'] and row['status'] in ('ok', 'unsure'):
            print(f"           guide: {row['plot'][:100]}")
            if ep:
                print(f"           tvmaze: {clean_summary(ep.get('summary'))[:100]}")
    ready = [r for r in rows if r['status'] == 'ok']
    print(f'\n{len(ready)} confident, {sum(r["status"] == "unsure" for r in rows)} unsure, '
          f'{sum(r["status"].startswith(("partial", "duplicate", "no ")) for r in rows)} left alone.')

    if not args.apply:
        print('\nNothing was moved.  Run again with --apply to move the confident ones.')
        return 0
    show_dir = resolve_show_dir(tv_root, args.show, year)
    moved = 0
    for row in ready:
        ep = row['episode']
        dest = os.path.join(show_dir, f"Season {int(ep['season']):02d}", target_name(args.show, ep))
        if os.path.exists(dest):
            print(f'SKIPPED (already exists): {dest}')
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(os.path.join(folder, row['name']), dest)
        moved += 1
        print(f'moved -> {dest}')
    print(f'\nMoved {moved} file(s).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
