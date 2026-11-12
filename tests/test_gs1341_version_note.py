"""0.13.41: the release note matches the Chrome harness."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTE = os.path.join(ROOT, "docs", "versioning.md")


class ChromeNote(unittest.TestCase):
    def test_version_note_matches_the_harness(self):
        with open(NOTE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("## 0.13.41", text)
        note = text.split("## 0.13.41", 1)[1].split("\n## ", 1)[0]
        self.assertIn("no `--user-data-dir` is passed", note)
        self.assertIn("TMPDIR", note)
        self.assertIn("Bots menu page still uses `--virtual-time-budget`", note)
        self.assertIn("fonts and animations are stripped", note)
        self.assertIn("home cards are hidden at night even with no updates", note)
        self.assertNotIn("does not use `--virtual-time-budget`", note)
        self.assertNotIn("own `--user-data-dir`", note)


if __name__ == "__main__":
    unittest.main()
