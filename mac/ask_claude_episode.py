#!/usr/bin/env python3
"""Ask Claude which episode a video is, from what is said in it.  Guesses only: nothing is filed or moved.

Run on the Mac mini.  It asks Claude in one of two ways:
  * the Claude Code command-line tool (``claude``), signed in once with your Claude account: no key to keep (default when
    ``claude`` is installed and no key is set), or
  * the Anthropic API with a key in the environment (never typed into chat or a command):
        export ANTHROPIC_API_KEY=...      (set once in ~/.zshrc, or for one window)

    # How good is it?  Test on episodes you already confirmed (it prints right or wrong for each):
    python3 ~/EPG-Manager/mac/ask_claude_episode.py --show "Everybody Loves Raymond" \
        --folder "/Volumes/Plex/TV Needing Episode Info/Everybody Loves Raymond" --folder ~/raymond_recovered \
        --truth "2026-11-23_0847=S01E21,2026-11-23_0910=S01E22"

    # Guess some videos (their stamps are in the file names):
    python3 ~/EPG-Manager/mac/ask_claude_episode.py --show "Everybody Loves Raymond" --folder DIR --only 2026-11-23_1417

What leaves the Mac: the transcript of the chosen video and the list of episode titles and plot summaries, sent to the
Anthropic API.  The transcripts come from each folder's .transcripts (made by identify_episodes.py or the recorder).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from episode_match import clean_summary, merge_episodes  # noqa: E402
import name_episodes as ne  # noqa: E402
import similar_episodes as se  # noqa: E402

API = 'https://api.anthropic.com/v1/messages'
DEFAULT_MODEL = 'claude-sonnet-5-5'
MAX_WORDS = 5000            # of a transcript; a 22 minute episode is about 3500 words

SYSTEM = ("You identify which episode of a TV show a transcript comes from. The transcript is automatic speech "
          "recognition of one episode, so names and words may be misspelled. You are given every episode's number, title "
          "and plot summary. Plot summaries are short and several episodes share themes, so decide from specific events, "
          "named guest characters and running jokes in the transcript, not general topics. If the transcript does not "
          "contain enough to be sure, say so. Reply with ONLY a JSON object.")


def episode_list(episodes):
    lines = []
    for ep in sorted(episodes, key=lambda e: (int(e['season']), int(e['number']))):
        texts = [clean_summary(ep.get('summary'))] + [clean_summary(t) for t in ep.get('extra_summaries', [])]
        plot = max((t for t in texts if t), key=len, default='')
        lines.append('S%02dE%02d | %s | %s' % (int(ep['season']), int(ep['number']), ep.get('name', ''), plot[:400]))
    return '\n'.join(lines)


def prompt(show, episodes, transcript):
    spoken = ' '.join(transcript.split()[:MAX_WORDS])
    return ('Show: %s\n\nEpisodes:\n%s\n\nTranscript of the video:\n%s\n\n'
            'Which episode is it? Reply with JSON: {"season": N, "episode": N, "confidence": "high" | "medium" | "low", '
            '"evidence": "the specific events or lines in the transcript that show it", '
            '"alternative": "SxxEyy of the next most likely episode, or empty"}'
            % (show, episode_list(episodes), spoken))


def ask(key, model, show, episodes, transcript, post=None):
    """The parsed JSON answer, or a dict with 'error'."""
    body = json.dumps({'model': model, 'max_tokens': 500, 'system': SYSTEM,
                       'messages': [{'role': 'user', 'content': prompt(show, episodes, transcript)}]}).encode()
    request = urllib.request.Request(API, body, {'x-api-key': key, 'anthropic-version': '2023-06-01',
                                                'content-type': 'application/json'})
    try:
        with (post or urllib.request.urlopen)(request, timeout=120) as reply:
            data = json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        return {'error': 'the API answered %s' % exc.code}
    except Exception as exc:
        return {'error': 'could not reach the API (%s)' % exc.__class__.__name__}
    text = ''.join(block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text')
    found = re.search(r'\{.*\}', text, flags=re.S)
    if not found:
        return {'error': 'no JSON in the answer'}
    try:
        return json.loads(found.group(0))
    except ValueError:
        return {'error': 'the answer was not valid JSON'}


def ask_cli(model, show, episodes, transcript, run=None):
    """The same question through the Claude Code command line (``claude -p``), which uses the signed-in account."""
    text = SYSTEM + '\n\n' + prompt(show, episodes, transcript)
    try:
        done = (run or subprocess.run)(['claude', '-p', '--model', model], input=text, capture_output=True,
                                       text=True, timeout=300)
    except Exception as exc:
        return {'error': 'could not run claude (%s)' % exc.__class__.__name__}
    if done.returncode != 0:
        return {'error': 'claude stopped with code %s: %s' % (done.returncode, (done.stderr or '').strip()[:200])}
    found = re.search(r'\{.*\}', done.stdout or '', flags=re.S)
    if not found:
        return {'error': 'no JSON in the answer'}
    try:
        return json.loads(found.group(0))
    except ValueError:
        return {'error': 'the answer was not valid JSON'}


def code(answer):
    try:
        return 'S%02dE%02d' % (int(answer['season']), int(answer['episode']))
    except (KeyError, TypeError, ValueError):
        return ''


def stamp_of(name):
    found = re.search(r'(\d{4}-\d{2}-\d{2}_\d{4})(?: \(\d+\))?\.mp4$', name)
    return found.group(1) if found else ''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--show', required=True)
    parser.add_argument('--folder', action='append', required=True)
    parser.add_argument('--server', default='http://192.168.1.176:5001')
    parser.add_argument('--model', default='')
    parser.add_argument('--via', choices=['auto', 'cli', 'api'], default='auto',
                        help='cli = the claude command (signed-in account), api = ANTHROPIC_API_KEY')
    parser.add_argument('--truth', default='', help='confirmed ones to test against: 2026-11-23_0847=S01E21,...')
    parser.add_argument('--only', action='append', default=[], help='a stamp to guess (repeat)')
    args = parser.parse_args(argv)
    key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    via = args.via
    if via == 'auto':
        via = 'cli' if shutil.which('claude') else ('api' if key else '')
    if via == 'api' and not key:
        print('Set ANTHROPIC_API_KEY first (export ANTHROPIC_API_KEY=... in the Terminal window, typed by you).')
        return 2
    if via == 'cli' and not shutil.which('claude'):
        print('The claude command is not installed on this Mac (see the notes at the top of this file).')
        return 2
    if not via:
        print('Neither the claude command nor ANTHROPIC_API_KEY is available.')
        return 2
    model = args.model or ('sonnet' if via == 'cli' else DEFAULT_MODEL)
    truth = {}
    for part in [p for p in args.truth.split(',') if p.strip()]:
        stamp, _eq, wanted = part.strip().partition('=')
        truth[stamp.strip()] = wanted.strip().upper()
    transcripts = se.load(args.folder)
    wanted_stamps = set(args.only) | set(truth)
    chosen = {n: t for n, t in transcripts.items() if stamp_of(n) in wanted_stamps}
    if not chosen:
        print('no transcripts found for those stamps in', args.folder)
        return 1
    _year, tmdb = ne.load_tmdb(args.server, args.show)
    try:
        _y, omdb = ne.load_omdb(args.server, args.show, max_minutes=5)
    except Exception:
        omdb = []
    episodes = merge_episodes(tmdb, omdb)
    right = wrong = 0
    for name, text in sorted(chosen.items()):
        stamp = stamp_of(name)
        answer = (ask_cli(model, args.show, episodes, text) if via == 'cli'
                  else ask(key, model, args.show, episodes, text))
        if 'error' in answer:
            print('%s: %s' % (stamp, answer['error']))
            continue
        guess = code(answer)
        verdict = ''
        if stamp in truth:
            if guess == truth[stamp]:
                right += 1
                verdict = '  RIGHT'
            else:
                wrong += 1
                verdict = '  WRONG (it is %s)' % truth[stamp]
        print('%s: %s (%s)%s' % (stamp, guess or '?', answer.get('confidence', '?'), verdict))
        print('    %s' % str(answer.get('evidence', ''))[:300])
        if answer.get('alternative'):
            print('    alternative: %s' % answer['alternative'])
    if truth:
        print('\n%d right, %d wrong out of %d tested.' % (right, wrong, right + wrong))
    return 0


if __name__ == '__main__':
    sys.exit(main())
