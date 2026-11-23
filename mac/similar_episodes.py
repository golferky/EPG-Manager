#!/usr/bin/env python3
"""Which videos are the same episode?  Compares what is SAID in each, not the plot summaries.

Run on the Mac mini (reads the transcripts that identify_episodes.py and the recorder keep in ``<folder>/.transcripts``):
    python3 ~/EPG-Manager/mac/similar_episodes.py --folder "/Volumes/Plex/TV Needing Episode Info/Everybody Loves Raymond" --folder ~/raymond_recovered

Two copies of the same episode share nearly all their dialogue, so their match is high (0.8 or more); different episodes of
the same show score about 0.3 to 0.5.  For each video it prints the closest other videos.  Reads and prints only.
"""
import argparse
import math
import os
import re
import sys

STOP = set('a an and the of to in on at for with his her their he she it is are was be by as that this from but or who '
           'when while after before into out up down over i you we they me my your our not no yes so do did does what '
           'there then than if just oh well okay yeah hey know like get got go going want right now have has had can '
           'will would could should one all about here how why us him them its dont thats im ill ive youre youve'.split())


def words(text):
    return [w.strip("'") for w in re.findall(r"[a-z0-9']+", str(text or '').lower()) if w.strip("'") not in STOP and len(w) > 2]


def vector(text):
    counts = {}
    for word in words(text):
        counts[word] = counts.get(word, 0) + 1
    return counts


def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(count * b.get(word, 0) for word, count in a.items())
    norm = math.sqrt(sum(c * c for c in a.values())) * math.sqrt(sum(c * c for c in b.values()))
    return dot / norm if norm else 0.0


def load(folders):
    """{video name: transcript} from each folder's .transcripts (file ``<video name>.txt``)."""
    found = {}
    for folder in folders:
        store = os.path.join(os.path.expanduser(folder), '.transcripts')
        if not os.path.isdir(store):
            continue
        for name in sorted(os.listdir(store)):
            if name.endswith('.txt'):
                with open(os.path.join(store, name), encoding='utf-8') as handle:
                    found[name[:-4]] = handle.read()
    return found


def closest(transcripts, top=3):
    """{name: [(score, other name)]} best first."""
    vectors = {name: vector(text) for name, text in transcripts.items()}
    result = {}
    for name, vec in vectors.items():
        scored = [(cosine(vec, other), o) for o, other in vectors.items() if o != name]
        result[name] = sorted(scored, reverse=True)[:top]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--folder', action='append', required=True, help='a folder with a .transcripts (repeat)')
    parser.add_argument('--top', type=int, default=3)
    args = parser.parse_args(argv)
    transcripts = load(args.folder)
    if len(transcripts) < 2:
        print('need at least two transcripts; found', len(transcripts))
        return 1
    for name, near in closest(transcripts, args.top).items():
        short = name.replace('Everybody Loves Raymond - ', '')
        print(short)
        for score, other in near:
            tag = '  SAME EPISODE' if score >= 0.8 else ''
            print('    %.2f  %s%s' % (score, other.replace('Everybody Loves Raymond - ', ''), tag))
    return 0


if __name__ == '__main__':
    sys.exit(main())
