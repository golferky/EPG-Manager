#!/usr/bin/env python3
"""Scan the Plex Movies library for damaged video.

Run on the Mac mini (best overnight; it reads every film over the network):
    python3 ~/EPG-Manager/mac/check_library.py              decode every film in full
    python3 ~/EPG-Manager/mac/check_library.py --quick      only the last 10 minutes of each (much faster)
    python3 ~/EPG-Manager/mac/check_library.py --report     show the results saved so far, scan nothing
    python3 ~/EPG-Manager/mac/check_library.py --only Heat  only folders whose name contains "Heat"

A recording can be the right length and still be damaged where the stream stalled: green or blocky
video that only shows when you play it.  For each movie folder this decodes the film (its largest
video file) and counts FFmpeg's error lines.  50 or more is "damaged"; 1 to 49 is minor.

Results are saved after every film in library_check_cache.json next to the recorder config, so you can
stop with Ctrl+C and run it again: films that have not changed since they were checked are skipped.
This only reads.  It never moves, changes or deletes anything.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_movie_years as base  # noqa: E402
import review_incomplete as review  # noqa: E402

DAMAGED_ERROR_LINES = 50
QUICK_SECONDS = 600


def movie_files(library, only=''):
    """[(folder name, path of its main video)] for every movie folder that holds a video."""
    found = []
    for name in sorted(os.listdir(library), key=str.lower):
        path = os.path.join(library, name)
        if name.startswith(('.', '_', '@')) or not os.path.isdir(path):
            continue
        if only and only.lower() not in name.lower():
            continue
        video = review.main_video(path)
        if video:
            found.append((name, video))
    return found


def load_cache(path):
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path, cache):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(cache, handle)
    os.replace(tmp, path)


def fingerprint(video):
    stat = os.stat(video)
    return {'size': stat.st_size, 'mtime': int(stat.st_mtime)}


def cached_result(cache, video, quick):
    """The saved result for an unchanged file, or None.  A full scan result also serves a quick request."""
    entry = cache.get(video)
    if not entry or entry.get('errors') is None:
        return None
    if {'size': entry.get('size'), 'mtime': entry.get('mtime')} != fingerprint(video):
        return None
    if quick and entry.get('mode') in ('quick', 'full'):
        return entry
    return entry if entry.get('mode') == 'full' else None


def check_one(name, video, ffmpeg, quick):
    errors = review.count_decode_errors(video, ffmpeg, QUICK_SECONDS if quick else None)
    return {'name': name, 'errors': errors, 'mode': 'quick' if quick else 'full',
            'checked_at': int(time.time()), **fingerprint(video)}


def classify(errors, limit=DAMAGED_ERROR_LINES):
    if errors is None:
        return 'unreadable'
    if errors >= limit:
        return 'damaged'
    return 'minor' if errors else 'clean'


def format_report(entries, limit=DAMAGED_ERROR_LINES):
    """entries: [{'name', 'errors', 'mode'}].  A readable summary, worst first."""
    groups = {'damaged': [], 'minor': [], 'unreadable': [], 'clean': []}
    for entry in entries:
        groups[classify(entry['errors'], limit)].append(entry)
    lines = [f"Checked {len(entries)} film(s): {len(groups['clean'])} clean, {len(groups['minor'])} with minor errors, "
             f"{len(groups['damaged'])} damaged, {len(groups['unreadable'])} could not be read.", '']
    if groups['damaged']:
        lines.append(f'DAMAGED ({limit} or more decode errors), worst first:')
        lines += [f"  {e['name']}: {e['errors']} errors" + ('  (last 10 minutes only)' if e.get('mode') == 'quick' else '')
                  for e in sorted(groups['damaged'], key=lambda e: -e['errors'])]
        lines.append('')
    if groups['minor']:
        lines.append('Minor errors (probably a glitch or two):')
        lines += [f"  {e['name']}: {e['errors']} errors" for e in sorted(groups['minor'], key=lambda e: -e['errors'])]
        lines.append('')
    if groups['unreadable']:
        lines.append('Could not be read:')
        lines += [f"  {e['name']}" for e in groups['unreadable']]
        lines.append('')
    return '\n'.join(lines).rstrip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--movies', help='Plex Movies folder (default: plex_path from the recorder config)')
    parser.add_argument('--quick', action='store_true', help='decode only the last 10 minutes of each film')
    parser.add_argument('--report', action='store_true', help='show saved results and scan nothing')
    parser.add_argument('--only', default='', help='only folders whose name contains this text')
    parser.add_argument('--workers', type=int, default=2, help='films checked at once (default 2)')
    parser.add_argument('--limit', type=int, default=DAMAGED_ERROR_LINES, help='errors that count as damaged (default 50)')
    args = parser.parse_args(argv)
    default_movies, _server = base.load_defaults(args.config)
    library = args.movies or default_movies
    if not library or not os.path.isdir(library):
        print(f'Cannot open the Movies folder ({library or "not set"}). Is the Plex share mounted?')
        return 1
    ffmpeg = base.load_config_value(args.config, 'ffmpeg') or 'ffmpeg'
    cache_path = os.path.join(os.path.dirname(os.path.expanduser(args.config)) or '.', 'library_check_cache.json')
    cache = load_cache(cache_path)
    films = movie_files(library, args.only)
    if args.report:
        entries = [{'name': n, 'errors': cache[v].get('errors'), 'mode': cache[v].get('mode')}
                   for n, v in films if v in cache]
        print(format_report(entries, args.limit) if entries else 'Nothing has been scanned yet.')
        return 0
    todo, done = [], []
    for name, video in films:
        saved = cached_result(cache, video, args.quick)
        (done if saved else todo).append((name, video, saved))
    print(f'{len(films)} film(s) in {library}: {len(done)} already checked and unchanged, {len(todo)} to check '
          f'({"last 10 minutes of each" if args.quick else "full decode"}, {args.workers} at a time).')
    if todo:
        print('Stop any time with Ctrl+C; run it again to carry on.\n')
    results = {name: {'name': name, 'errors': saved['errors'], 'mode': saved.get('mode')} for name, _v, saved in done}
    started = time.time()
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [(name, video, pool.submit(check_one, name, video, ffmpeg, args.quick)) for name, video, _s in todo]
            for index, (name, video, future) in enumerate(futures, 1):
                entry = future.result()
                cache[video] = entry
                save_cache(cache_path, cache)
                results[name] = {'name': name, 'errors': entry['errors'], 'mode': entry['mode']}
                shown = 'could not be read' if entry['errors'] is None else f"{entry['errors']} errors"
                print(f'[{index}/{len(futures)}] {name}: {shown}', flush=True)
    except KeyboardInterrupt:
        print('\nStopped.  The results so far are saved; run it again to carry on.')
    print(f'\nTook {(time.time() - started) / 60:.0f} minute(s).\n')
    print(format_report(list(results.values()), args.limit))
    print('\nNothing was moved or deleted.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
