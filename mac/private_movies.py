#!/usr/bin/env python3
"""Keep some movies private: move them out of the Plex Movies library.

Run on the Mac mini:
    python3 ~/EPG-Manager/mac/private_movies.py            choose movies to make private
    python3 ~/EPG-Manager/mac/private_movies.py --restore choose private movies to put back
    python3 ~/EPG-Manager/mac/private_movies.py --show     list what is private

A private movie is moved to "Movies Private", next to the Movies folder (set
private_movies_path in the recorder config to change it).  Plex only shows what is in the
libraries you give it, so a private movie disappears from Plex for everyone.  To watch them
yourself, add "Movies Private" to Plex as its own Movies library and do not share it.

The dashboard and the recorder still count a private movie as one you have, so it is
never offered for recording again.  Nothing is deleted or copied: a folder is moved on the
same share, and it never overwrites a folder that is already there.

At the prompt:
    list            show every movie that can be chosen
    find TEXT       show the movies whose name contains TEXT
    adult           check every movie against the film database and list the ones that look
                    adult (flag, NC-17/X rating, keywords like "softcore"), with the reasons
    add 3 5 7       choose numbers from the list on screen   (add all: every number on screen)
    chosen          show your choices
    drop 5          take one off your choices
    go              move the chosen movies
    q               quit without moving anything
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_movie_years as base  # noqa: E402
import wanted_from_empty as scan  # noqa: E402


def private_root(movies_dir, configured=''):
    """The private folder: configured, or "Movies Private" next to the Movies folder."""
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(os.path.dirname(os.path.abspath(movies_dir)), 'Movies Private')


def load_private_path(config_path):
    return base.load_config_value(config_path, 'private_movies_path')


def library_films(folder):
    """Names of the movie folders (not hidden, not starting with _) that hold a video file."""
    films = []
    if not os.path.isdir(folder):
        return films
    for name in sorted(os.listdir(folder), key=str.lower):
        path = os.path.join(folder, name)
        if not name.startswith(('.', '_')) and os.path.isdir(path) and scan.has_video(path):
            films.append(name)
    return films


def matching(names, text):
    """The names containing ``text`` (accents and punctuation ignored)."""
    wanted = base.fold_title(text)
    return [name for name in names if wanted and wanted in base.fold_title(name)]


def show_numbered(names, title):
    print(f'\n{title} ({len(names)}):')
    for number, name in enumerate(names, 1):
        print(f'  {number:>3}. {name}')


# Keywords in the film database that point to adult content, strongest first.  A rating of
# NC-17 or X, or the database's adult flag, counts for more.  Nothing here is certain: the
# list is a set of suggestions, and you choose.
ADULT_KEYWORDS = ('softcore', 'erotica', 'erotic movie', 'erotic thriller', 'sexploitation',
                  'adult film', 'pornograph', 'erotic')
LIKELY_SCORE = 2


def adult_signals(info):
    """(score, reasons) from one film's database entry.  Score 2 or more is a likely adult film."""
    if not info or not info.get('found'):
        return 0, []
    score, reasons = 0, []
    if info.get('adult'):
        score += 3
        reasons.append('adult flag')
    rating = str(info.get('certification') or '').upper()
    if rating in ('NC-17', 'X'):
        score += 3
        reasons.append(f'rated {rating}')
    elif rating in ('NR', 'UNRATED', 'NOT RATED'):
        score += 1
        reasons.append('unrated')
    hits = [k for k in info.get('keywords', []) if any(word in str(k).lower() for word in ADULT_KEYWORDS)]
    if hits:
        score += 2
        reasons.append('keywords: ' + ', '.join(hits[:3]))
    return score, reasons


def default_cache_path(config_path):
    return os.path.join(os.path.dirname(os.path.expanduser(config_path)), 'film_signals_cache.json')


def load_cache(path):
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path, cache):
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(cache, handle)
    except OSError:
        pass


def scan_adult(server, names, cache, workers=4):
    """Ask the dashboard about every film.  Returns (candidates, checked, not_found).

    ``candidates`` is [(name, score, reasons)] with a score of 2 or more, strongest first.
    Answers the dashboard found are kept in ``cache`` (by folder name), so a second scan
    only asks about what is new.
    """
    def ask(name):
        if name in cache:
            return name, cache[name]
        match = scan.TITLE_AND_YEAR.match(name)
        title, year = (match.group(1).strip(), match.group(2)) if match else (name.strip(), '')
        info = scan.get_json(server, '/epg-web/api/film-signals', {'title': title, 'year': year}, timeout=30)
        return name, info
    with ThreadPoolExecutor(max_workers=workers) as pool:
        answers = list(pool.map(ask, names))
    candidates, not_found = [], 0
    for name, info in answers:
        if info and info.get('found'):
            cache[name] = info
        else:
            not_found += 1
        score, reasons = adult_signals(info)
        if score >= LIKELY_SCORE:
            candidates.append((name, score, reasons))
    candidates.sort(key=lambda item: (-item[1], item[0].lower()))
    return candidates, len(names), not_found


def parse_numbers(words, limit):
    """([numbers], error message) from the words after add/drop."""
    numbers = []
    for word in words:
        if not word.isdigit() or not 1 <= int(word) <= limit:
            return [], f'"{word}" is not a number from the list on screen (1 to {limit}).'
        numbers.append(int(word))
    return numbers, ('' if numbers else 'Give one or more numbers, for example: add 3 5 7')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--movies', help='Plex Movies folder (default: plex_path from the recorder config)')
    parser.add_argument('--private', help='private folder (default: Movies Private next to the Movies folder)')
    parser.add_argument('--server', help='dashboard address (default: server_url from the recorder config)')
    parser.add_argument('--restore', action='store_true', help='put private movies back into the library')
    parser.add_argument('--show', action='store_true', help='list the private movies and exit')
    parser.add_argument('--cache', help='where to remember what the film database said (default: film_signals_cache.json next to the config)')
    parser.add_argument('--dry-run', action='store_true', help='show what would be moved, move nothing')
    args = parser.parse_args(argv)
    default_movies, default_server = base.load_defaults(args.config)
    movies_dir = args.movies or default_movies
    server = args.server or default_server
    private_dir = args.private or private_root(movies_dir or '', load_private_path(args.config))
    if not movies_dir or not os.path.isdir(movies_dir):
        print(f'Cannot open the Movies folder ({movies_dir or "not set"}). Is the Plex share mounted?')
        return 1
    if args.show:
        names = library_films(private_dir)
        print(f'{len(names)} private movie(s) in {private_dir}' + (':' if names else '.'))
        for name in names:
            print(f'  {name}')
        return 0
    source_dir, target_dir = (private_dir, movies_dir) if args.restore else (movies_dir, private_dir)
    verb = 'put back into the library' if args.restore else 'made private'
    names = library_films(source_dir)
    if not names:
        print(f'No movies to choose from in {source_dir}.')
        return 0
    if not args.dry_run:
        copying = base.recorder_copying(server)
        if copying:
            print('The recorder is copying into the library right now (' + ', '.join(copying) +
                  '). Run this again when it has finished.')
            return 1
    print(f'{len(names)} movie(s) in {source_dir}.  Moving a movie sends it to {target_dir}.')
    print('Type find TEXT to look for one, add N to choose it, go to move your choices, q to quit.')
    cache_path = args.cache or default_cache_path(args.config)
    shown, chosen = names, []
    while True:
        try:
            text = input('> ')
        except EOFError:
            text = 'q'
        parts = text.strip().split()
        command, rest = (parts[0].lower(), parts[1:]) if parts else ('', [])
        if command in ('q', 'quit'):
            print('Nothing was moved.')
            return 0
        if command == 'list':
            shown = names
            show_numbered(shown, 'Movies')
        elif command == 'find':
            found = matching(names, ' '.join(rest))
            if not rest:
                print('find needs some text, for example: find minority')
            elif not found:
                print(f'No movie matches "{" ".join(rest)}".')
            else:
                shown = found
                show_numbered(shown, f'Movies matching "{" ".join(rest)}"')
        elif command == 'adult':
            if args.restore:
                print('adult works on the Movies library, not on the private folder.')
                continue
            if not server:
                print('The dashboard address is needed: set server_url in the recorder config or use --server.')
                continue
            print(f'Checking {len(names)} movie(s) with the film database (a few minutes the first time)...')
            cache = load_cache(cache_path)
            candidates, checked, not_found = scan_adult(server, names, cache)
            save_cache(cache_path, cache)
            if not_found == checked:
                print('None of them could be looked up: the dashboard may have no TMDb key, or cannot reach TMDb.')
                continue
            shown = [name for name, _score, _reasons in candidates]
            print(f'\n{len(candidates)} of {checked} look adult ({not_found} not found in the database):')
            for number, (name, _score, reasons) in enumerate(candidates, 1):
                print(f'  {number:>3}. {name}   [{"; ".join(reasons)}]')
            if candidates:
                print('Type add 3 5 to choose some, add all for every one, then go.  Check the list first: it is a guess.')
        elif command == 'add' and rest == ['all']:
            for name in shown:
                if name not in chosen:
                    chosen.append(name)
            print(f'{len(chosen)} chosen.  Type chosen to see them, go to move them.')
        elif command == 'add':
            numbers, error = parse_numbers(rest, len(shown))
            if error:
                print(error)
            else:
                for number in numbers:
                    if shown[number - 1] not in chosen:
                        chosen.append(shown[number - 1])
                print(f'{len(chosen)} chosen.  Type chosen to see them, go to move them.')
        elif command == 'chosen':
            show_numbered(chosen, 'Chosen') if chosen else print('Nothing chosen yet.')
        elif command == 'drop':
            numbers, error = parse_numbers(rest, len(chosen))
            if error:
                print(error)
            else:
                for name in [chosen[n - 1] for n in numbers]:
                    chosen.remove(name)
                print(f'{len(chosen)} chosen.')
        elif command == 'go':
            if not chosen:
                print('Nothing chosen yet: use find and add first.')
                continue
            break
        else:
            print('Type list, find TEXT, add N, chosen, drop N, go, or q.')
    moved, failed = [], []
    for name in chosen:
        if args.dry_run:
            print(f'  (dry run) would move "{name}" to {target_dir}')
            moved.append(name)
            continue
        try:
            base.move_out(source_dir, name, target_dir)
        except (OSError, base.shutil.Error) as exc:
            failed.append((name, str(exc)))
            continue
        print(f'  Moved "{name}"')
        moved.append(name)
    print(f'\nDone. {"Would move" if args.dry_run else "Moved"} {len(moved)}, could not move {len(failed)}.')
    for name, error in failed:
        print(f'  Not moved: {name} ({error})')
    if moved and not args.dry_run:
        if args.restore:
            print('Scan the Movies library in Plex so the films show up again.')
        else:
            print(f'They are now {verb} in {target_dir}.  Nothing was deleted.')
            print('Scan the Movies library in Plex so they disappear from it.  To watch them yourself, add '
                  f'"{os.path.basename(target_dir)}" in Plex as its own Movies library and do not share it.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
