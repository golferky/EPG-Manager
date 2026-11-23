import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mac"))

import identify_episodes as ie  # noqa: E402

EPISODES = [
    {"season": 1, "number": 1, "name": "Pilot", "summary": "Ray forgets the anniversary dinner and Debra sulks while his parents argue about lasagna."},
    {"season": 2, "number": 5, "name": "The Sculpture", "summary": "Marie unveils a giant marble sculpture of Robert in the living room and everyone pretends to admire it."},
    {"season": 3, "number": 9, "name": "Golf Weekend", "summary": "Ray sneaks away for a golf tournament while Frank hides the car keys in the garage."},
]
SPEECH_SCULPTURE = ("so marie brought over the sculpture and it is huge marble robert looks like a giant in the living room "
                    "everyone says it is beautiful and admire it but nobody wants it in the house ") * 6
SPEECH_NOTHING = "okay well see you later bye " * 3


class IdentifyTests(unittest.TestCase):
    def test_the_episode_whose_story_is_spoken_ranks_first(self):
        ranked = ie.rank_by_transcript(SPEECH_SCULPTURE, EPISODES)
        self.assertEqual(ranked[0][1]["name"], "The Sculpture")
        self.assertGreater(ranked[0][0], ranked[1][0])

    def test_a_clear_match_is_confident_and_a_weak_one_is_not(self):
        episode, score, confident, _ranked = ie.pick_by_transcript(SPEECH_SCULPTURE, EPISODES)
        self.assertEqual(episode["number"], 5)
        self.assertTrue(confident)
        vague = "ray debra marie frank went home and talked about things " * 8
        _e, _s, vague_confident, _r = ie.pick_by_transcript(vague, EPISODES)
        self.assertFalse(vague_confident)

    def test_plan_files_the_sure_ones_and_leaves_the_rest(self):
        rows = ie.plan(["a - 2026-11-22_0808.mp4", "b - 2026-11-22_0829.mp4"],
                       {"a - 2026-11-22_0808.mp4": SPEECH_SCULPTURE, "b - 2026-11-22_0829.mp4": SPEECH_NOTHING}, EPISODES)
        by = {r["name"]: r for r in rows}
        self.assertEqual(by["a - 2026-11-22_0808.mp4"]["status"], "ok")
        self.assertTrue(by["b - 2026-11-22_0829.mp4"]["status"].startswith("no speech"))

    def test_two_videos_that_match_the_same_episode_are_both_held_back(self):
        rows = ie.plan(["a - 2026-11-22_0808.mp4", "b - 2026-11-22_0829.mp4"],
                       {"a - 2026-11-22_0808.mp4": SPEECH_SCULPTURE, "b - 2026-11-22_0829.mp4": SPEECH_SCULPTURE}, EPISODES)
        self.assertTrue(all(r["status"].startswith("duplicate") for r in rows))


class ClueTests(unittest.TestCase):
    def test_a_clue_is_read_as_words_for_one_video(self):
        self.assertEqual(ie.parse_clues(["2026-11-22_0808=Amy break"]), {"2026-11-22_0808": ["amy", "break"]})
        with self.assertRaises(ValueError):
            ie.parse_clues(["nonsense"])

    def test_only_episodes_that_mention_every_clue_word_are_kept(self):
        episodes = EPISODES + [{"season": 6, "number": 2, "name": "Split", "summary": "Robert and Amy break up after a fight."}]
        found = ie.episodes_with_clue(episodes, ["amy", "break"])
        self.assertEqual([e["name"] for e in found], ["Split"])
        self.assertEqual(ie.episodes_with_clue(episodes, ["marble"])[0]["name"], "The Sculpture")

class CloseCallTests(unittest.TestCase):
    def test_a_win_of_less_than_the_margin_is_held_not_filed(self):
        # Nov 23: "Be Nice" scored 1.00 against 0.85 for the real episode, and was filed wrongly.
        self.assertGreaterEqual(ie.CONFIDENT_MARGIN, 0.20)
        episodes = [{"season": 3, "number": 23, "name": "Be Nice", "summary": "Ray and Debra vow to be nicer to each other."},
                    {"season": 2, "number": 6, "name": "Anniversary", "summary": "A surprise party for Frank and Marie's anniversary."}]
        _ep, _score, confident, _ranked = ie.pick_by_transcript(
            "ray and debra be nicer each other vow surprise party frank marie anniversary", episodes)
        self.assertFalse(confident)

class CommonSpeechWordsTests(unittest.TestCase):
    EPISODES = [{"season": 3, "number": 23, "name": "Be Nice",
                 "summary": "Ray and Debra vow to be nicer to each other and to change their ways."},
                {"season": 2, "number": 6, "name": "Anniversary",
                 "summary": "A surprise party for Frank and Marie's anniversary, and Ray is shocked."},
                {"season": 1, "number": 1, "name": "Pilot", "summary": "Ray and Debra host Marie and Frank for dinner."}]
    COMMON = "ray debra nicer change ways each other vow be to and "

    def test_words_every_transcript_says_no_longer_make_a_generic_plot_win(self):
        party = self.COMMON + "surprise party frank marie anniversary shocked"
        others = [self.COMMON + "dinner", self.COMMON + "monsters drawing", self.COMMON + "radio psychologist"]
        plain = ie.pick_by_transcript(party, self.EPISODES)
        aware = ie.pick_by_transcript(party, self.EPISODES, background=others)
        self.assertEqual(plain[0]["name"], "Be Nice")                 # recall alone is fooled
        self.assertEqual(aware[0]["name"], "Anniversary")             # common speech is discounted
        self.assertGreater(aware[3][0][0] - aware[3][1][0], plain[3][0][0] - plain[3][1][0] - 1)

    def test_a_batch_of_files_is_each_others_background(self):
        transcripts = {"a.mp4": (self.COMMON + "surprise party frank marie anniversary shocked ") * 6,
                       "b.mp4": (self.COMMON + "dinner ") * 6, "c.mp4": (self.COMMON + "radio psychologist ") * 6}
        rows = ie.plan(["a.mp4", "b.mp4", "c.mp4"], transcripts, self.EPISODES)
        self.assertEqual(rows[0]["episode"]["name"], "Anniversary")


if __name__ == "__main__":
    unittest.main()