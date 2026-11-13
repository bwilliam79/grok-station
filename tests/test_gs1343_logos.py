"""0.13.43: a college abbreviation logo is skipped with one log line, and the TEAM line names a real id."""
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "admin"))
import cards  # noqa: E402


def _team_block(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    start = text.index("  TEAM:")
    end = text.index("\ncalendar:", start)
    return text[start:end]


class CollegeLogoSkip(unittest.TestCase):
    def test_abbreviation_logs_one_line_without_the_url(self):
        lines = []
        cache = cards.LogoCache(tempfile.mkdtemp(), ["a.espncdn.com"], log=lines.append, resolve=lambda host: True)
        card = {
            "type": "sports", "v": 1, "league": "NCAAM",
            "home": {"name": "Arkansas", "abbr": "ARK",
                     "logo_url": "https://a.espncdn.com/i/teamlogos/ncaa/500/ARK.png"},
            "away": {"name": "Gonzaga", "abbr": "GONZ"},
            "home_score": 70, "away_score": 68, "status": "final", "period": "Final", "schedule": [],
        }
        out = cards.prepare_display_logos(card, cache, wait_s=0)
        skipped = [line for line in lines if line.startswith("logo skipped")]
        self.assertEqual(skipped, ["logo skipped: college logo needs ESPN team id"])
        self.assertNotIn("http", skipped[0])
        self.assertNotIn("ARK", skipped[0])
        self.assertNotIn("/", skipped[0])
        self.assertTrue(out["home"].get("logo_badge"))
        self.assertNotIn("logo_id", out["home"])

    def test_other_refusals_name_the_reason(self):
        lines = []
        cache = cards.LogoCache(tempfile.mkdtemp(), [], log=lines.append, resolve=lambda host: True)
        card = {
            "type": "sports", "v": 1, "league": "NFL",
            "home": {"name": "Kansas City", "abbr": "KC",
                     "logo_url": "https://a.espncdn.com/i/teamlogos/nfl/500/kc.png"},
            "away": {"name": "Denver", "abbr": "DEN",
                     "logo_url": "https://a.espncdn.com/combiner/i?img=/i/teamlogos/nfl/500/den.png"},
            "home_score": 1, "away_score": 0, "status": "final", "period": "Final", "schedule": [],
        }
        cards.prepare_display_logos(card, cache, wait_s=0)
        self.assertEqual(lines, [
            "logo skipped: host not allowed",
            "logo skipped: not a team-logo path",
        ])
        for line in lines:
            self.assertNotIn("http", line)
            self.assertNotIn("espncdn", line)


class TeamInstruction(unittest.TestCase):
    def test_ncaa_team_line_is_a_numeric_espn_logo(self):
        home = _team_block(os.path.join(ROOT, "admin", "routine-home.txt"))
        routine = _team_block(os.path.join(ROOT, "admin", "gs_routine.py"))
        self.assertEqual(home, routine)
        self.assertIn("omit logo_url", home)
        found = re.search(r"https://a\.espncdn\.com/i/teamlogos/ncaa/500/\d+\.png", home)
        self.assertIsNotNone(found, home)
        url = found.group(0)
        self.assertTrue(cards.espn_teamlogo_url(url))
        self.assertFalse(cards.logo_known_missing(url))
        college = home.split("College teams", 1)[1]
        self.assertNotIn("<abbr>", college)
        self.assertIn("ncaa path", college)
        self.assertNotIn("mens-college-basketball", college)
        self.assertIn("/500/<abbr>.png", home.split("College teams", 1)[0])


if __name__ == "__main__":
    unittest.main()
