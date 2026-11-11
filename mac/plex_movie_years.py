#!/usr/bin/env python3
"""Give every Plex movie (or TV show) folder a year:  Title  ->  Title (2004).

Run on the Mac mini:
    python3 ~/EPG-Manager/mac/plex_movie_years.py          movies
    python3 ~/EPG-Manager/mac/plex_tv_years.py             TV shows (same tool, --tv)

It looks up a year for every folder that has none (a show's first year for TV), then
shows ONE recap and asks once.  The lookup goes by title, so for two films or shows with
one title it can suggest the wrong one: read the recap before you say y.

Folders with a junk name (a YYYYMMDD HHMMSS stamp, or a -Copy(1) ending) are not renamed:
they are moved out of Plex, into "Movies Needing Attention" (or "TV Needing Attention")
next to the library, so you can look at each one.  Nothing is deleted.

For a junk-named folder it also works out a cleaned title (stamp and copy suffix removed),
looks that up, and shows what the folder could be called, for example
"Bottle Shock (2008)".  You decide: leave it to be moved out, or  fix 32  to rename it
(and the file inside) in place.

At the recap prompt:
    y            rename the "Will be renamed" list, fix the "fixed in place" list and
                 move the "Will be moved" list
    skip 3 7     leave numbers 3 and 7 alone (renamed, fixed or moved)
    fix 32       rename junk-named number 32 in place to its cleaned name and year
    fix 32 2008  the same, with a year you give
    fix all      fix every junk-named folder that has a cleaned name and year
    rename only  leave every junk-named folder alone: only the plain renames happen
    move 32      put a fixed one back to "move out of Plex"
    set 12 2008  give number 12 (or change its) year
    list         show the recap again
    q            quit without renaming anything
Options:
    --dry-run    show the recap and what would happen, rename nothing
    --yes        rename every suggestion without asking (after printing the recap)

It will not run while the recorder is copying a file into the library, and it never
overwrites an existing folder.  Folders starting with _ (such as _Needs Episode Info)
are left alone.  Plex sees a renamed folder as a new item, so watched marks and
ratings for that title can be lost.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import shutil
import sys
import unicodedata
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


# A folder name that is a leftover, not a title: a recording stamp ("... 20260205 181000")
# or a file-manager copy suffix ("...-Copy(1)", "... - Copy", "... Copy(2)").
JUNK_NAME = re.compile(r'(?:\b\d{8}[ _-]\d{4,6}\b|[-_]\s*copy(?:\s*\(\d+\))?\s*$|\s+copy\s*\(\d+\)\s*$)',
                       re.IGNORECASE)


def junk_folders(library_dir):
    """Library folders (not hidden, not starting with _) whose name looks like a leftover."""
    found = []
    for name in sorted(os.listdir(library_dir)):
        if name.startswith(('.', '_')):
            continue
        if os.path.isdir(os.path.join(library_dir, name)) and JUNK_NAME.search(name):
            found.append(name)
    return found


_STAMP = re.compile(r'\b\d{8}[ _-]\d{4,6}\b')
_COPY_SUFFIX = re.compile(r'(?:[-_]\s*copy(?:\s*\(\d+\))?|\s+copy\s*\(\d+\))\s*$', re.IGNORECASE)
_TRAILING_YEAR = re.compile(r'\(\s*((?:19|20)\d{2})\s*\)\s*$')


def clean_junk_title(name):
    """(title, year hint) from a junk folder name: the stamp and copy suffix removed.

    "Bottle Shock 20260205 181000-Copy(1)" -> ("Bottle Shock", "").  A year already in the
    name is kept as the hint:  "Film 20260205 181000 (2008)" -> ("Film", "2008").
    """
    text = name
    year = ''
    for _ in range(4):                      # the pieces can come in any order
        before = text
        text = _STAMP.sub(' ', text)
        text = _COPY_SUFFIX.sub('', text.strip())
        match = _TRAILING_YEAR.search(text.strip())
        if match:
            year = match.group(1)
            text = text.strip()[:match.start()]
        text = re.sub(r'[ _]+', ' ', text).strip(' -_.')
        if text == before:
            break
    return text, year


def fix_in_place(library_dir, old_name, title, year):
    """Rename a junk-named folder to "Title (Year)" and the files in it that carry its old name.

    Never overwrites.  Returns the new folder name.
    """
    if not title or not re.fullmatch(r'(19|20)\d{2}', str(year)):
        raise ValueError('a cleaned title and a four-digit year are needed')
    new_name = f'{title} ({year})'
    source = os.path.join(library_dir, old_name)
    target = os.path.join(library_dir, new_name)
    if os.path.exists(target):
        raise FileExistsError(f'"{new_name}" already exists')
    os.rename(source, target)
    for entry in sorted(os.listdir(target)):
        path = os.path.join(target, entry)
        if os.path.isfile(path) and entry.startswith(old_name):
            renamed = title + entry[len(old_name):]
            if not os.path.exists(os.path.join(target, renamed)):
                os.rename(path, os.path.join(target, renamed))
    return new_name


def attention_root(library_dir, tv=False, configured=''):
    """Folder, outside the Plex library, for folders that need a person to look at them."""
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(os.path.dirname(os.path.abspath(library_dir)),
                        'TV Needing Attention' if tv else 'Movies Needing Attention')


def move_out(library_dir, name, root):
    """Move one library folder into ``root``.  Never overwrites; returns the new path."""
    source = os.path.join(library_dir, name)
    destination = os.path.join(root, name)
    if os.path.exists(destination):
        raise FileExistsError(f'"{name}" is already in {root}')
    os.makedirs(root, exist_ok=True)
    shutil.move(source, destination)
    return destination


def suggest_year(server, title, kind=''):
    """A year for ``title`` from the dashboard's lookup.

    ``kind`` is 'series' or 'movie'.  Without it the lookup can return a film that shares
    a show's name (Outlander the 2008 film, Yellowstone the 1936 film).
    """
    query = {'title': title}
    if kind in ('series', 'movie'):
        query['content_type'] = kind
    url = server.rstrip('/') + '/epg-web/api/prog-info?' + urllib.parse.urlencode(query)
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


_TITLE_LIGATURES = {'\u00e6': 'ae', '\u0153': 'oe', '\u00df': 'ss', '\u00f8': 'o', '\u0111': 'd',
                    '\u00f0': 'd', '\u00fe': 'th', '\u0142': 'l', '\u0131': 'i'}


def fold_title(value):
    """Lower case, accents and ligatures folded, only letters and digits kept."""
    text = str(value or '').lower()
    for special, plain in _TITLE_LIGATURES.items():
        text = text.replace(special, plain)
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r'[^a-z0-9]', '', text)


def load_defaults(config_path, tv=False):
    """(library folder, dashboard address) from the recorder's config."""
    try:
        with open(os.path.expanduser(config_path), encoding='utf-8') as handle:
            cfg = json.load(handle)
    except (OSError, ValueError):
        return '', ''
    folder = cfg.get('plex_tv_path', '') if tv else cfg.get('plex_path', '')
    return folder, cfg.get('server_url', '')


def load_attention_path(config_path):
    """needs_attention_path from the recorder config, or ''."""
    try:
        with open(os.path.expanduser(config_path), encoding='utf-8') as handle:
            return str(json.load(handle).get('needs_attention_path', '') or '')
    except (OSError, ValueError):
        return ''


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


def gather_suggestions(server, names, kind, workers=8):
    """{folder name: suggested year or ''} for every name, looked up a few at a time."""
    if not server or not names:
        return {name: '' for name in names}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        years = list(pool.map(lambda name: suggest_year(server, name, kind), names))
    return dict(zip(names, years))


def build_plan(names, suggestions, junk=(), cleaned=None):
    """One row per folder, numbered from 1.

    A junk-named folder is moved out by default.  ``cleaned`` maps its name to
    {'title', 'year', 'exists'}: what it could be called, and whether that folder is taken.
    """
    junk = set(junk)
    cleaned = cleaned or {}
    rows = []
    for i, name in enumerate(names, 1):
        info = cleaned.get(name, {})
        rows.append({'n': i, 'name': name, 'year': '' if name in junk else suggestions.get(name, ''),
                     'action': 'move' if name in junk else 'rename', 'skip': False,
                     'clean': info.get('title', ''), 'clean_year': info.get('year', ''),
                     'exists': bool(info.get('exists'))})
    return rows


def will_rename(plan):
    return [row for row in plan if row['action'] == 'rename' and row['year'] and not row['skip']]


def will_move(plan):
    return [row for row in plan if row['action'] == 'move' and not row['skip']]


def will_fix(plan):
    return [row for row in plan if row['action'] == 'fix' and not row['skip']]


def cleaned_name(row):
    """\"Title (Year)\" for a junk row, or '' when there is no usable title and year."""
    return f"{row['clean']} ({row['clean_year']})" if row.get('clean') and row.get('clean_year') else ''


def format_recap(plan, move_root=''):
    """The recap text: what will be renamed, moved, or left alone, and why."""
    lines = []
    go, move, fix = will_rename(plan), will_move(plan), will_fix(plan)
    width = min(52, max([len(row['name']) for row in plan] + [10]))

    def entry(row, arrow):
        name = row['name'] if len(row['name']) <= width else row['name'][:width - 1] + '\u2026'
        return f"  {row['n']:>3}. {name:<{width}}  {arrow}"
    lines.append(f'Will be renamed ({len(go)}):')
    lines += [entry(row, f"->  {row['name']} ({row['year']})") for row in go] or ['  (none)']
    if fix:
        lines.append(f'\nWill be fixed in place, folder and file renamed ({len(fix)}):')
        lines += [entry(row, f"->  {row['clean']} ({row['year']})") for row in fix]
    if move:
        lines.append(f'\nWill be moved out of Plex to {move_root or "a Needing Attention folder"} ({len(move)}):')
        for row in move:
            hint = cleaned_name(row)
            if hint and row.get('exists'):
                note = f'junk name; "{hint}" already exists: likely a duplicate'
            elif hint:
                note = f'junk name; cleaned: "{hint}"  (fix {row["n"]} to rename it in place)'
            else:
                note = 'junk name: needs a look'
            lines.append(entry(row, note))
    skipped = [row for row in plan if row['skip']]
    if skipped:
        lines.append(f'\nSkipped by you ({len(skipped)}):')
        lines += [entry(row, '(left as is)') for row in skipped]
    nothing = [row for row in plan if row['action'] == 'rename' and not row['year'] and not row['skip']]
    if nothing:
        lines.append(f'\nNo year found, left alone unless you set one ({len(nothing)}):')
        lines += [entry(row, 'no suggestion') for row in nothing]
    return '\n'.join(lines)


def parse_command(text, plan, move_root=''):
    """Apply a recap command to the plan.  Returns (action, message).

    action is 'apply', 'quit' or 'continue'.
    """
    parts = text.strip().lower().split()
    if not parts:
        return 'continue', 'Type y to go ahead, skip N, set N YEAR, list, or q.'
    command, rest = parts[0], parts[1:]
    if command in ('y', 'yes'):
        return 'apply', ''
    if command in ('q', 'quit', 'n', 'no'):
        return 'quit', ''
    if command == 'list':
        return 'continue', format_recap(plan, move_root)
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
        return 'continue', f'Left alone: {", ".join(str(n) for n in numbers)}.  Type list to see the recap, y to go ahead.'
    if command in ('rename', 'renameonly', 'rename-only') and (rest == ['only'] or command != 'rename'):
        count = 0
        for row in plan:
            if row['action'] in ('move', 'fix') and not row['skip']:
                row['skip'] = True
                count += 1
        return 'continue', (f'{count} junk-named folder(s) will be left alone. Only the plain renames will happen.  '
                            'Type list to check, y to go ahead.' if count else
                            'There are no junk-named folders to leave out.')
    if command == 'fix':
        if rest == ['all']:
            count = 0
            for row in plan:
                if row['action'] == 'move' and not row['skip'] and cleaned_name(row) and not row['exists']:
                    row['action'], row['year'] = 'fix', row['clean_year']
                    count += 1
            return 'continue', (f'{count} folder(s) will be fixed in place.  Type list to see them, y to go ahead.'
                                if count else 'No junk-named folder has a cleaned name and year without a clash.')
        if not rest or not rest[0].isdigit() or int(rest[0]) not in by_number or len(rest) > 2:
            return 'continue', 'fix needs a list number, and optionally a year: fix 32   or   fix 32 2008'
        row = by_number[int(rest[0])]
        if row['action'] not in ('move', 'fix'):
            return 'continue', f'{row["n"]}. {row["name"]} does not have a junk name; use set {row["n"]} YEAR.'
        year = rest[1] if len(rest) == 2 else row['clean_year']
        if not re.fullmatch(r'(19|20)\d{2}', year or ''):
            return 'continue', f'No year found for "{row["clean"]}". Give one: fix {row["n"]} 2008'
        if not row['clean']:
            return 'continue', f'Could not work out a title from "{row["name"]}".'
        row['clean_year'] = year
        if row['exists'] and not len(rest) == 2:
            return 'continue', f'"{cleaned_name(row)}" already exists, so this is likely a duplicate. Skip it, or move it out.'
        row['action'], row['year'], row['skip'] = 'fix', year, False
        return 'continue', f'{row["n"]}. will be renamed to "{row["clean"]} ({year})".'
    if command == 'move':
        if len(rest) != 1 or not rest[0].isdigit() or int(rest[0]) not in by_number:
            return 'continue', 'move needs a list number: move 32'
        row = by_number[int(rest[0])]
        if row['action'] == 'fix':
            row['action'], row['year'] = 'move', ''
        return 'continue', f'{row["n"]}. will be moved out of Plex.'
    if command == 'set':
        if (len(rest) != 2 or not rest[0].isdigit() or int(rest[0]) not in by_number
                or not re.fullmatch(r'(19|20)\d{2}', rest[1])):
            return 'continue', 'set needs a list number and a four-digit year, for example: set 12 2008'
        row = by_number[int(rest[0])]
        if row['action'] in ('move', 'fix'):
            return 'continue', (f'{row["n"]}. {row["name"]} has a junk name. Use  fix {row["n"]} {rest[1]}  '
                                'to rename it in place with that year, or skip it.')
        row['year'], row['skip'] = rest[1], False
        return 'continue', f'{row["n"]}. {row["name"]} will be renamed to "{row["name"]} ({row["year"]})".'
    return 'continue', f'Unknown command "{command}". Type y, skip N, set N YEAR, list, or q.'


def apply_plan(movies_dir, plan, dry_run=False, move_root=''):
    """Rename the rows with a year and move the junk-named rows out.

    Returns (renamed rows, moved rows, [(row, error)]); fixed rows count as renamed.
    """
    renamed, moved, failed = [], [], []
    for row in will_fix(plan):
        if dry_run:
            print(f'  (dry run) would fix "{row["name"]}" to "{row["clean"]} ({row["year"]})"')
            renamed.append(row)
            continue
        try:
            new_name = fix_in_place(movies_dir, row['name'], row['clean'], row['year'])
        except (OSError, ValueError) as exc:
            failed.append((row, str(exc)))
            continue
        print(f'  Fixed to "{new_name}"')
        renamed.append(row)
    for row in will_rename(plan):
        if dry_run:
            print(f'  (dry run) would rename "{row["name"]}" to "{row["name"]} ({row["year"]})"')
            renamed.append(row)
            continue
        try:
            new_name = rename_with_year(movies_dir, row['name'], row['year'])
        except (OSError, ValueError) as exc:
            failed.append((row, str(exc)))
            continue
        print(f'  Renamed to "{new_name}"')
        renamed.append(row)
    for row in will_move(plan):
        if dry_run:
            print(f'  (dry run) would move "{row["name"]}" to {move_root}')
            moved.append(row)
            continue
        try:
            move_out(movies_dir, row['name'], move_root)
        except (OSError, shutil.Error) as exc:
            failed.append((row, str(exc)))
            continue
        print(f'  Moved "{row["name"]}" to {move_root}')
        moved.append(row)
    return renamed, moved, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--tv', action='store_true', help='work on TV shows instead of movies')
    parser.add_argument('--movies', help='library folder (default: plex_path, or plex_tv_path with --tv, from the recorder config)')
    parser.add_argument('--server', help='dashboard address (default: server_url from the recorder config)')
    parser.add_argument('--dry-run', action='store_true', help='show the recap and what would happen, rename nothing')
    parser.add_argument('--yes', action='store_true', help='rename every suggestion without asking')
    args = parser.parse_args(argv)
    default_movies, default_server = load_defaults(args.config, args.tv)
    movies_dir = args.movies or default_movies
    server = args.server or default_server
    kind = 'TV show' if args.tv else 'movie'
    library = 'TV Shows' if args.tv else 'Movies'
    if not movies_dir or not os.path.isdir(movies_dir):
        print(f'Cannot open the {kind} folder ({movies_dir or "not set"}). Is the Plex share mounted?')
        return 1
    junk = junk_folders(movies_dir)
    todo = sorted(set(yearless_folders(movies_dir)) | set(junk))
    if not todo:
        print(f'Every {kind} folder already has a year, and none has a junk name.')
        return 0
    move_root = attention_root(movies_dir, args.tv, load_attention_path(args.config))
    if not args.dry_run:
        copying = recorder_copying(server)
        if copying:
            print('The recorder is copying into the library right now (' + ', '.join(copying) +
                  '). Run this again when it has finished.')
            return 1
    print(f'{len(todo)} {kind} folder(s) need attention in {movies_dir} ({len(junk)} with a junk name).  '
          'Looking up years...\n')
    cleaned = {name: dict(zip(('title', 'hint'), clean_junk_title(name))) for name in junk}
    lookup = sorted({name for name in todo if name not in set(junk)} |
                    {info['title'] for info in cleaned.values() if info['title']})
    suggestions = gather_suggestions(server, lookup, 'series' if args.tv else 'movie')
    for name, info in cleaned.items():
        year = suggestions.get(info['title'], '') or info['hint']
        target = f"{info['title']} ({year})" if info['title'] and year else ''
        info.update(year=year, exists=bool(target and os.path.exists(os.path.join(movies_dir, target))))
    plan = build_plan(todo, suggestions, junk, cleaned)
    print(format_recap(plan, move_root))
    if not will_rename(plan) and not will_move(plan) and not will_fix(plan):
        print('\nNothing to do. Use  set N YEAR  to give a folder a year, or q to quit.' if not args.yes else '\nNothing to do.')
    if not args.yes:
        print('\nType one command and press Enter:\n'
              '  y            go ahead and do everything listed above\n'
              '  skip 3 7     leave numbers 3 and 7 alone (use the numbers at the left of each line)\n'
              '  set 12 2008  give number 12 the year 2008\n'
              + ('  fix 32       rename junk-named number 32 in place to its cleaned name (fix all = every one)\n'
                 '  rename only  leave every junk-named folder alone, just do the plain renames\n'
                 if any(row['action'] != 'rename' for row in plan) else '')
              + '  list         show the list again\n'
              '  q            quit without changing anything')
        while True:
            try:
                text = input('> ')
            except EOFError:
                text = 'q'
            action, message = parse_command(text, plan, move_root)
            if message:
                print(message)
            if action == 'quit':
                print('Nothing was renamed.')
                return 0
            if action == 'apply':
                break
    renamed, moved, failed = apply_plan(movies_dir, plan, args.dry_run, move_root)
    left = [row for row in plan if row['skip'] or (row['action'] == 'rename' and not row['year'])]
    verb = ('Would rename or fix', 'would move') if args.dry_run else ('Renamed or fixed', 'moved')
    print(f'\nDone. {verb[0]} {len(renamed)}, {verb[1]} {len(moved)}, '
          f'left alone {len(left)}, could not do {len(failed)}.')
    for row, error in failed:
        print(f'  Not renamed: {row["name"]} ({error})')
    if left:
        print('Left alone: ' + '; '.join(row['name'] for row in left[:20]) + (' ...' if len(left) > 20 else ''))
    if moved and not args.dry_run:
        print(f'The moved folders are in {move_root}.  Nothing was deleted.')
    if (renamed or moved) and not args.dry_run:
        print(f'Scan the {library} library in Plex to pick up the changes.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
