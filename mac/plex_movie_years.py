#!/usr/bin/env python3
"""Give every Plex movie (or TV show) folder a year:  Title  ->  Title (2004).

Run on the Mac mini:
    python3 ~/EPG-Manager/mac/plex_movie_years.py          movies
    python3 ~/EPG-Manager/mac/plex_tv_years.py             TV shows (same tool, --tv)
Add --dry-run to see what it would ask without renaming anything.

For each folder with no (YYYY) it shows a year suggested by the dashboard's lookup
(a show's first year for TV).  The lookup goes by title, so for two films or shows
with one title it can suggest the wrong one: you decide each time.

It will not run while the recorder is copying a file into the library, and it never
overwrites an existing folder.  Folders starting with _ (such as _Needs Episode Info)
are left alone.  Plex sees a renamed folder as a new item, so watched marks and
ratings for that title can be lost: rename titles you care about last, or skip them.
    Enter       accept the suggestion
    2004        type the year instead
    s           skip this one
    q           stop
Only the folder name changes; Plex re-reads it on the next library scan.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

YEAR_AT_END = re.compile(r'\((19\d{2}|20\d{2})\)\s*$')


def yearless_folders(movies_dir):
    """Movie folders (not hidden, not starting with _) whose name has no (YYYY)."""
    found = []
    for name in sorted(os.listdir(movies_dir)):
        if name.startswith(('.', '_')):
            continue
        if os.path.isdir(os.path.join(movies_dir, name)) and not YEAR_AT_END.search(name):
            found.append(name)
    return found


def suggest_year(server, title):
    url = server.rstrip('/') + '/epg-web/api/prog-info?' + urllib.parse.urlencode({'title': title})
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            info = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return ''
    match = re.search(r'\b(19\d{2}|20\d{2})\b', str(info.get('year', '')))
    return match.group(1) if match else ''


def rename_with_year(movies_dir, name, year):
    """Rename ``name`` to ``name (year)``.  Returns the new name; refuses to overwrite."""
    if not re.fullmatch(r'(19|20)\d{2}', str(year)):
        raise ValueError(f'"{year}" is not a year')
    new_name = f'{name.strip()} ({year})'
    source = os.path.join(movies_dir, name)
    target = os.path.join(movies_dir, new_name)
    if os.path.exists(target):
        raise FileExistsError(f'"{new_name}" already exists')
    os.rename(source, target)
    return new_name


def load_defaults(config_path, tv=False):
    """(library folder, dashboard address) from the recorder's config."""
    try:
        with open(os.path.expanduser(config_path), encoding='utf-8') as handle:
            cfg = json.load(handle)
    except (OSError, ValueError):
        return '', ''
    folder = cfg.get('plex_tv_path', '') if tv else cfg.get('plex_path', '')
    return folder, cfg.get('server_url', '')


def recorder_copying(server):
    """Titles the recorder is converting or copying into the library right now.

    Renaming a folder under a copy in progress could send the file to the wrong place.
    Returns [] when nothing is copying, or when the dashboard cannot be asked.
    """
    if not server:
        return []
    url = server.rstrip('/') + '/epg-web/api/recordings/overview'
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return []
    return [str(item.get('title', '')) for item in data.get('finishing', [])]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--tv', action='store_true', help='work on TV shows instead of movies')
    parser.add_argument('--movies', help='library folder (default: plex_path, or plex_tv_path with --tv, from the recorder config)')
    parser.add_argument('--server', help='dashboard address (default: server_url from the recorder config)')
    parser.add_argument('--dry-run', action='store_true', help='ask and show, but rename nothing')
    args = parser.parse_args(argv)
    default_movies, default_server = load_defaults(args.config, args.tv)
    movies_dir = args.movies or default_movies
    server = args.server or default_server
    kind = 'TV show' if args.tv else 'movie'
    if not movies_dir or not os.path.isdir(movies_dir):
        print(f'Cannot open the {kind} folder ({movies_dir or "not set"}). Is the Plex share mounted?')
        return 1
    todo = yearless_folders(movies_dir)
    if not todo:
        print(f'Every {kind} folder already has a year.')
        return 0
    if not args.dry_run:
        copying = recorder_copying(server)
        if copying:
            print('The recorder is copying into the library right now (' + ', '.join(copying) +
                  '). Run this again when it has finished.')
            return 1
    print(f'{len(todo)} {kind} folder(s) have no year in {movies_dir}\n')
    renamed = skipped = 0
    for name in todo:
        suggestion = suggest_year(server, name) if server else ''
        hint = f'suggested {suggestion}' if suggestion else 'no suggestion'
        while True:
            answer = input(f'{name}  [{hint}]  Enter=accept, year, s=skip, q=quit: ').strip().lower()
            if answer == 'q':
                print(f'\nStopped. Renamed {renamed}, skipped {skipped}, {len(todo) - renamed - skipped} left.')
                return 0
            if answer == 's':
                skipped += 1
                break
            year = suggestion if answer == '' else answer
            if not re.fullmatch(r'(19|20)\d{2}', year):
                print('  Type a four-digit year, s to skip, or q to stop.')
                continue
            if args.dry_run:
                print(f'  (dry run) would rename to "{name} ({year})"')
                renamed += 1
                break
            try:
                new_name = rename_with_year(movies_dir, name, year)
            except (OSError, ValueError) as exc:
                print(f'  Not renamed: {exc}')
                skipped += 1
                break
            print(f'  Renamed to "{new_name}"')
            renamed += 1
            break
    print(f'\nDone. Renamed {renamed}, skipped {skipped}. Scan the {"TV Shows" if args.tv else "Movies"} library in Plex to pick up the new names.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
