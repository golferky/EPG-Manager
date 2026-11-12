#!/usr/bin/env python3
"""Find Plex movie folders named with the wrong year, and correct them.

Run on the Mac mini:
    python3 ~/EPG-Manager/mac/check_years.py --dry-run
    python3 ~/EPG-Manager/mac/check_years.py

A folder like "Spider-Man 3 (2026)" has a year that is not the film's year (it was probably
the year it aired).  This checks each folder's title against the film database: if no film
with that title came out within a year of the folder's year, it looks for the right one.

  - A year one off is fine: release dates differ by a year between countries and festivals,
    and Plex copes with that.  Such folders are not listed.
  - One film has that title and the file's length fits it: its year is suggested.
  - Several films share the title: the length of the file picks the one that fits, if exactly
    one does.  Otherwise the options are listed and you choose with  set N YEAR.
  - A suggestion that moves the year by 10 or more years is a BIG CHANGE (the folder may be an
    original or a TV series, not the remake or film found): it is listed apart and not renamed
    unless you confirm it with  set N YEAR.  A folder year of this year or later is clearly
    wrong, so those are renamed normally.
  - Films whose length is wildly different from the file's (shorts, TV episodes) are ignored.
  - The title is not in the database at all: it is listed as "could not check" and left alone.

Folders the private-movies scan has already confirmed (film_signals_cache.json) are skipped:
their title and year matched a real film.  Run  private_movies.py  and type  adult  first
to make that list; without it every folder is checked.

You see ONE recap and are asked once.  At the prompt:
    y            rename everything under "Will be renamed"
    skip 3 7     leave numbers 3 and 7 alone
    set 12 1987  give number 12 this year (also for the ones that need a choice)
    list         show the recap again
    q            quit without renaming anything
Options:  --dry-run  show the recap, rename nothing    --yes  rename without asking
A folder is only renamed; nothing is deleted.  It never overwrites a folder that is already
there, except an EMPTY one of the new name, which it removes first.  It will not run while the
recorder is copying into the library.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_movie_years as base  # noqa: E402
import private_movies as private  # noqa: E402
import wanted_from_empty as scan  # noqa: E402

TITLE_AND_YEAR = re.compile(r'^(.*?)\s*\(((?:19|20)\d{2})\)\s*$')


def dated_folders(movies_dir):
    """[(folder name, title, year)] for movie folders named Title (YYYY) that hold a video."""
    found = []
    for name in sorted(os.listdir(movies_dir), key=str.lower):
        path = os.path.join(movies_dir, name)
        match = TITLE_AND_YEAR.match(name)
        if (match and not name.startswith(('.', '_')) and os.path.isdir(path) and scan.has_video(path)):
            found.append((name, match.group(1).strip(), match.group(2)))
    return found


def file_minutes(folder, ffprobe='ffprobe'):
    """Length in minutes of the longest video file in the folder, or 0 if it cannot be read."""
    longest = 0.0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if name.startswith('.') or os.path.splitext(name)[1].lower() not in scan.VIDEO_EXTENSIONS:
                continue
            try:
                out = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                                      '-of', 'default=nw=1:nk=1', os.path.join(root, name)],
                                     capture_output=True, text=True, timeout=60).stdout.strip()
                longest = max(longest, float(out))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
    return longest / 60


BIG_CHANGE_YEARS = 10


def judge(year, candidates, minutes, this_year=None):
    """(suggested year, reason, options text) for a folder named with ``year``.

    Reasons: ok (nothing to do), unknown, fix (rename), big (a change of 10+ years: needs your
    confirmation), choose (several films fit).  ``suggested`` is '' unless the reason is fix.
    """
    if this_year is None:
        import datetime
        this_year = datetime.date.today().year
    if not candidates:
        return '', 'unknown', ''
    # Films whose running time is wildly different from the file are not this film.
    plausible = [c for c in candidates
                 if not minutes or not c.get('runtime') or 0.6 * minutes <= c['runtime'] <= 1.6 * minutes]
    if any(abs(int(year) - int(c['year'])) <= 1 for c in plausible):
        return '', 'ok', ''
    if not plausible:
        return '', 'unknown', ''

    def describe(c):
        return f"{c['year']}" + (f" ({c['runtime']} min)" if c.get('runtime') else '')
    mine = f' your file is {minutes:.0f} min;' if minutes else ''
    pick = None
    if len(plausible) == 1:
        c = plausible[0]
        if minutes and c.get('runtime') and abs(c['runtime'] - minutes) > max(15.0, minutes * 0.2):
            return '', 'choose', f'{describe(c)};{mine} the length does not fit well: choose with set N YEAR'
        pick, reason = c, 'the only film with this title'
    elif minutes:
        tolerance = max(8.0, minutes * 0.12)
        close = [c for c in plausible if c.get('runtime') and abs(c['runtime'] - minutes) <= tolerance]
        if len(close) == 1:
            pick, reason = close[0], f'its length ({minutes:.0f} min) fits only this one'
    if pick is None:
        return '', 'choose', ', '.join(describe(c) for c in plausible) + f';{mine} choose with set N YEAR'
    jump = abs(int(year) - int(pick['year']))
    if jump >= BIG_CHANGE_YEARS and int(year) < this_year:
        return '', 'big', (f"possibly {describe(pick)};{mine} a {jump}-year change, so it may be a different film "
                           f"or a TV series: confirm with set N {pick['year']}")
    return pick['year'], reason, ''


def build_plan(server, folders, movies_dir, ffprobe, workers=4):
    """One numbered row per folder that needs a look."""
    def check(item):
        name, title, year = item
        info = scan.get_json(server, '/epg-web/api/film-years', {'title': title, 'runtime': 1}, timeout=60)
        candidates = info.get('candidates') or []
        minutes = file_minutes(os.path.join(movies_dir, name), ffprobe) if candidates else 0
        suggestion, reason, options = judge(year, candidates, minutes)
        return {'name': name, 'title': title, 'year': year, 'new': suggestion, 'reason': reason,
                'options': options, 'skip': False}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(check, folders))
    needing = [row for row in rows if row['reason'] not in ('ok',)]
    for number, row in enumerate(needing, 1):
        row['n'] = number
    return needing, len(rows) - len(needing)


def will_rename(plan):
    return [row for row in plan if row['new'] and not row['skip']]


def format_recap(plan):
    go = will_rename(plan)
    lines = [f'Will be renamed ({len(go)}):']
    lines += [f"  {row['n']:>3}. {row['name']}  ->  {row['title']} ({row['new']})   [{row['reason']}]" for row in go] or ['  (none)']
    big = [row for row in plan if row['reason'] == 'big' and not row['new']]
    if big:
        lines.append(f'\nBig changes, check these first: NOT renamed unless you confirm ({len(big)}):')
        lines += [f"  {row['n']:>3}. {row['name']}   {row['options']}" for row in big]
    choose = [row for row in plan if row['reason'] == 'choose' and not row['new']]
    if choose:
        lines.append(f'\nSeveral films share the title: you choose ({len(choose)}):')
        lines += [f"  {row['n']:>3}. {row['name']}   {row['options']}" for row in choose]
    skipped = [row for row in plan if row['skip']]
    if skipped:
        lines.append(f'\nLeft alone by you ({len(skipped)}):')
        lines += [f"  {row['n']:>3}. {row['name']}" for row in skipped]
    unknown = [row for row in plan if row['reason'] == 'unknown']
    if unknown:
        lines.append(f'\nCould not check, the title is not in the film database ({len(unknown)}):')
        lines += [f"  {row['n']:>3}. {row['name']}" for row in unknown]
    return '\n'.join(lines)


def parse_command(text, plan):
    """Returns (action, message); action is 'apply', 'quit' or 'continue'."""
    parts = text.strip().lower().split()
    if not parts:
        return 'continue', 'Type y to rename, skip N, set N YEAR, list, or q.'
    command, rest = parts[0], parts[1:]
    if command in ('y', 'yes'):
        return 'apply', ''
    if command in ('q', 'quit', 'n', 'no'):
        return 'quit', ''
    if command == 'list':
        return 'continue', format_recap(plan)
    by_number = {row['n']: row for row in plan}
    if command == 'skip':
        numbers = []
        for item in rest:
            if not item.isdigit() or int(item) not in by_number:
                return 'continue', f'"{item}" is not a number from the list.'
            numbers.append(int(item))
        if not numbers:
            return 'continue', 'skip needs numbers, for example: skip 3 7'
        for number in numbers:
            by_number[number]['skip'] = True
        return 'continue', f'Left alone: {", ".join(str(n) for n in numbers)}.  Type list to check, y to rename.'
    if command == 'set':
        if (len(rest) != 2 or not rest[0].isdigit() or int(rest[0]) not in by_number
                or not re.fullmatch(r'(19|20)\d{2}', rest[1])):
            return 'continue', 'set needs a list number and a four-digit year, for example: set 12 1987'
        row = by_number[int(rest[0])]
        row['new'], row['skip'] = rest[1], False
        if row['reason'] in ('choose', 'unknown', 'big'):
            row['reason'] = 'your choice'
        return 'continue', f'{row["n"]}. {row["name"]} will be renamed to "{row["title"]} ({rest[1]})".'
    return 'continue', f'Unknown command "{command}". Type y, skip N, set N YEAR, list, or q.'


def change_year(library_dir, name, title, new_year):
    """Rename ``name`` to "Title (new_year)".  Never overwrites; an EMPTY folder of that name is replaced."""
    if not re.fullmatch(r'(19|20)\d{2}', str(new_year)):
        raise ValueError(f'"{new_year}" is not a year')
    new_name = f'{title} ({new_year})'
    source = os.path.join(library_dir, name)
    target = os.path.join(library_dir, new_name)
    if os.path.exists(target) and os.path.normcase(target) != os.path.normcase(source):
        if os.path.isdir(target) and not os.listdir(target):
            os.rmdir(target)               # an empty leftover folder: safe to replace
        else:
            raise FileExistsError(f'"{new_name}" already exists and is not empty')
    os.rename(source, target)
    for entry in sorted(os.listdir(target)):          # a file named like the old folder follows it
        path = os.path.join(target, entry)
        if os.path.isfile(path) and entry.startswith(name):
            renamed = new_name + entry[len(name):]
            if not os.path.exists(os.path.join(target, renamed)):
                os.rename(path, os.path.join(target, renamed))
    return new_name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--movies', help='Plex Movies folder (default: plex_path from the recorder config)')
    parser.add_argument('--server', help='dashboard address (default: server_url from the recorder config)')
    parser.add_argument('--cache', help='the private-movies scan cache (default: film_signals_cache.json next to the config)')
    parser.add_argument('--dry-run', action='store_true', help='show the recap, rename nothing')
    parser.add_argument('--yes', action='store_true', help='rename without asking')
    args = parser.parse_args(argv)
    default_movies, default_server = base.load_defaults(args.config)
    movies_dir = args.movies or default_movies
    server = args.server or default_server
    if not server:
        print('No dashboard address: set server_url in the recorder config or use --server.')
        return 1
    if not movies_dir or not os.path.isdir(movies_dir):
        print(f'Cannot open the Movies folder ({movies_dir or "not set"}). Is the Plex share mounted?')
        return 1
    if not args.dry_run:
        copying = base.recorder_copying(server)
        if copying:
            print('The recorder is copying into the library right now (' + ', '.join(copying) +
                  '). Run this again when it has finished.')
            return 1
    cache = private.load_cache(args.cache or private.default_cache_path(args.config))
    verified = {name for name, info in cache.items() if isinstance(info, dict) and info.get('found')}
    folders = [item for item in dated_folders(movies_dir) if item[0] not in verified]
    if not folders:
        print('Every dated folder is already confirmed by the film database.')
        return 0
    ffprobe = base.load_config_value(args.config, 'ffprobe') or 'ffprobe'
    print(f'Checking {len(folders)} folder(s) ({len(verified)} already confirmed are skipped)...\n')
    plan, fine = build_plan(server, folders, movies_dir, ffprobe)
    print(f'{fine} of them have a year that matches a real film.\n')
    if not plan:
        print('Nothing to correct.')
        return 0
    print(format_recap(plan))
    if not will_rename(plan) and args.yes:
        print('\nNothing to rename.')
        return 0
    if not args.yes:
        print('\nType one command and press Enter:\n'
              '  y            rename everything under "Will be renamed"\n'
              '  skip 3 7     leave numbers 3 and 7 alone\n'
              '  set 12 1987  give number 12 this year (also for the ones that need a choice)\n'
              '  list         show the list again\n'
              '  q            quit without changing anything')
        while True:
            try:
                text = input('> ')
            except EOFError:
                text = 'q'
            action, message = parse_command(text, plan)
            if message:
                print(message)
            if action == 'quit':
                print('Nothing was renamed.')
                return 0
            if action == 'apply':
                break
    renamed, failed = [], []
    for row in will_rename(plan):
        if args.dry_run:
            print(f'  (dry run) would rename "{row["name"]}" to "{row["title"]} ({row["new"]})"')
            renamed.append(row)
            continue
        try:
            new_name = change_year(movies_dir, row['name'], row['title'], row['new'])
        except (OSError, ValueError) as exc:
            failed.append((row, str(exc)))
            continue
        print(f'  Renamed to "{new_name}"')
        renamed.append(row)
    print(f'\nDone. {"Would rename" if args.dry_run else "Renamed"} {len(renamed)}, could not rename {len(failed)}.')
    for row, error in failed:
        print(f'  Not renamed: {row["name"]} ({error})')
    if renamed and not args.dry_run:
        print('Scan the Movies library in Plex to pick up the new names.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
