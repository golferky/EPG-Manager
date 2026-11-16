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

NAME_TIME = re.compile(r' - (\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})\.\w+$')
STOP_WORDS = set('a an and the of to in on at for with his her their he she it is are was be by as that this '
                 'from but or who when while after before into out up down over'.split())
CONFIDENT_SCORE = 0.5       # share of the plot line's telling words found in the episode summary
CONFIDENT_MARGIN = 0.15     # how much better than the second-best episode
PARTIAL_RATIO = 0.85        # smaller than this share of the typical file size = a partial


def words(text):
    found = re.findall(r"[a-z0-9']+", str(text or '').lower())
    return [w.strip("'") for w in found if w.strip("'") not in STOP_WORDS and len(w.strip("'")) > 1]


def clean_summary(text):
    return html.unescape(re.sub(r'<[^>]+>', ' ', str(text or ''))).strip()


_IDF_CACHE = {}


def word_weights(episodes):
    """How telling each word is: one that is in every summary ("Tony", "Jeannie") counts for little."""
    key = id(episodes)
    if key in _IDF_CACHE and _IDF_CACHE[key][0] is episodes:
        return _IDF_CACHE[key][1]
    docs = [set(words(clean_summary(ep.get('summary')))) for ep in episodes]
    total = len(docs) or 1
    frequency = {}
    for doc in docs:
        for word in doc:
            frequency[word] = frequency.get(word, 0) + 1
    weights = {word: math.log((total + 1) / (count + 1)) + 0.1 for word, count in frequency.items()}
    _IDF_CACHE[key] = (episodes, weights)
    return weights


def similarity(guide_text, summary, weights=None):
    """0 to 1: how much of a short guide plot line is found in an episode summary, rare words counting most."""
    guide_words, summary_words = set(words(guide_text)), set(words(summary))
    if not guide_words or not summary_words:
        return 0.0
    weights = weights or {}
    unseen = max(weights.values()) if weights else 1.0       # a word no summary has can only be unmatched
    def weight(word):
        return weights.get(word, unseen)
    return sum(weight(w) for w in guide_words & summary_words) / sum(weight(w) for w in guide_words)


def rank_episodes(guide_text, episodes):
    """[(score, episode)] best first."""
    weights = word_weights(episodes)
    scored = [(similarity(guide_text, clean_summary(ep.get('summary')), weights), ep) for ep in episodes]
    return sorted(scored, key=lambda pair: pair[0], reverse=True)


def pick(guide_text, episodes, min_score=None, margin=None):
    """(episode, score, confident) for a plot line."""
    min_score = CONFIDENT_SCORE if min_score is None else min_score
    margin = CONFIDENT_MARGIN if margin is None else margin
    ranked = rank_episodes(guide_text, episodes)
    if not ranked or ranked[0][0] <= 0:
        return None, 0.0, False
    best_score, best = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    return best, best_score, (best_score >= min_score and best_score - second >= margin)


def top_candidates(plot, episodes, count=3):
    """The closest few episodes to a plot line: [(score, episode)], best first."""
    return rank_episodes(plot, episodes)[:max(0, count)]


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
    parser.add_argument('--source', choices=('tmdb', 'tvmaze', 'both'), default='tmdb',
                        help='where the episode summaries come from; both = only moves files the two agree on')
    parser.add_argument('--min-score', type=float, default=CONFIDENT_SCORE, help='how well a plot line must match')
    parser.add_argument('--margin', type=float, default=CONFIDENT_MARGIN, help='how far ahead of the runner-up')
    parser.add_argument('--top', type=int, default=0, metavar='N',
                        help='for each unsure file, also list its N closest episodes so you can pick one by eye')
    parser.add_argument('--accept', default='', help='matches you checked by eye and want moved even though the tool was '
                        'unsure, e.g. 2026-08-14_1130=S02E15,2026-08-15_2100=S02E31')
    parser.add_argument('--apply', action='store_true', help='move the confident matches into Plex')
    args = parser.parse_args()

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
