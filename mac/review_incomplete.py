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
A Plex copy that is longer can still be damaged (a stream stall leaves green or blocky video), so
each one is also decoded from start to end and its errors counted.  A copy with 50 or more errors is
listed as damaged, and its partial should be kept.  This takes a few minutes per film; --no-decode
skips it.

This only reads.  It never moves or deletes anything.
"""
import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_years as years  # noqa: E402
import plex_movie_years as base  # noqa: E402
import tidy_library as tidy  # noqa: E402
import wanted_from_empty as scan  # noqa: E402

INCOMPLETE_NAME = re.compile(r'^(?P<title>.+?) - (?:INCOMPLETE|DAMAGED) - (?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{4})\.(?P<ext>\w+)$')
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


DAMAGED_ERROR_LINES = 50


def main_video(folder):
    """The largest video file in a movie folder (the film itself), or '' if there is none."""
    best, best_size = '', -1
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if name.startswith('.') or os.path.splitext(name)[1].lower() not in scan.VIDEO_EXTENSIONS:
                continue
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            if size > best_size:
                best, best_size = path, size
    return best


def count_decode_errors(path, ffmpeg='ffmpeg'):
    """Decode a video from start to end and count FFmpeg's error lines; None if it could not run."""
    try:
        result = subprocess.run([ffmpeg, '-v', 'error', '-i', path, '-f', 'null', '-'],
                                capture_output=True, text=True, timeout=4 * 3600)
    except (OSError, subprocess.SubprocessError):
        return None
    return len([line for line in result.stderr.splitlines() if line.strip()])


def judge(minutes, plex_minutes):
    """(verdict, text): how a Plex copy compares with the incomplete file."""
    if not plex_minutes:
        return 'not_in_plex', 'not in Plex'
    if plex_minutes >= minutes - 1:
        return 'covered', f'Plex has {plex_minutes:.0f} min (this file {minutes:.0f} min)'
    return 'shorter', f'Plex has only {plex_minutes:.0f} min (this file {minutes:.0f} min)'


def review(folder, movies_dir, tv_dir, ffprobe, workers=4, check_decode=True, ffmpeg='ffmpeg',
           limit=DAMAGED_ERROR_LINES):
    def one(name):
        title, year, date = parse_name(name)
        path = os.path.join(folder, name)
        size = os.path.getsize(path)
        minutes = tidy.video_minutes(path, ffprobe)
        movies = folders_for(movies_dir, title, year) if movies_dir else []
        if movies:
            lengths = {m: years.file_minutes(os.path.join(movies_dir, m), ffprobe) for m in movies}
            best_folder = max(lengths, key=lengths.get)
            verdict, text = judge(minutes, lengths[best_folder])
            if verdict == 'covered' and check_decode:
                video = main_video(os.path.join(movies_dir, best_folder))
                errors = count_decode_errors(video, ffmpeg) if video else None
                if errors is None:
                    text += ' (video not checked)'
                elif errors >= limit:
                    verdict = 'damaged'
                    text = f'Plex has {lengths[best_folder]:.0f} min but its video is damaged ({errors} decode errors)'
                elif errors:
                    text += f', {errors} minor decode errors'
                else:
                    text += ', decodes cleanly'
        elif tv_dir and folders_for(tv_dir, title, ''):
            verdict, text = 'tv', 'TV show: check by hand'
        else:
            verdict, text = judge(minutes, 0)
        return {'name': name, 'title': title, 'date': date, 'size': size, 'minutes': minutes,
                'verdict': verdict, 'text': text}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, incomplete_files(folder)))


GROUPS = [
    ('covered', 'Plex already has an equal or longer copy that decodes well: the incomplete file adds nothing'),
    ('damaged', 'Plex has a longer copy but its video is damaged: keep the partial until it is replaced'),
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
    parser.add_argument('--no-decode', action='store_true',
                        help='do not decode the Plex copies to check them for damage (much faster)')
    args = parser.parse_args(argv)
    movies_dir, _server = base.load_defaults(args.config)
    tv_dir, _server = base.load_defaults(args.config, tv=True)
    folder = args.folder or (os.path.join(os.path.dirname(os.path.abspath(movies_dir)), 'Incomplete Recordings')
                             if movies_dir else '')
    if not folder or not os.path.isdir(folder):
        print(f'Cannot open the Incomplete Recordings folder ({folder or "not set"}). Is the Plex share mounted?')
        return 1
    ffprobe = base.load_config_value(args.config, 'ffprobe') or 'ffprobe'
    ffmpeg = base.load_config_value(args.config, 'ffmpeg') or 'ffmpeg'
    print(f'Measuring the files in {folder} ...')
    if not args.no_decode:
        print('Plex copies that cover a partial are also decoded to check for damage: a few minutes per film.')
    print()
    print(format_report(review(folder, movies_dir if movies_dir and os.path.isdir(movies_dir) else '',
                               tv_dir if tv_dir and os.path.isdir(tv_dir) else '', ffprobe,
                               check_decode=not args.no_decode, ffmpeg=ffmpeg)))
    print('\nNothing was moved or deleted.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
