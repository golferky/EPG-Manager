"""Match a guide's one-line description to the episode of a series it describes.

Used by the dashboard (to show the probable episode in the programme window) and by the Mac tool
``mac/name_episodes.py``.  Pure functions, no network: the caller supplies the episodes.  An episode is a dict with
``season``, ``number``, ``name`` and ``summary`` (and optionally ``extra_summaries``, other wordings of the same story).
"""
import html
import math
import re

STOP_WORDS = set('a an and the of to in on at for with his her their he she it is are was be by as that this '
                 'from but or who when while after before into out up down over'.split())
CONFIDENT_SCORE = 0.5       # share of the plot line's telling words found in the episode summary
CONFIDENT_MARGIN = 0.15     # how much better than the second-best episode


def words(text):
    found = re.findall(r"[a-z0-9']+", str(text or '').lower())
    return [w.strip("'") for w in found if w.strip("'") not in STOP_WORDS and len(w.strip("'")) > 1]


def clean_summary(text):
    return html.unescape(re.sub(r'<[^>]+>', ' ', str(text or ''))).strip()


_IDF_CACHE = {}


def episode_texts(episode):
    """Every wording we hold of an episode's story (TMDb's summary, plus OMDb's plot when merged)."""
    texts = [clean_summary(episode.get('summary'))] + [clean_summary(t) for t in episode.get('extra_summaries', [])]
    return [t for t in texts if t]


def word_weights(episodes):
    """How telling each word is: one that is in every summary ("Tony", "Jeannie") counts for little."""
    key = id(episodes)
    if key in _IDF_CACHE and _IDF_CACHE[key][0] is episodes:
        return _IDF_CACHE[key][1]
    docs = [set(words(' '.join(episode_texts(ep)))) for ep in episodes]
    total = len(docs) or 1
    frequency = {}
    for doc in docs:
        for word in doc:
            frequency[word] = frequency.get(word, 0) + 1
    weights = {word: math.log((total + 1) / (count + 1)) + 0.1 for word, count in frequency.items()}
    _IDF_CACHE[key] = (episodes, weights)
    return weights


def similarity(guide_text, summary, weights=None):
    """0 to 1: how much of a short guide plot line is found in an episode summary, rare words counting most."""
    guide_words, summary_words = set(words(guide_text)), set(words(summary))
    if not guide_words or not summary_words:
        return 0.0
    weights = weights or {}
    unseen = max(weights.values()) if weights else 1.0       # a word no summary has can only be unmatched
    def weight(word):
        return weights.get(word, unseen)
    return sum(weight(w) for w in guide_words & summary_words) / sum(weight(w) for w in guide_words)


def rank_episodes(guide_text, episodes):
    """[(score, episode)] best first.  An episode scores by its best-matching wording."""
    weights = word_weights(episodes)
    scored = [(max([similarity(guide_text, text, weights) for text in episode_texts(ep)] or [0.0]), ep) for ep in episodes]
    return sorted(scored, key=lambda pair: pair[0], reverse=True)


def title_key(name):
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def merge_episodes(base, extra):
    """The base list (the numbering we trust) with each episode's OMDb plot added as a second wording.

    An OMDb episode is matched by its title, or failing that by the same season and number.
    """
    by_title = {title_key(e.get('name')): e for e in extra if title_key(e.get('name'))}
    by_number = {(e.get('season'), e.get('number')): e for e in extra}
    merged = []
    for episode in base:
        other = by_title.get(title_key(episode.get('name'))) or by_number.get((episode.get('season'), episode.get('number')))
        copy = dict(episode)
        copy['extra_summaries'] = [other['summary']] if other and other.get('summary') else []
        merged.append(copy)
    return merged


def pick(guide_text, episodes, min_score=None, margin=None):
    """(episode, score, confident) for a plot line."""
    min_score = CONFIDENT_SCORE if min_score is None else min_score
    margin = CONFIDENT_MARGIN if margin is None else margin
    ranked = rank_episodes(guide_text, episodes)
    if not ranked or ranked[0][0] <= 0:
        return None, 0.0, False
    best_score, best = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    return best, best_score, (best_score >= min_score and best_score - second >= margin)


def top_candidates(plot, episodes, count=3):
    """The closest few episodes to a plot line: [(score, episode)], best first."""
    return rank_episodes(plot, episodes)[:max(0, count)]
