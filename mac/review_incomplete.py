#!/usr/bin/env python3
"""Look through the "Incomplete Recordings" folder and say which files Plex already makes redundant.

Run on the Mac mini:
    python3 ~/EPG-Manager/mac/review_incomplete.py

For each "Title - INCOMPLETE - date.mp4" it measures the file, looks for a movie folder with the same
title (and a year within one) in the Plex Movies folder, and compares lengths:
    Plex has a longer or equal copy   the incomplete file adds nothing
    Plex copy is shorter              the incomplete file is the longer one: look at both
    not in Plex                       nothing replaces it: keep it, or record the film again
    TV show                           a series episode: check by hand
This only reads.  It never moves or deletes anything.
"""
import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_years as years  # noqa: E402
import plex_movie_years as base  # noqa: E402
import tidy_library as tidy  # noqa: E402
import wanted_from_empty as scan  # noqa: E402

INCOMPLETE_NAME = re.compile(r'^(?P<title>.+?) - INCOMPLETE - (?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{4})\.(?P<ext>\w+)$')
TITLE_YEAR = re.compile(r'^(.*?)\s*\(((?:19|20)\d{2})\)\s*$')


def parse_name(filename):
    """(title, year, date) from an incomplete file's name, or None if it is not one."""
    match = INCOMPLETE_NAME.match(filename)
    if not match:
        return None
    title, year = match.group('title').strip(), ''
    with_year = TITLE_YEAR.match(title)
    if with_year:
        title, year = with_year.group(1).strip(), with_year.group(2)
    return title, year, match.group('date')


def incomplete_files(folder):
    return sorted(name for name in os.listdir(folder)
                  if not name.startswith('.') and parse_name(name) is not None
                  and os.path.isfile(os.path.join(folder, name)))


def folders_for(library, title, year):
    """Folders in a library named for the same title (the year within one, when both are known)."""
    found = []
    wanted = base.fold_title(title)
    for name in sorted(os.listdir(library), key=str.lower):
        path = os.path.join(library, name)
        if name.startswith(('.', '_')) or not os.path.isdir(path):
            continue
        match = TITLE_YEAR.match(name)
        folder_title, folder_year = (match.group(1), match.group(2)) if match else (name, '')
        if base.fold_title(folder_title) != wanted:
            continue
        if year and folder_year and abs(int(year) - int(folder_year)) > 1:
            continue
        found.append(name)
    return found


def judge(minutes, plex_minutes):
    """(verdict, text): how a Plex copy compares with the incomplete file."""
    if not plex_minutes:
        return 'not_in_plex', 'not in Plex'
    if plex_minutes >= minutes - 1:
        return 'covered', f'Plex has {plex_minutes:.0f} min (this file {minutes:.0f} min)'
    return 'shorter', f'Plex has only {plex_minutes:.0f} min (this file {minutes:.0f} min)'


def review(folder, movies_dir, tv_dir, ffprobe, workers=4):
    def one(name):
        title, year, date = parse_name(name)
        path = os.path.join(folder, name)
        size = os.path.getsize(path)
        minutes = tidy.video_minutes(path, ffprobe)
        movies = folders_for(movies_dir, title, year) if movies_dir else []
        if movies:
            best = max(years.file_minutes(os.path.join(movies_dir, m), ffprobe) for m in movies)
            verdict, text = judge(minutes, best)
        elif tv_dir and folders_for(tv_dir, title, ''):
            verdict, text = 'tv', 'TV show: check by hand'
        else:
            verdict, text = judge(minutes, 0)
        return {'name': name, 'title': title, 'date': date, 'size': size, 'minutes': minutes,
                'verdict': verdict, 'text': text}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, incomplete_files(folder)))


GROUPS = [
    ('covered', 'Plex already has an equal or longer copy: the incomplete file adds nothing'),
    ('shorter', 'Plex has a shorter copy: the incomplete file is the longer one, so look at both'),
    ('not_in_plex', 'Not in Plex: nothing replaces these'),
    ('tv', 'TV episodes: check by hand'),
]


def format_report(rows):
    lines = []
    for verdict, heading in GROUPS:
        group = [r for r in rows if r['verdict'] == verdict]
        if not group:
            continue
        total = sum(r['size'] for r in group)
        lines.append(f'{heading} ({len(group)}, {tidy.size_text(total)}):')
        for r in sorted(group, key=lambda r: (r['title'].lower(), r['date'])):
            lines.append(f"  {r['title']}  [{r['date']}]  {tidy.size_text(r['size'])}, {r['minutes']:.0f} min:  {r['text']}")
        lines.append('')
    return '\n'.join(lines).rstrip() or 'No incomplete recordings found.'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--folder', help='Incomplete Recordings folder (default: next to the Movies folder)')
    args = parser.parse_args(argv)
    movies_dir, _server = base.load_defaults(args.config)
    tv_dir, _server = base.load_defaults(args.config, tv=True)
    folder = args.folder or (os.path.join(os.path.dirname(os.path.abspath(movies_dir)), 'Incomplete Recordings')
                             if movies_dir else '')
    if not folder or not os.path.isdir(folder):
        print(f'Cannot open the Incomplete Recordings folder ({folder or "not set"}). Is the Plex share mounted?')
        return 1
    ffprobe = base.load_config_value(args.config, 'ffprobe') or 'ffprobe'
    print(f'Measuring the files in {folder} ...\n')
    print(format_report(review(folder, movies_dir if movies_dir and os.path.isdir(movies_dir) else '',
                               tv_dir if tv_dir and os.path.isdir(tv_dir) else '', ffprobe)))
    print('\nNothing was moved or deleted.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
