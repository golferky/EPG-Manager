#!/usr/bin/env python3
"""Tidy the Plex Movies library: loose video files, and films that have two folders.

Run on the Mac mini:
    python3 ~/EPG-Manager/mac/tidy_library.py loose --dry-run     video files sitting loose in Movies
    python3 ~/EPG-Manager/mac/tidy_library.py dupes --dry-run     one film in two folders

LOOSE FILES.  A video file directly in the Movies folder (not in a folder of its own) is turned
into  Title (Year)/Title.mp4 .  The title and year come from the file name; if it has no year, the
film database is asked (using the file's length to pick between films of one name).  You see ONE
recap and are asked once.  At the prompt:
    y             do everything under "Will be moved into folders"
    skip 3 7      leave numbers 3 and 7 alone
    set 12 1987   give number 12 this year
    title 4 Some Name   give number 4 this title (then it is looked up again if it has no year)
    out 5         move number 5's file out of Plex (to Movies Needing Attention)
    list, q
A file whose film already has a folder is NOT merged: it is listed to compare, and  out N  sends it out.

DUPLICATES.  Folders that look like one film (same title with & and and, apostrophes and accents
ignored, the same year or one year apart, or a year this year or later that is plainly wrong) are
grouped.  Each shows its video's size and length, and a suggestion: keep the larger file when it is
not the shorter one.  At the prompt:
    keep 3 a      keep copy a of group 3, move the others out of Plex
    auto          keep the suggested copy in every group that has a suggestion
    skip 3, list, go (do it), q
The copies that are not kept are MOVED to "Movies Needing Attention/Duplicates", never deleted.

Both refuse to run while the recorder is copying into the library.  --dry-run shows the recap and what
would happen, and moves nothing.
"""
import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_years as years  # noqa: E402
import plex_movie_years as base  # noqa: E402
import wanted_from_empty as scan  # noqa: E402

TITLE_AND_YEAR = re.compile(r'^(.*?)\s*\(((?:19|20)\d{2})\)\s*$')
FILE_TAGS = re.compile(r'\b(16x9|1080p|720p|480p|3d|hd|dvdrip|bluray|brrip|webrip)\b', re.IGNORECASE)
COPY_MARK = re.compile(r'\s*-\s*copy(?:\s*\(\d{1,3}\))?\s*$', re.IGNORECASE)
APOSTROPHE = re.compile(r"(?<=[A-Za-z]) (s|t|re|ll|ve)\b(?=\s|$)")
VOLUME = re.compile(r'\b(Vol)(\d)\b', re.IGNORECASE)
FILE_YEAR = re.compile(r'\(?\b((?:19|20)\d{2})\b\)?\s*$')


# ---------------------------------------------------------------- loose files

def loose_files(movies_dir):
    """Names of video files directly in the Movies folder (not hidden)."""
    return sorted((name for name in os.listdir(movies_dir)
                   if not name.startswith(('.', '_')) and os.path.isfile(os.path.join(movies_dir, name))
                   and os.path.splitext(name)[1].lower() in scan.VIDEO_EXTENSIONS), key=str.lower)


def clean_file_name(filename):
    """(title, year, is_copy) from a file name such as "The_Descendants__2011_.mp4"."""
    stem = os.path.splitext(filename)[0].replace('_', ' ')
    is_copy = bool(COPY_MARK.search(stem))
    stem = COPY_MARK.sub('', stem)
    year = ''
    match = FILE_YEAR.search(stem.strip())
    if match and stem.strip()[:match.start()].strip():
        year = match.group(1)
        stem = stem.strip()[:match.start()]
    stem = FILE_TAGS.sub(' ', stem)
    title = re.sub(r'\s+', ' ', stem).strip(' -.')
    title = VOLUME.sub(r'\1 \2', APOSTROPHE.sub(r"'\1", title))
    return title, year, is_copy


def existing_keys(movies_dir):
    """{(folded title, year)} -> folder name, for the movie folders that hold a video."""
    keys = {}
    for name in os.listdir(movies_dir):
        match = TITLE_AND_YEAR.match(name)
        path = os.path.join(movies_dir, name)
        if match and not name.startswith(('.', '_')) and os.path.isdir(path):
            keys[(base.fold_title(match.group(1)), match.group(2))] = name
    return keys


def video_minutes(path, ffprobe):
    """Length in minutes of one video file, or 0 if it cannot be read."""
    try:
        out = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'default=nw=1:nk=1', path],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out) / 60.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def pick_year(candidates, minutes):
    """(year, note): a year only when one film clearly fits; otherwise '' and what to choose from."""
    if not candidates:
        return '', 'not found in the film database'
    plausible = [c for c in candidates
                 if not minutes or not c.get('runtime') or 0.6 * minutes <= c['runtime'] <= 1.6 * minutes]
    if not plausible:
        return '', 'no film with this title fits the length'

    def describe(c):
        return f"{c['year']}" + (f" ({c['runtime']} min)" if c.get('runtime') else '')
    mine = f' your file is {minutes:.0f} min;' if minutes else ''
    if len(plausible) == 1:
        c = plausible[0]
        if minutes and c.get('runtime') and abs(c['runtime'] - minutes) > max(15.0, minutes * 0.2):
            return '', f'{describe(c)};{mine} the length does not fit well: choose with set N YEAR'
        return str(c['year']), 'the only film with this title'
    if minutes:
        tolerance = max(8.0, minutes * 0.12)
        close = [c for c in plausible if c.get('runtime') and abs(c['runtime'] - minutes) <= tolerance]
        if len(close) == 1:
            return str(close[0]['year']), f'its length ({minutes:.0f} min) fits only this one'
    return '', ', '.join(describe(c) for c in plausible) + f';{mine} choose with set N YEAR'


def lookup_year(server, title, minutes):
    """(year, note) for a title with no year: the film database and the file's length decide."""
    info = scan.get_json(server, '/epg-web/api/film-years', {'title': title, 'runtime': 1}, timeout=60)
    return pick_year(info.get('candidates') or [], minutes)


def build_loose_plan(movies_dir, files, server, ffprobe, workers=4):
    keys = existing_keys(movies_dir)

    def make(item):
        name = item
        title, year, is_copy = clean_file_name(name)
        row = {'file': name, 'title': title, 'year': year, 'note': '', 'out': False, 'skip': False,
               'copy': is_copy, 'minutes': 0}
        if not year and title and server:
            row['minutes'] = video_minutes(os.path.join(movies_dir, name), ffprobe)
            row['year'], row['note'] = lookup_year(server, title, row['minutes'])
        return row
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(make, files))
    for number, row in enumerate(rows, 1):
        row['n'] = number
        refresh_loose_row(row, keys)
        add_compare(row, movies_dir, ffprobe)
    return rows


def refresh_loose_row(row, keys):
    """state: ready (can be filed), exists (its folder is already there), noyear, notitle, copy."""
    if row['copy']:
        row['state'] = 'copy'
    elif not row['title']:
        row['state'] = 'notitle'
    elif not row['year']:
        row['state'] = 'noyear'
    elif (base.fold_title(row['title']), row['year']) in keys:
        row['state'] = 'exists'
        row['folder'] = keys[(base.fold_title(row['title']), row['year'])]
    else:
        row['state'] = 'ready'


def loose_to_move(plan):
    return [r for r in plan if r['state'] == 'ready' and not r['skip'] and not r['out']]


def loose_to_send_out(plan):
    return [r for r in plan if r['out']]


def matching_folders(movies_dir, row):
    """Movie folders that hold the same film as a loose file: same title, and the same year if known."""
    found = []
    for name in sorted(os.listdir(movies_dir), key=str.lower):
        match = TITLE_AND_YEAR.match(name)
        if not match or name.startswith(('.', '_')) or not os.path.isdir(os.path.join(movies_dir, name)):
            continue
        if base.fold_title(match.group(1)) != base.fold_title(row['title']):
            continue
        if row['year'] and abs(int(match.group(2)) - int(row['year'])) > 1:
            continue
        if scan.has_video(os.path.join(movies_dir, name)):
            found.append(name)
    return found


def compare_note(loose, folder):
    """Which is the better copy, a loose file or a folder's video: 'folder', 'loose' or 'unsure'."""
    pick = suggest_keep([loose, folder])
    return 'unsure' if pick is None else ('loose' if pick == 0 else 'folder')


def add_compare(row, movies_dir, ffprobe):
    """Fill row['compare'] with size and length of the loose file and of the folder(s) it may duplicate."""
    row['compare'] = []
    if row['state'] not in ('exists', 'copy'):
        return
    path = os.path.join(movies_dir, row['file'])
    try:
        loose = {'size': os.path.getsize(path), 'minutes': video_minutes(path, ffprobe)}
    except OSError:
        return
    folders = [row['folder']] if row.get('folder') else matching_folders(movies_dir, row)
    for name in folders:
        size, minutes = folder_video_info(os.path.join(movies_dir, name), ffprobe)
        folder = {'size': size, 'minutes': minutes}
        verdict = compare_note(loose, folder)
        row['compare'].append({'folder': name, 'loose': loose, 'copy': folder, 'verdict': verdict})


def compare_lines(row):
    lines = []
    advice = {'folder': 'the folder copy is better, so out N is safe',
              'loose': 'the loose file is the larger one: look at it before moving anything',
              'unsure': 'cannot tell by size and length: play both'}
    for item in row.get('compare') or []:
        loose, copy = item['loose'], item['copy']
        lines.append(f"       loose {size_text(loose['size'])} {loose['minutes']:.0f} min   "
                     f"folder \"{item['folder']}\" {size_text(copy['size'])} {copy['minutes']:.0f} min   -> {advice[item['verdict']]}")
    if row['state'] == 'copy' and not row.get('compare'):
        lines.append('       (no folder with this title found)')
    return lines


def format_loose_recap(plan):
    ready = loose_to_move(plan)
    lines = [f'Will be moved into folders ({len(ready)}):']
    lines += [f"  {r['n']:>3}. {r['file']}  ->  {r['title']} ({r['year']})/{r['title']}{os.path.splitext(r['file'])[1].lower()}"
              for r in ready] or ['  (none)']
    out = loose_to_send_out(plan)
    if out:
        lines.append(f'\nWill be moved out of Plex ({len(out)}):')
        lines += [f"  {r['n']:>3}. {r['file']}" for r in out]
    exists = [r for r in plan if r['state'] == 'exists' and not r['out']]
    if exists:
        lines.append(f'\nThe film already has a folder: compare, then  out N  if this is the copy to remove ({len(exists)}):')
        for r in exists:
            lines.append(f"  {r['n']:>3}. {r['file']}   (folder: {r['folder']})")
            lines += compare_lines(r)
    copies = [r for r in plan if r['state'] == 'copy' and not r['out']]
    if copies:
        lines.append(f'\nLooks like a copy ("-Copy"): probably a duplicate, so  out N  ({len(copies)}):')
        for r in copies:
            lines.append(f"  {r['n']:>3}. {r['file']}")
            lines += compare_lines(r)
    unknown = [r for r in plan if r['state'] in ('noyear', 'notitle') and not r['out'] and not r['skip']]
    if unknown:
        lines.append(f'\nNo year found: give one with  set N YEAR  (or a title with  title N ...) ({len(unknown)}):')
        lines += [f"  {r['n']:>3}. {r['file']}" + (f"   [{r['note']}]" if r['note'] else '') for r in unknown]
    skipped = [r for r in plan if r['skip']]
    if skipped:
        lines.append(f'\nLeft alone by you ({len(skipped)}):')
        lines += [f"  {r['n']:>3}. {r['file']}" for r in skipped]
    return '\n'.join(lines)


def parse_loose_command(text, plan, keys, movies_dir, server='', ffprobe='ffprobe'):
    """Returns (action, message); action is 'apply', 'quit' or 'continue'."""
    parts = text.strip().split()
    if not parts:
        return 'continue', 'Type y, skip N, set N YEAR, title N NAME, out N, list, or q.'
    command, rest = parts[0].lower(), parts[1:]
    by_number = {r['n']: r for r in plan}
    if command in ('y', 'yes'):
        return 'apply', ''
    if command in ('q', 'quit', 'n', 'no'):
        return 'quit', ''
    if command == 'list':
        return 'continue', format_loose_recap(plan)
    if command in ('skip', 'out'):
        numbers = []
        for item in rest:
            if not item.isdigit() or int(item) not in by_number:
                return 'continue', f'"{item}" is not a number from the list.'
            numbers.append(int(item))
        if not numbers:
            return 'continue', f'{command} needs numbers, for example: {command} 3 7'
        for number in numbers:
            by_number[number]['skip' if command == 'skip' else 'out'] = True
        return 'continue', f'{"Left alone" if command == "skip" else "Will go out of Plex"}: ' + ', '.join(map(str, numbers)) + '.  Type list to check.'
    if command == 'set':
        if len(rest) != 2 or not rest[0].isdigit() or int(rest[0]) not in by_number or not re.fullmatch(r'(19|20)\d{2}', rest[1]):
            return 'continue', 'set needs a list number and a four-digit year, for example: set 12 1987'
        row = by_number[int(rest[0])]
        row['year'], row['skip'], row['out'], row['copy'] = rest[1], False, False, False
        refresh_loose_row(row, keys)
        add_compare(row, movies_dir, ffprobe)
        return 'continue', f'{row["n"]}. {row["file"]}: {row["title"]} ({row["year"]})' + ('  (its folder already exists: compare)' if row['state'] == 'exists' else '')
    if command == 'title':
        if len(rest) < 2 or not rest[0].isdigit() or int(rest[0]) not in by_number:
            return 'continue', 'title needs a list number and a title, for example: title 4 The Fugitive'
        row = by_number[int(rest[0])]
        row['title'], row['skip'], row['out'], row['copy'] = ' '.join(rest[1:]), False, False, False
        if not row['year'] and server:
            row['year'], row['note'] = lookup_year(server, row['title'], row['minutes'] or video_minutes(os.path.join(movies_dir, row['file']), ffprobe))
        refresh_loose_row(row, keys)
        add_compare(row, movies_dir, ffprobe)
        found = f' ({row["year"]})' if row['year'] else f'  [{row["note"]}]'
        return 'continue', f'{row["n"]}. {row["file"]}: {row["title"]}{found}'
    return 'continue', f'Unknown command "{command}". Type y, skip N, set N YEAR, title N NAME, out N, list, or q.'


def file_into_folder(movies_dir, filename, title, year):
    """Move a loose file into Title (Year)/Title.ext.  Never overwrites."""
    if not title or not re.fullmatch(r'(19|20)\d{2}', str(year)):
        raise ValueError('a title and a four-digit year are needed')
    folder = os.path.join(movies_dir, f'{title} ({year})')
    target = os.path.join(folder, title + os.path.splitext(filename)[1].lower())
    if os.path.exists(target):
        raise FileExistsError(f'"{os.path.basename(target)}" is already in "{title} ({year})"')
    os.makedirs(folder, exist_ok=True)
    shutil.move(os.path.join(movies_dir, filename), target)
    return target


def attention_dir(movies_dir, config):
    return base.attention_root(movies_dir, False, base.load_attention_path(config))


def run_loose(args, movies_dir, server, ffprobe, move_root):
    files = loose_files(movies_dir)
    if not files:
        print('No loose video files in the Movies folder.')
        return 0
    print(f'{len(files)} loose video file(s) in {movies_dir}.  Looking up years...\n')
    plan = build_loose_plan(movies_dir, files, server, ffprobe)
    keys = existing_keys(movies_dir)
    print(format_loose_recap(plan))
    if not args.yes:
        print('\nType one command and press Enter:\n'
              '  y             do everything under "Will be moved into folders"\n'
              '  skip 3 7      leave numbers 3 and 7 alone\n'
              '  set 12 1987   give number 12 this year\n'
              '  title 4 NAME  give number 4 this title\n'
              '  out 5         move number 5 out of Plex (to Movies Needing Attention)\n'
              '  list          show the list again\n'
              '  q             quit without changing anything')
        while True:
            try:
                text = input('> ')
            except EOFError:
                text = 'q'
            action, message = parse_loose_command(text, plan, keys, movies_dir, server, ffprobe)
            if message:
                print(message)
            if action == 'quit':
                print('Nothing was moved.')
                return 0
            if action == 'apply':
                break
    moved = failed = sent = 0
    for row in loose_to_move(plan):
        if args.dry_run:
            print(f'  (dry run) would file "{row["file"]}" as "{row["title"]} ({row["year"]})"')
            moved += 1
            continue
        try:
            file_into_folder(movies_dir, row['file'], row['title'], row['year'])
            print(f'  Filed "{row["file"]}" as "{row["title"]} ({row["year"]})"')
            moved += 1
        except (OSError, ValueError, shutil.Error) as exc:
            print(f'  Not moved: {row["file"]} ({exc})')
            failed += 1
    for row in loose_to_send_out(plan):
        if args.dry_run:
            print(f'  (dry run) would move "{row["file"]}" out of Plex')
            sent += 1
            continue
        try:
            os.makedirs(move_root, exist_ok=True)
            destination = os.path.join(move_root, row['file'])
            if os.path.exists(destination):
                raise FileExistsError('already in Movies Needing Attention')
            shutil.move(os.path.join(movies_dir, row['file']), destination)
            print(f'  Moved "{row["file"]}" out of Plex')
            sent += 1
        except (OSError, shutil.Error) as exc:
            print(f'  Not moved out: {row["file"]} ({exc})')
            failed += 1
    left = len(plan) - moved - sent
    print(f'\nDone. {"Would file" if args.dry_run else "Filed"} {moved}, {"would move" if args.dry_run else "moved"} out {sent}, '
          f'left alone {left}, could not do {failed}.')
    if (moved or sent) and not args.dry_run:
        print('Scan the Movies library in Plex to pick up the changes.  Nothing was deleted.')
    return 0


# ---------------------------------------------------------------- duplicates

def folder_video_info(path, ffprobe):
    """(size in bytes of the largest video, its minutes) for one movie folder."""
    best = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            if not name.startswith('.') and os.path.splitext(name)[1].lower() in scan.VIDEO_EXTENSIONS:
                best = max(best, os.path.getsize(os.path.join(root, name)))
    return best, years.file_minutes(path, ffprobe)


def find_groups(movies_dir, this_year=None):
    """Groups of folder names that look like one film.  Returns [[name, ...], ...]."""
    this_year = this_year or datetime.date.today().year
    by_title = {}
    for name in sorted(os.listdir(movies_dir), key=str.lower):
        match = TITLE_AND_YEAR.match(name)
        path = os.path.join(movies_dir, name)
        if match and not name.startswith(('.', '_')) and os.path.isdir(path) and scan.has_video(path):
            by_title.setdefault(base.fold_title(match.group(1)), []).append((name, int(match.group(2))))
    groups = []
    for members in by_title.values():
        if len(members) < 2:
            continue
        parent = list(range(len(members)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i][1], members[j][1]
                if abs(a - b) <= 1 or a >= this_year or b >= this_year:
                    parent[find(i)] = find(j)
        clusters = {}
        for i, (name, _year) in enumerate(members):
            clusters.setdefault(find(i), []).append(name)
        groups += [sorted(names, key=str.lower) for names in clusters.values() if len(names) > 1]
    return sorted(groups, key=lambda names: names[0].lower())


def suggest_keep(copies):
    """Index of the copy to keep, or None: the larger file, if it is not clearly the shorter one."""
    if not copies:
        return None
    ranked = sorted(range(len(copies)), key=lambda i: copies[i]['size'], reverse=True)
    best = ranked[0]
    others = [copies[i] for i in ranked[1:]]
    if copies[best]['size'] <= max(o['size'] for o in others) * 1.02:
        return None                      # the same size: no way to tell them apart from here
    if any(copies[best]['minutes'] and o['minutes'] and copies[best]['minutes'] < o['minutes'] * 0.9 for o in others):
        return None                      # bigger but much shorter: look at it yourself
    return best


def build_dupe_plan(movies_dir, ffprobe, groups=None):
    groups = groups if groups is not None else find_groups(movies_dir)
    plan = []
    for number, names in enumerate(groups, 1):
        copies = []
        for name in names:
            size, minutes = folder_video_info(os.path.join(movies_dir, name), ffprobe)
            copies.append({'name': name, 'size': size, 'minutes': minutes})
        plan.append({'n': number, 'copies': copies, 'suggest': suggest_keep(copies), 'keep': None, 'skip': False})
    return plan


def size_text(size):
    return f'{size / 1e9:.1f} GB' if size >= 1e9 else f'{size / 1e6:.0f} MB'


def format_dupe_recap(plan):
    lines = []
    for group in plan:
        status = ('  (skipped)' if group['skip'] else
                  f"  -> keep {chr(97 + group['keep'])}" if group['keep'] is not None else '')
        lines.append(f"{group['n']:>3}.{status}")
        for i, copy in enumerate(group['copies']):
            marker = '   <- suggested' if group['suggest'] == i and group['keep'] is None else ''
            mine = '   KEEP' if group['keep'] == i else ('   (moves out)' if group['keep'] is not None else '')
            lines.append(f"      {chr(97 + i)}) {copy['name']}   {size_text(copy['size'])}  {copy['minutes']:.0f} min{marker}{mine}")
    return '\n'.join(lines) if lines else 'No duplicate folders found.'


def parse_dupe_command(text, plan):
    """Returns (action, message); action is 'apply', 'quit' or 'continue'."""
    parts = text.strip().lower().split()
    if not parts:
        return 'continue', 'Type keep N a, auto, skip N, list, go, or q.'
    command, rest = parts[0], parts[1:]
    by_number = {g['n']: g for g in plan}
    if command in ('go', 'y', 'yes'):
        return 'apply', ''
    if command in ('q', 'quit'):
        return 'quit', ''
    if command == 'list':
        return 'continue', format_dupe_recap(plan)
    if command == 'auto':
        count = 0
        for group in plan:
            if group['suggest'] is not None and group['keep'] is None and not group['skip']:
                group['keep'] = group['suggest']
                count += 1
        return 'continue', (f'Suggested copy kept in {count} group(s).  Type list to check, go to move the others out.'
                            if count else 'No group has a suggestion: use  keep N a  to choose.')
    if command == 'keep':
        if len(rest) != 2 or not rest[0].isdigit() or int(rest[0]) not in by_number or len(rest[1]) != 1:
            return 'continue', 'keep needs a group number and a letter, for example: keep 3 a'
        group = by_number[int(rest[0])]
        index = ord(rest[1]) - 97
        if not 0 <= index < len(group['copies']):
            return 'continue', f'Group {group["n"]} has copies a to {chr(96 + len(group["copies"]))}.'
        group['keep'], group['skip'] = index, False
        return 'continue', f'Group {group["n"]}: keep {rest[1]} ({group["copies"][index]["name"]}); the others move out.'
    if command == 'skip':
        numbers = [int(x) for x in rest if x.isdigit() and int(x) in by_number]
        if not numbers or len(numbers) != len(rest):
            return 'continue', 'skip needs group numbers from the list, for example: skip 3 7'
        for number in numbers:
            by_number[number]['skip'], by_number[number]['keep'] = True, None
        return 'continue', 'Left alone: ' + ', '.join(map(str, numbers)) + '.'
    return 'continue', f'Unknown command "{command}". Type keep N a, auto, skip N, list, go, or q.'


def run_dupes(args, movies_dir, ffprobe, move_root):
    groups = find_groups(movies_dir)
    if not groups:
        print('No duplicate folders found.')
        return 0
    print(f'{len(groups)} film(s) with more than one folder.  Measuring the files...\n')
    plan = build_dupe_plan(movies_dir, ffprobe, groups)
    print(format_dupe_recap(plan))
    if not args.yes:
        print('\nType one command and press Enter:\n'
              '  keep 3 a   keep copy a of group 3 and move the others out of Plex\n'
              '  auto       keep the suggested copy (the larger file) in every group that has one\n'
              '  skip 3     leave group 3 alone\n'
              '  list       show the list again\n'
              '  go         do it\n'
              '  q          quit without changing anything')
        while True:
            try:
                text = input('> ')
            except EOFError:
                text = 'q'
            action, message = parse_dupe_command(text, plan)
            if message:
                print(message)
            if action == 'quit':
                print('Nothing was moved.')
                return 0
            if action == 'apply':
                break
    else:
        parse_dupe_command('auto', plan)
    dupe_root = os.path.join(move_root, 'Duplicates')
    moved = failed = 0
    for group in plan:
        if group['keep'] is None or group['skip']:
            continue
        for i, copy in enumerate(group['copies']):
            if i == group['keep']:
                continue
            if args.dry_run:
                print(f'  (dry run) would move "{copy["name"]}" out of Plex (keeping "{group["copies"][group["keep"]]["name"]}")')
                moved += 1
                continue
            try:
                base.move_out(movies_dir, copy['name'], dupe_root)
                print(f'  Moved "{copy["name"]}" out of Plex (kept "{group["copies"][group["keep"]]["name"]}")')
                moved += 1
            except (OSError, shutil.Error) as exc:
                print(f'  Not moved: {copy["name"]} ({exc})')
                failed += 1
    print(f'\nDone. {"Would move" if args.dry_run else "Moved"} {moved} folder(s) out, could not move {failed}, '
          f'left alone {sum(1 for g in plan if g["keep"] is None)} group(s).')
    if moved and not args.dry_run:
        print(f'They are in {dupe_root}.  Nothing was deleted.  Scan the Movies library in Plex.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['loose', 'dupes'])
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--movies', help='Plex Movies folder (default: plex_path from the recorder config)')
    parser.add_argument('--server', help='dashboard address (default: server_url from the recorder config)')
    parser.add_argument('--dry-run', action='store_true', help='show the recap, move nothing')
    parser.add_argument('--yes', action='store_true', help='do everything suggested without asking')
    args = parser.parse_args(argv)
    default_movies, default_server = base.load_defaults(args.config)
    movies_dir = args.movies or default_movies
    server = args.server or default_server
    if not movies_dir or not os.path.isdir(movies_dir):
        print(f'Cannot open the Movies folder ({movies_dir or "not set"}). Is the Plex share mounted?')
        return 1
    if not args.dry_run:
        copying = base.recorder_copying(server)
        if copying:
            print('The recorder is copying into the library right now (' + ', '.join(copying) +
                  '). Run this again when it has finished.')
            return 1
    ffprobe = base.load_config_value(args.config, 'ffprobe') or 'ffprobe'
    move_root = attention_dir(movies_dir, args.config)
    if args.mode == 'loose':
        return run_loose(args, movies_dir, server, ffprobe, move_root)
    return run_dupes(args, movies_dir, ffprobe, move_root)


if __name__ == '__main__':
    sys.exit(main())
