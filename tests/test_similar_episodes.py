import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))

import similar_episodes as se  # noqa: E402

A = "the sculpture marble huge robert living room admire sculpture marble giant statue unveils marie " * 10
A2 = A + "bye"
B = "golf tournament weekend keys garage frank hides sneaks tournament clubs course golf " * 10


class SimilarTests(unittest.TestCase):
    def test_the_same_dialogue_scores_high_and_another_episode_low(self):
        self.assertGreater(se.cosine(se.vector(A), se.vector(A2)), 0.95)
        self.assertLess(se.cosine(se.vector(A), se.vector(B)), 0.2)

    def test_transcripts_are_read_from_each_folders_dot_transcripts(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            for folder, name, text in ((one, "x - 1.mp4", A), (two, "x - 2.mp4", A2), (two, "x - 3.mp4", B)):
                os.makedirs(os.path.join(folder, ".transcripts"), exist_ok=True)
                with open(os.path.join(folder, ".transcripts", name + ".txt"), "w") as handle:
                    handle.write(text)
            found = se.load([one, two])
            near = se.closest(found)
            self.assertEqual(sorted(found), ["x - 1.mp4", "x - 2.mp4", "x - 3.mp4"])
            self.assertEqual(near["x - 1.mp4"][0][1], "x - 2.mp4")


if __name__ == "__main__":
    unittest.main()
