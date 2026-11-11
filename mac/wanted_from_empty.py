#!/usr/bin/env python3
"""Put the films from empty Plex movie folders back on the Wanted list.

Run on the Mac mini:
    python3 ~/EPG-Manager/mac/wanted_from_empty.py --dry-run
    python3 ~/EPG-Manager/mac/wanted_from_empty.py

A folder like "Minority Report (2002)" with no video file in it is a film that is not
really in Plex.  This reads the title and year from each such folder, shows ONE recap, and
adds the ones you agree to to the dashboard's Wanted list.  Nothing is removed from Plex:
the empty folders stay where they are.

Commands at the recap prompt:
    y            add everything under "Will be added to Wanted"
    skip 3 7     leave numbers 3 and 7 out
    list         show the recap again
    q            quit without adding anything
Options:
    --dry-run    show the recap and what would happen, add nothing
    --yes        add everything without asking (after printing the recap)

Folders with no (YYYY) in their name are listed but not added: a film needs a year so
the right one is recorded.  Run plex_movie_years.py first to give them one.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_movie_years as years  # noqa: E402

VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.m4v', '.avi', '.mov', '.ts', '.wmv', '.mpg', '.mpeg'}
TITLE_AND_YEAR = re.compile(r'^(.*?)\s*\(((?:19|20)\d{2})\)\s*$')


def has_video(folder):
    """True if a video file is anywhere under ``folder``."""
    for _root, _dirs, files in os.walk(folder):
        for name in files:
            if not name.startswith('.') and os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                return True
    return False


def empty_film_folders(movies_dir):
    """[(folder name, title, year)] for movie folders with no video file in them.

    Hidden folders and folders starting with _ are left out.  A name with no (YYYY) gets
    year ''.
    """
    found = []
    for name in sorted(os.listdir(movies_dir)):
        path = os.path.join(movies_dir, name)
        if name.startswith(('.', '_')) or not os.path.isdir(path) or has_video(path):
            continue
        match = TITLE_AND_YEAR.match(name)
        title, year = (match.group(1).strip(), match.group(2)) if match else (name.strip(), '')
        found.append((name, title, year))
    return found


def get_json(server, path, params=None, timeout=20):
    url = server.rstrip('/') + path + ('?' + urllib.parse.urlencode(params) if params else '')
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def already_wanted(server):
    """{(folded title, year)} of the movies already on the Wanted list, or None if unreadable."""
    data = get_json(server, '/epg-web/api/recommendations')
    if 'recommendations' not in data:
        return None
    return {(years.fold_title(item.get('title')), str(item.get('year') or '')[:4])
            for item in data['recommendations'] if item.get('type') != 'series'}


def canonical(server, title, year):
    """(title, imdb id) from the film database for this title and year, else (title, '').

    The database's spelling (apostrophes, accents) is what the guide uses, so it is the
    better title to want.  It is only used when it is the same film: same year, same name.
    """
    info = get_json(server, '/epg-web/api/prog-info',
                    {'title': title, 'year': year, 'content_type': 'movie'})
    if (str(info.get('year', ''))[:4] == year and info.get('title')
            and years.fold_title(info['title']) == years.fold_title(title)):
        return info['title'], str(info.get('imdb_id') or '')
    return title, ''


def build_plan(folders, wanted, server):
    """One numbered row per folder: add, already wanted, or no year."""
    def resolve(item):
        name, title, year = item
        if not year:
            return {'name': name, 'title': title, 'year': '', 'imdb_id': '', 'state': 'noyear'}
        wanted_title, imdb_id = canonical(server, title, year) if server else (title, '')
        known = wanted is not None and (years.fold_title(wanted_title), year) in wanted
        return {'name': name, 'title': wanted_title, 'year': year, 'imdb_id': imdb_id,
                'state': 'already' if known else 'add'}
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(resolve, folders))
    for number, row in enumerate(rows, 1):
        row['n'], row['skip'] = number, False
    return rows


def will_add(plan):
    return [row for row in plan if row['state'] == 'add' and not row['skip']]


def format_recap(plan):
    add = will_add(plan)
    lines = [f'Will be added to Wanted ({len(add)}):']
    lines += [f"  {row['n']:>3}. {row['title']} ({row['year']})" for row in add] or ['  (none)']
    already = [row for row in plan if row['state'] == 'already']
    if already:
        lines.append(f'\nAlready on the Wanted list ({len(already)}):')
        lines += [f"  {row['n']:>3}. {row['title']} ({row['year']})" for row in already]
    skipped = [row for row in plan if row['skip']]
    if skipped:
        lines.append(f'\nLeft out by you ({len(skipped)}):')
        lines += [f"  {row['n']:>3}. {row['title']} ({row['year']})" for row in skipped]
    noyear = [row for row in plan if row['state'] == 'noyear']
    if noyear:
        lines.append(f'\nNot added, no year in the folder name ({len(noyear)}):')
        lines += [f"  {row['n']:>3}. {row['name']}" for row in noyear]
    return '\n'.join(lines)


def parse_command(text, plan):
    """Returns (action, message); action is 'apply', 'quit' or 'continue'."""
    parts = text.strip().lower().split()
    if not parts:
        return 'continue', 'Type y to add, skip N, list, or q.'
    command, rest = parts[0], parts[1:]
    if command in ('y', 'yes'):
        return 'apply', ''
    if command in ('q', 'quit', 'n', 'no'):
        return 'quit', ''
    if command == 'list':
        return 'continue', format_recap(plan)
    if command == 'skip':
        by_number = {row['n']: row for row in plan}
        numbers = []
        for item in rest:
            if not item.isdigit() or int(item) not in by_number:
                return 'continue', f'"{item}" is not a number from the list.'
            numbers.append(int(item))
        if not numbers:
            return 'continue', 'skip needs numbers, for example: skip 3 7'
        for number in numbers:
            by_number[number]['skip'] = True
        return 'continue', f'Left out: {", ".join(str(n) for n in numbers)}.  Type list to check, y to add.'
    return 'continue', f'Unknown command "{command}". Type y, skip N, list, or q.'


def add_to_wanted(server, row):
    """POST one film to the Wanted list.  Returns '' on success, else the reason."""
    body = json.dumps({'action': 'add', 'title': row['title'], 'year': row['year'],
                       'imdb_id': row['imdb_id'], 'type': 'movie'}).encode()
    request = urllib.request.Request(server.rstrip('/') + '/epg-web/api/wanted', data=body,
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return str(json.load(exc).get('error') or exc)
        except (ValueError, OSError):
            return str(exc)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return str(exc)
    return '' if answer.get('ok') else str(answer.get('error') or 'the dashboard refused it')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--movies', help='Plex Movies folder (default: plex_path from the recorder config)')
    parser.add_argument('--server', help='dashboard address (default: server_url from the recorder config)')
    parser.add_argument('--dry-run', action='store_true', help='show the recap, add nothing')
    parser.add_argument('--yes', action='store_true', help='add everything without asking')
    args = parser.parse_args(argv)
    default_movies, default_server = years.load_defaults(args.config)
    movies_dir = args.movies or default_movies
    server = args.server or default_server
    if not server:
        print('No dashboard address: set server_url in the recorder config or use --server.')
        return 1
    if not movies_dir or not os.path.isdir(movies_dir):
        print(f'Cannot open the Movies folder ({movies_dir or "not set"}). Is the Plex share mounted?')
        return 1
    folders = empty_film_folders(movies_dir)
    if not folders:
        print('No movie folder is without a video file.')
        return 0
    wanted = already_wanted(server)
    if wanted is None:
        print('Could not read the Wanted list from the dashboard, so nothing can be added safely.')
        return 1
    print(f'{len(folders)} movie folder(s) have no video file in {movies_dir}.  Checking each...\n')
    plan = build_plan(folders, wanted, server)
    print(format_recap(plan))
    if not will_add(plan):
        print('\nNothing to add.')
        return 0
    if not args.yes:
        print('\nType one command and press Enter:\n'
              '  y            add everything under "Will be added to Wanted"\n'
              '  skip 3 7     leave numbers 3 and 7 out\n'
              '  list         show the list again\n'
              '  q            quit without adding anything')
        while True:
            try:
                text = input('> ')
            except EOFError:
                text = 'q'
            action, message = parse_command(text, plan)
            if message:
                print(message)
            if action == 'quit':
                print('Nothing was added.')
                return 0
            if action == 'apply':
                break
    added, failed = [], []
    for row in will_add(plan):
        if args.dry_run:
            print(f'  (dry run) would add "{row["title"]} ({row["year"]})"')
            added.append(row)
            continue
        reason = add_to_wanted(server, row)
        if reason:
            failed.append((row, reason))
        else:
            print(f'  Added "{row["title"]} ({row["year"]})"')
            added.append(row)
    print(f'\nDone. {"Would add" if args.dry_run else "Added"} {len(added)}, could not add {len(failed)}.')
    for row, reason in failed:
        print(f'  Not added: {row["title"]} ({row["year"]}): {reason}')
    if added and not args.dry_run:
        print('Open Recommendations in the dashboard: each film shows its next airing when the guide has one.')
        print('The empty folders are still in Plex. Nothing was removed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
