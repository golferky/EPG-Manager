#!/usr/bin/env python3
"""Work out which episode each recording in "the holding folder" is, and file it in Plex.

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
import html
import json
import math
import os
import re
import shutil
import statistics
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from episode_match import (CONFIDENT_MARGIN, CONFIDENT_SCORE, STOP_WORDS, clean_summary, episode_texts,  # noqa: E402,F401
                           merge_episodes, pick, rank_episodes, similarity, title_key, top_candidates,
                           word_weights, words)

NAME_TIME = re.compile(r' - (\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})\.\w+$')
PARTIAL_RATIO = 0.85        # smaller than this share of the typical file size = a partial


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


def load_tmdb(server, show):
    """Episodes from TMDb, fetched by the dashboard (it holds the key)."""
    data = fetch_json(server.rstrip('/') + '/epg-web/api/tmdb-episodes?title=' + urllib.parse.quote(show))
    episodes = [ep for ep in data.get('episodes', []) if ep.get('season') and ep.get('number')]
    return str(data.get('premiered') or '')[:4], episodes


def load_omdb(server, show, poll_seconds=5, max_minutes=30):
    """Episodes with OMDb plots, fetched by the dashboard in the background (it holds the key)."""
    url = server.rstrip('/') + '/epg-web/api/omdb-episodes?title=' + urllib.parse.quote(show)
    deadline = time.time() + max_minutes * 60
    last = None
    while True:
        data = fetch_json(url)
        if data.get('error') and 'episodes' not in data:
            raise RuntimeError(data['error'])
        state = data.get('status')
        if state != 'running' or time.time() > deadline:
            break
        progress = (data.get('done'), data.get('total'))
        if progress != last:
            print(f'  OMDb: fetching plots {progress[0]}/{progress[1]} ...')
            last = progress
        time.sleep(poll_seconds)
    if state == 'limit':
        print('  OMDb: its daily request limit was reached; using the plots fetched so far (run again tomorrow for the rest).')
    elif state == 'error':
        print(f"  OMDb: could not finish ({data.get('error') or 'error'}); using what was fetched.")
    episodes = [ep for ep in data.get('episodes', []) if ep.get('season') and ep.get('number')]
    return str(data.get('premiered') or '')[:4], episodes


def load_guide(server, show):
    data = fetch_json(server.rstrip('/') + '/epg-web/api/guide-descriptions?title=' + urllib.parse.quote(show))
    return data.get('airings', [])


def mark_duplicates(rows):
    """Two files that are both going to the same episode are held back, so one can be chosen by hand."""
    taken = {}
    for row in rows:
        if row['status'] == 'ok':
            taken.setdefault((row['episode']['season'], row['episode']['number']), []).append(row)
    for group in taken.values():
        if len(group) > 1:
            for row in group:
                row['status'] = 'duplicate: another file matches the same episode'


def parse_accept(text):
    """"2026-08-14_1130=S02E15,2026-08-15_2100=S02E31" -> {"2026-08-14_1130": (2, 15), ...}"""
    accepted = {}
    for part in str(text or '').split(','):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}_\d{4})=S(\d{1,2})E(\d{1,3})', part, flags=re.I)
        if not match:
            raise ValueError(f'Cannot read "{part}": use 2026-08-14_1130=S02E15')
        accepted[match.group(1)] = (int(match.group(2)), int(match.group(3)))
    return accepted


def apply_accepted(rows, accepted, episodes):
    """Mark files the owner has checked by eye as matched to the episode named.  Returns messages."""
    messages = []
    by_number = {(ep['season'], ep['number']): ep for ep in episodes}
    for stamp, key in accepted.items():
        row = next((r for r in rows if f' - {stamp}.' in r['name']), None)
        if row is None:
            messages.append(f'No file for {stamp}')
        elif row['status'].startswith('partial'):
            messages.append(f'{row["name"]}: a partial episode is never accepted')
        elif key not in by_number:
            messages.append(f'{row["name"]}: S{key[0]:02d}E{key[1]:02d} is not an episode of this show')
        else:
            row['episode'], row['status'], row['score'] = by_number[key], 'ok', 1.0
    mark_duplicates(rows)
    return messages


def plan(files, airings, episodes, min_score=None, margin=None):
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
            elif airing.get('episode'):
                known = airing['episode']                       # the dashboard found this exact description on a numbered airing
                row['plot'] = airing['desc']
                row['episode'] = {'season': known['season'], 'number': known['number'], 'name': known.get('name', ''), 'summary': ''}
                row['score'], row['status'] = 1.0, 'ok'
            else:
                row['plot'] = airing['desc']
                episode, score, confident = pick(airing['desc'], episodes, min_score, margin)
                row['episode'], row['score'] = episode, score
                row['status'] = 'ok' if confident else 'unsure'
        rows.append(row)
    mark_duplicates(rows)
    return rows


def agree(rows_a, rows_b):
    """Keep a match only where both sources are confident and name the same episode."""
    by_name = {row['name']: row for row in rows_b}
    for row in rows_a:
        other = by_name.get(row['name'])
        if row['status'] != 'ok':
            continue
        same = (other and other['status'] == 'ok' and other['episode']
                and (row['episode']['season'], row['episode']['number']) ==
                    (other['episode']['season'], other['episode']['number']))
        if not same:
            row['status'] = 'unsure'
    return rows_a


def evaluate(truth, airings, episode_sets):
    """How often the right episode is first (and in the first three) for each wording source.

    truth: {"2026-08-14_1130": (2, 15)}; episode_sets: {"tmdb": [...], "omdb": [...], "tmdb+omdb": [...]}.
    The truth list was chosen by eye partly because TMDb already matched, so it flatters TMDb; the fairer
    test is how many of the files still unresolved get a confident match.
    """
    report = {}
    for mode, episodes in episode_sets.items():
        top1 = top3 = counted = 0
        misses = []
        for stamp, key in truth.items():
            airing = nearest_airing(airings, file_start_ts(f'x - {stamp}.mp4'))
            if not airing:
                continue
            counted += 1
            ranked = rank_episodes(airing['desc'], episodes)
            order = [(int(ep['season']), int(ep['number'])) for score, ep in ranked[:3] if score > 0]     # a zero score is no answer
            top1 += bool(order) and order[0] == key
            top3 += key in order
            if not order or order[0] != key:
                misses.append(stamp)
        report[mode] = {'counted': counted, 'top1': top1, 'top3': top3, 'misses': misses}
    return report


def target_name(show, episode):
    from recording_agent import safe_filename
    title = safe_filename(episode.get('name') or '')
    suffix = f' - {title}' if title else ''
    return f"{safe_filename(show)} - S{int(episode['season']):02d}E{int(episode['number']):02d}{suffix}.mp4"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--show', required=True, help='the show, as named in the holding folder')
    parser.add_argument('--server', default='http://192.168.1.176:5001', help='the dashboard on the QNAP')
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--holding', default='', help='the holding folder (default: TV Needing Episode Info beside TV Shows)')
    parser.add_argument('--source', choices=('tmdb', 'tvmaze', 'both', 'omdb', 'tmdb+omdb'), default='tmdb',
                        help='where the episode summaries come from; tmdb+omdb scores each episode by its best wording; both = only moves files the two agree on')
    parser.add_argument('--evaluate', default='', metavar='TRUTH',
                        help='measure each source against episodes you know, e.g. 2026-08-14_1130=S02E15,...  (changes nothing)')
    parser.add_argument('--min-score', type=float, default=CONFIDENT_SCORE, help='how well a plot line must match')
    parser.add_argument('--margin', type=float, default=CONFIDENT_MARGIN, help='how far ahead of the runner-up')
    parser.add_argument('--top', type=int, default=0, metavar='N',
                        help='for each unsure file, also list its N closest episodes so you can pick one by eye')
    parser.add_argument('--accept', default='', help='matches you checked by eye and want moved even though the tool was '
                        'unsure, e.g. 2026-08-14_1130=S02E15,2026-08-15_2100=S02E31')
    parser.add_argument('--apply', action='store_true', help='move the confident matches into Plex')
    args = parser.parse_args()

    if args.evaluate:
        airings = load_guide(args.server, args.show)
        _year, tmdb_eps = load_tmdb(args.server, args.show)
        _year, omdb_eps = load_omdb(args.server, args.show)
        truth = parse_accept(args.evaluate)
        report = evaluate(truth, airings, {'tmdb': tmdb_eps, 'omdb': omdb_eps, 'tmdb+omdb': merge_episodes(tmdb_eps, omdb_eps)})
        print(f'\nKnown episodes checked: {len(truth)}')
        for mode, r in report.items():
            print(f"  {mode:10} right first: {r['top1']}/{r['counted']}   right in the top 3: {r['top3']}/{r['counted']}")
        print('\nThis only measures episodes you already confirmed, which were partly chosen because TMDb matched them.')
        print('The real test is how many of the unresolved files now get a confident match: run the normal command with --source tmdb+omdb.')
        return 0
    from recording_agent import load_config, resolve_show_dir
    cfg = load_config(os.path.expanduser(args.config))
    tv_root = cfg.get('plex_tv_path', '/Volumes/Plex/TV Shows')
    holding = args.holding or str(cfg.get('needs_episode_path', '') or '')
    if not holding:
        holding = os.path.join(os.path.dirname(tv_root.rstrip('/')), 'TV Needing Episode Info')
        if not os.path.isdir(holding):                       # the older place, inside the TV library
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

    airings = load_guide(args.server, args.show)
    year = ''
    sets = []
    if args.source in ('tmdb', 'both'):
        year, episodes = load_tmdb(args.server, args.show)
        sets.append(('TMDb', episodes))
    if args.source == 'omdb':
        year, episodes = load_omdb(args.server, args.show)
        sets.append(('OMDb', episodes))
    if args.source == 'tmdb+omdb':
        year, tmdb_eps = load_tmdb(args.server, args.show)
        _y, omdb_eps = load_omdb(args.server, args.show)
        sets.append(('TMDb+OMDb', merge_episodes(tmdb_eps, omdb_eps)))
    if args.source in ('tvmaze', 'both'):
        tv_year, episodes = load_tvmaze(args.show)
        year = year or tv_year
        sets.append(('TVMaze', episodes))
    print(f'{len(airings)} guide plot lines; ' + ', '.join(f'{len(e)} episodes from {n}' for n, e in sets) + '.\n')
    rows = plan(files, airings, sets[0][1], args.min_score, args.margin)
    if len(sets) == 2:
        rows = agree(rows, plan(files, airings, sets[1][1], args.min_score, args.margin))

    accepted = parse_accept(args.accept)
    if accepted:
        for message in apply_accepted(rows, accepted, sets[0][1]):
            print('NOTE:', message)
    for row in rows:
        ep = row['episode']
        label = f"S{ep['season']:02d}E{ep['number']:02d} {ep.get('name', '')}" if ep else ''
        print(f"{row['status'].upper()[:9]:9}  {row['name']}  {label}  (match {row['score']:.2f})")
        if row['plot'] and row['status'] in ('ok', 'unsure'):
            print(f"           guide: {row['plot'][:100]}")
            if ep:
                print(f"           episode: {clean_summary(ep.get('summary'))[:100]}")
            if args.top and row['status'] == 'unsure':
                for score, cand in top_candidates(row['plot'], sets[0][1], args.top):
                    print(f"             - S{int(cand['season']):02d}E{int(cand['number']):02d} {cand.get('name', '')} ({score:.2f}): "
                          f"{clean_summary(cand.get('summary'))[:90]}")
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
