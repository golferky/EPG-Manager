#!/usr/bin/env python3
"""Move the damaged films found by check_library.py out of Plex and put them on Wanted.

Run on the Mac mini, after check_library.py has found damaged films:
    python3 ~/EPG-Manager/mac/fix_damaged.py --dry-run     show the recap, change nothing
    python3 ~/EPG-Manager/mac/fix_damaged.py               show the recap, ask, then do it

It reads the results saved by check_library.py (library_check_cache.json) and lists every film in the
Plex Movies library with 50 or more decode errors, as long as the file is unchanged since it was checked.
For each film you approve it:
    1. moves the folder to  Movies Needing Attention/Damaged  (never overwrites, never deletes), and
    2. adds the film to Wanted, using the year in its folder name, so it records again from the next airing
       on a commercial-free channel.
A folder with no year in its name is moved but cannot be added to Wanted (Wanted needs a year).

At the prompt:
    y            do everything listed
    skip 3 7     leave numbers 3 and 7 alone
    nowanted 5   move number 5 but do not add it to Wanted
    list         show the list again
    q            quit without changing anything
--yes does everything without asking.  It refuses to run while the recorder is copying into the library.
"""
import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_library as library_check  # noqa: E402
import plex_movie_years as base  # noqa: E402
import wanted_from_empty as scan  # noqa: E402

TITLE_AND_YEAR = re.compile(r'^(.*?)\s*\(((?:19|20)\d{2})\)\s*$')


def damaged_films(library, cache, limit):
    """[{'folder', 'title', 'year', 'errors', 'mode'}] for films in the library whose saved result is damaged.

    A saved result only counts while the file is unchanged since it was checked.
    """
    films = []
    for folder, video in library_check.movie_files(library):
        entry = cache.get(video)
        if not entry or entry.get('errors') is None or entry['errors'] < limit:
            continue
        if {'size': entry.get('size'), 'mtime': entry.get('mtime')} != library_check.fingerprint(video):
            continue
        match = TITLE_AND_YEAR.match(folder)
        title, year = (match.group(1), match.group(2)) if match else (folder, '')
        films.append({'folder': folder, 'title': title, 'year': year, 'errors': entry['errors'],
                      'mode': entry.get('mode') or '', 'skip': False, 'wanted': bool(year)})
    films.sort(key=lambda film: -film['errors'])
    for number, film in enumerate(films, 1):
        film['n'] = number
    return films


def format_recap(films):
    if not films:
        return 'No damaged films found in the saved results.'
    todo = [f for f in films if not f['skip']]
    lines = [f'Will be moved out of Plex ({len(todo)}), worst first:']
    for film in films:
        if film['skip']:
            continue
        note = 'and added to Wanted' if film['wanted'] else 'moved only' + (
            ' (no year in the name)' if not film['year'] else ' (not added to Wanted)')
        quick = ', last 10 minutes' if film['mode'] == 'quick' else ''
        lines.append(f"  {film['n']:>3}. {film['folder']}: {film['errors']} errors{quick}  ->  {note}")
    skipped = [f for f in films if f['skip']]
    if skipped:
        lines.append(f'\nLeft alone by you ({len(skipped)}):')
        lines += [f"  {f['n']:>3}. {f['folder']}" for f in skipped]
    return '\n'.join(lines)


def parse_command(text, films):
    """Returns (action, message); action is 'apply', 'quit' or 'continue'."""
    parts = text.strip().lower().split()
    if not parts:
        return 'continue', 'Type y, skip N, nowanted N, list, or q.'
    command, rest = parts[0], parts[1:]
    by_number = {f['n']: f for f in films}
    if command in ('y', 'yes'):
        return 'apply', ''
    if command in ('q', 'quit', 'n', 'no'):
        return 'quit', ''
    if command == 'list':
        return 'continue', format_recap(films)
    if command in ('skip', 'nowanted'):
        numbers = []
        for item in rest:
            if not item.isdigit() or int(item) not in by_number:
                return 'continue', f'"{item}" is not a number from the list.'
            numbers.append(int(item))
        if not numbers:
            return 'continue', f'{command} needs numbers, for example: {command} 3 7'
        for number in numbers:
            if command == 'skip':
                by_number[number]['skip'] = True
            else:
                by_number[number]['wanted'] = False
        return 'continue', ('Left alone: ' if command == 'skip' else 'Will be moved but not added to Wanted: ') + \
            ', '.join(map(str, numbers)) + '.  Type list to check.'
    return 'continue', f'Unknown command "{command}". Type y, skip N, nowanted N, list, or q.'


def apply_plan(films, library, damaged_root, server, dry_run=False, add=None):
    """Move each folder and add it to Wanted.  Returns (moved, wanted, problems)."""
    add = add or scan.add_to_wanted
    moved = wanted = 0
    problems = []
    for film in films:
        if film['skip']:
            continue
        if dry_run:
            print(f"  (dry run) would move \"{film['folder']}\"" + (' and add it to Wanted' if film['wanted'] else ''))
            moved += 1
            wanted += 1 if film['wanted'] else 0
            continue
        try:
            base.move_out(library, film['folder'], damaged_root)
        except (OSError, shutil.Error) as exc:
            problems.append(f"{film['folder']}: not moved ({exc})")
            print(f"  Not moved: {film['folder']} ({exc})")
            continue
        moved += 1
        print(f"  Moved \"{film['folder']}\"")
        if film['wanted']:
            reason = add(server, {'title': film['title'], 'year': film['year'], 'imdb_id': ''})
            if reason:
                problems.append(f"{film['folder']}: moved, but not added to Wanted ({reason})")
                print(f"    Could not add to Wanted: {reason}")
            else:
                wanted += 1
    return moved, wanted, problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--movies', help='Plex Movies folder (default: plex_path from the recorder config)')
    parser.add_argument('--server', help='dashboard address (default: server_url from the recorder config)')
    parser.add_argument('--limit', type=int, default=library_check.DAMAGED_ERROR_LINES,
                        help='errors that count as damaged (default 50)')
    parser.add_argument('--dry-run', action='store_true', help='show the recap and what would happen, change nothing')
    parser.add_argument('--yes', action='store_true', help='do everything without asking')
    args = parser.parse_args(argv)
    default_movies, default_server = base.load_defaults(args.config)
    library = args.movies or default_movies
    server = args.server or default_server
    if not library or not os.path.isdir(library):
        print(f'Cannot open the Movies folder ({library or "not set"}). Is the Plex share mounted?')
        return 1
    cache_path = os.path.join(os.path.dirname(os.path.expanduser(args.config)) or '.', 'library_check_cache.json')
    cache = library_check.load_cache(cache_path)
    if not cache:
        print('No saved scan results. Run check_library.py first.')
        return 1
    if not args.dry_run:
        copying = base.recorder_copying(server)
        if copying:
            print('The recorder is copying into the library right now (' + ', '.join(copying) +
                  '). Run this again when it has finished.')
            return 1
    films = damaged_films(library, cache, args.limit)
    print(format_recap(films))
    if not films:
        return 0
    if not args.yes and not args.dry_run:
        print('\nType one command and press Enter:\n'
              '  y            do everything listed\n'
              '  skip 3 7     leave numbers 3 and 7 alone\n'
              '  nowanted 5   move number 5 but do not add it to Wanted\n'
              '  list         show the list again\n'
              '  q            quit without changing anything')
        while True:
            try:
                text = input('> ')
            except EOFError:
                text = 'q'
            action, message = parse_command(text, films)
            if message:
                print(message)
            if action == 'quit':
                print('Nothing was changed.')
                return 0
            if action == 'apply':
                break
    damaged_root = os.path.join(base.attention_root(library, False, base.load_attention_path(args.config)), 'Damaged')
    print()
    moved, wanted, problems = apply_plan(films, library, damaged_root, server, dry_run=args.dry_run)
    print(f'\nDone. {"Would move" if args.dry_run else "Moved"} {moved} folder(s) out of Plex, '
          f'{"would add" if args.dry_run else "added"} {wanted} to Wanted.')
    for problem in problems:
        print(f'  Problem: {problem}')
    if moved and not args.dry_run:
        print(f'They are in {damaged_root}.  Nothing was deleted.  Scan the Movies library in Plex.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
