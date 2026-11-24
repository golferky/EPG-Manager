import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mac"))

import ask_claude_episode as ac  # noqa: E402

EPISODES = [{"season": 2, "number": 13, "name": "Civil War", "summary": "Ray volunteers for a Civil War re-enactment."},
            {"season": 1, "number": 1, "name": "Pilot", "summary": "Dinner."}]


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def reply_with(text):
    return lambda request, timeout=0: Reply(json.dumps({"content": [{"type": "text", "text": text}]}).encode())


class AskTests(unittest.TestCase):
    def test_the_prompt_lists_every_episode_and_the_spoken_words(self):
        text = ac.prompt("Show", EPISODES, "we march in the reenactment")
        self.assertIn("S02E13 | Civil War | Ray volunteers", text)
        self.assertIn("we march in the reenactment", text)

    def test_the_answer_is_read_even_with_words_around_it(self):
        answer = ac.ask("k", "m", "Show", EPISODES, "x",
                        post=reply_with('Here: {"season": 2, "episode": 13, "confidence": "high", "evidence": "re-enactment"}'))
        self.assertEqual(ac.code(answer), "S02E13")

    def test_a_failed_call_is_reported_not_raised(self):
        def broken(request, timeout=0):
            raise OSError("down")
        self.assertIn("error", ac.ask("k", "m", "Show", EPISODES, "x", post=broken))
        self.assertIn("error", ac.ask("k", "m", "Show", EPISODES, "x", post=reply_with("no json here")))

    def test_the_key_is_sent_as_a_header_and_never_in_the_body(self):
        seen = {}

        def capture(request, timeout=0):
            seen["headers"] = dict(request.header_items())
            seen["body"] = request.data.decode()
            return reply_with('{"season": 1, "episode": 1}')(request)
        ac.ask("secret-key", "m", "Show", EPISODES, "x", post=capture)
        self.assertEqual(seen["headers"]["X-api-key"], "secret-key")
        self.assertNotIn("secret-key", seen["body"])

    def test_the_claude_command_is_asked_through_stdin_and_its_json_is_read(self):
        seen = {}

        class Done:
            returncode, stderr = 0, ""
            stdout = 'Sure. {"season": 2, "episode": 13, "confidence": "high", "evidence": "re-enactment"}'

        def run(command, **kwargs):
            seen["command"], seen["input"] = command, kwargs["input"]
            return Done()
        answer = ac.ask_cli("sonnet", "Show", EPISODES, "we march", run=run)
        self.assertEqual(ac.code(answer), "S02E13")
        self.assertEqual(seen["command"][:2], ["claude", "-p"])
        self.assertIn("we march", seen["input"])

    def test_a_failing_claude_command_is_reported(self):
        class Done:
            returncode, stderr, stdout = 1, "not signed in", ""
        self.assertIn("not signed in", ac.ask_cli("sonnet", "Show", EPISODES, "x", run=lambda *a, **k: Done())["error"])

    def test_stamps_are_read_from_file_names(self):
        self.assertEqual(ac.stamp_of("Everybody Loves Raymond - 2026-11-23_0910 (2).mp4"), "2026-11-23_0910")


if __name__ == "__main__":
    unittest.main()
