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
    add 3 5 7       choose numbers from the list on screen
    chosen          show your choices
    drop 5          take one off your choices
    go              move the chosen movies
    q               quit without moving anything
"""
import argparse
import os
import sys

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
