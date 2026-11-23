#!/usr/bin/env python3
"""Work out which episode of a show each video in a folder is, by listening to it, and file the sure ones in Plex.

Run on the Mac mini:
    python3 ~/EPG-Manager/mac/identify_episodes.py --show "Everybody Loves Raymond" --folder ~/raymond_episodes_v2
    python3 ~/EPG-Manager/mac/identify_episodes.py --show "Everybody Loves Raymond" --folder ~/raymond_episodes_v2 --apply

For videos with no guide data (a 24/7 channel).  Each video's speech is turned into text on this Mac (nothing is
sent anywhere), then matched against the plot summaries of every episode of the show (TMDb, plus OMDb plots, fetched
through the dashboard, which holds the keys).  An episode's summary scores by the share of its telling words that are
actually spoken, rare words counting most, and the best episode must clearly beat the runner-up.

This is a GUESS.  It prints each video's best match with the next closest, so you can check by eye.  Only a
confident match is filed in Plex, and only with --apply.  Anything unsure stays where it is; if you check one yourself
add it with --accept "2026-11-22_0808=S02E15" (the stamp is in the file name).  Nothing is ever deleted or overwritten.

Needs a free speech-to-text package, once:   pip3 install faster-whisper
The first run downloads the speech model (about 500 MB for small.en; --model base.en is smaller and quicker).
"""
import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from episode_match import clean_summary, episode_texts, merge_episodes, similarity, title_key, word_weights, words  # noqa: E402
import name_episodes as ne  # noqa: E402

CONFIDENT_SCORE = 0.40      # share of an episode summary's telling words that are spoken in the video
CONFIDENT_MARGIN = 0.08     # how far ahead of the runner-up
VIDEO_TYPES = ('.mp4', '.mkv', '.m4v')


def episode_story_texts(episode):
    """Every wording we hold of an episode, with its title (a title often names the thing the episode is about)."""
    name = str(episode.get('name') or '')
    return [f'{name} {text}'.strip() for text in episode_texts(episode)] or ([name] if name else [])


def rank_by_transcript(transcript, episodes):
    """[(score, episode)] best first: how much of each episode's story is found in the spoken words."""
    weights = word_weights(episodes)
    spoken = ' '.join(words(transcript))
    ranked = []
    for episode in episodes:
        score = max([similarity(text, spoken, weights) for text in episode_story_texts(episode)] or [0.0])
        ranked.append((score, episode))
    return sorted(ranked, key=lambda pair: pair[0], reverse=True)


def pick_by_transcript(transcript, episodes, min_score=CONFIDENT_SCORE, margin=CONFIDENT_MARGIN):
    """(episode, score, confident, ranked)."""
    ranked = rank_by_transcript(transcript, episodes)
    if not ranked or ranked[0][0] <= 0:
        return None, 0.0, False, ranked
    best_score, best = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    return best, best_score, (best_score >= min_score and best_score - second >= margin), ranked


def plan(files, transcripts, episodes, min_score=CONFIDENT_SCORE, margin=CONFIDENT_MARGIN):
    """Decide each file.  files: [name]; transcripts: {name: text}.  Returns a list of dicts."""
    rows = []
    for name in files:
        row = {'name': name, 'status': '', 'episode': None, 'score': 0.0, 'ranked': []}
        text = transcripts.get(name, '')
        if len(words(text)) < 50:
            row['status'] = 'no speech found: check by hand'
        else:
            episode, score, confident, ranked = pick_by_transcript(text, episodes, min_score, margin)
            row['episode'], row['score'], row['ranked'] = episode, score, ranked
            row['status'] = 'ok' if confident else 'unsure'
        rows.append(row)
    ne.mark_duplicates(rows)
    return rows


def parse_clues(items):
    """["2026-11-22_0808=amy break"] -> {"2026-11-22_0808": ["amy", "break"]}"""
    clues = {}
    for item in items or []:
        stamp, _eq, text = str(item).partition('=')
        if not stamp.strip() or not text.strip():
            raise ValueError(f'Cannot read "{item}": use 2026-11-22_0808=amy')
        clues[stamp.strip()] = [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w]
    return clues


def episodes_with_clue(episodes, clue_words):
    """The episodes whose title or story mention every clue word (the start of a word counts: "break" finds "breaks")."""
    found = []
    for episode in episodes:
        text = ' '.join(episode_story_texts(episode)).lower()
        tokens = re.findall(r"[a-z0-9']+", text)
        if all(any(token.startswith(word) for token in tokens) for word in clue_words):
            found.append(episode)
    return found


def transcribe(path, model_name='small.en', model=None):
    """The words spoken in a video.  Needs faster-whisper; returns (text, model) so the model is loaded once."""
    if model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise SystemExit('This needs the speech-to-text package (%s).  Run once:   pip3 install faster-whisper'
                             % exc)
        model = WhisperModel(model_name, device='cpu', compute_type='int8')
    segments, _info = model.transcribe(path, language='en', vad_filter=True)
    return ' '.join(segment.text.strip() for segment in segments), model


def load_transcripts(folder, files, model_name):
    """Transcripts for each file, reusing ones saved by an earlier run (in the folder's .transcripts)."""
    store = os.path.join(folder, '.transcripts')
    os.makedirs(store, exist_ok=True)
    transcripts, model = {}, None
    for number, name in enumerate(files, 1):
        saved = os.path.join(store, name + '.txt')
        if os.path.isfile(saved):
            with open(saved, encoding='utf-8') as handle:
                transcripts[name] = handle.read()
            continue
        print(f'  listening to {name} ({number}/{len(files)}) ...', flush=True)
        text, model = transcribe(os.path.join(folder, name), model_name, model)
        with open(saved, 'w', encoding='utf-8') as handle:
            handle.write(text)
        transcripts[name] = text
    return transcripts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--show', required=True)
    parser.add_argument('--folder', required=True, help='the folder of episode videos to identify')
    parser.add_argument('--server', default='http://192.168.1.176:5001', help='the dashboard on the QNAP')
    parser.add_argument('--config', default='~/EPG-Manager/recording_agent.json')
    parser.add_argument('--model', default='small.en', help='speech model: small.en (better) or base.en (quicker)')
    parser.add_argument('--min-score', type=float, default=CONFIDENT_SCORE)
    parser.add_argument('--margin', type=float, default=CONFIDENT_MARGIN)
    parser.add_argument('--top', type=int, default=3, help='how many close episodes to list for an unsure video')
    parser.add_argument('--clue', action='append', default=[], metavar='STAMP=WORDS',
                        help='a clue for one video: only episodes whose plot mentions these words are ranked, e.g. '
                             '2026-11-22_0808=amy break (repeat for more videos)')
    parser.add_argument('--accept', default='', help='matches you checked by eye, e.g. 2026-11-22_0808=S02E15')
    parser.add_argument('--apply', action='store_true', help='move the confident matches into Plex')
    args = parser.parse_args(argv)

    folder = os.path.expanduser(args.folder)
    if not os.path.isdir(folder):
        print('no such folder:', folder)
        return 2
    files = sorted(n for n in os.listdir(folder) if n.lower().endswith(VIDEO_TYPES) and not n.startswith('.'))
    if not files:
        print('no videos in', folder)
        return 1
    print(f'{len(files)} video(s) in {folder}')
    year, tmdb_eps = ne.load_tmdb(args.server, args.show)
    _y, omdb_eps = ne.load_omdb(args.server, args.show)
    episodes = merge_episodes(tmdb_eps, omdb_eps)
    print(f'{len(episodes)} episodes of {args.show} to choose from.')
    transcripts = load_transcripts(folder, files, args.model)
    rows = plan(files, transcripts, episodes, args.min_score, args.margin)
    for message in ne.apply_accepted(rows, ne.parse_accept(args.accept), episodes) if args.accept else []:
        print('NOTE:', message)

    for stamp, clue_words in parse_clues(args.clue).items():
        name = next((n for n in files if f' - {stamp}.' in n), None)
        if name is None:
            print(f'NOTE: no video for {stamp}')
            continue
        pool = episodes_with_clue(episodes, clue_words)
        print(f'\nClue "{" ".join(clue_words)}" for {name}: {len(pool)} episode(s) mention it.')
        for score, cand in rank_by_transcript(transcripts[name], pool)[:max(1, args.top)]:
            print(f"   - S{int(cand['season']):02d}E{int(cand['number']):02d} {cand.get('name', '')} ({score:.2f}): "
                  f"{clean_summary(cand.get('summary'))[:120]}")
        print('   (check one by eye, then file it with --accept ' + stamp + '=S02E15 using its own number)')
    print()
    for row in rows:
        ep = row['episode']
        label = f"S{int(ep['season']):02d}E{int(ep['number']):02d} {ep.get('name', '')}" if ep else ''
        print(f"{row['status'].upper()[:9]:9}  {row['name']}  {label}  (match {row['score']:.2f})")
        if ep:
            print(f"             plot: {clean_summary(ep.get('summary'))[:160]}")
        # The runner-up shows how clear the win is; an unsure video lists the closest few to choose from.
        close = row['ranked'][1:2] if row['status'] == 'ok' else row['ranked'][:max(0, args.top)]
        for score, cand in close:
            print(f"             - S{int(cand['season']):02d}E{int(cand['number']):02d} {cand.get('name', '')} ({score:.2f}): "
                  f"{clean_summary(cand.get('summary'))[:90]}")
    ready = [r for r in rows if r['status'] == 'ok']
    print(f'\n{len(ready)} confident, {sum(r["status"] == "unsure" for r in rows)} unsure, '
          f'{sum(r["status"].startswith(("no ", "duplicate")) for r in rows)} left alone.')
    if not args.apply:
        print('\nNothing was moved.  Run again with --apply to file the confident ones in Plex.')
        return 0

    from recording_agent import load_config, resolve_show_dir
    cfg = load_config(os.path.expanduser(args.config))
    show_dir = resolve_show_dir(cfg.get('plex_tv_path', '/Volumes/Plex/TV Shows'), args.show, year)
    moved = 0
    for row in ready:
        ep = row['episode']
        dest = os.path.join(show_dir, f"Season {int(ep['season']):02d}", ne.target_name(args.show, ep))
        if os.path.exists(dest):
            print(f'SKIPPED (already exists): {dest}')
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(os.path.join(folder, row['name']), dest)
        moved += 1
        print(f'moved -> {dest}')
    print(f'\nMoved {moved} file(s).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
