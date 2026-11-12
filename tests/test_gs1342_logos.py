"""0.13.42: a team logo URL with capital letters is fetched and cached."""
import os
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import cards  # noqa: E402


def _png(w=8, h=8, rgb=b"\xff\x00\x00"):
    raw = b"".join(b"\x00" + rgb * w for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _sports(home_url, away_url):
    return {
        "v": 1, "type": "sports", "league": "NFL",
        "home": {"name": "Kansas City", "abbr": "KC", "logo_url": home_url},
        "away": {"name": "Gonzaga", "abbr": "GONZ", "logo_url": away_url},
        "home_score": 17, "away_score": 14, "status": "final", "period": "Final",
        "schedule": [{"label": "Kansas City", "when": "Sun 1pm"}],
    }


class CapitalLogoUrl(unittest.TestCase):
    def test_capital_abbr_is_fetched_as_the_lowercase_file(self):
        calls = []

        def opener(url):
            calls.append(url)
            return _png(), "image/png"

        cache = cards.LogoCache(tempfile.mkdtemp(prefix="gs1342-logo-"), ["a.espncdn.com"],
                                 opener=opener, resolve=lambda host: ["23.1.2.3"])
        card = _sports(
            "https://a.espncdn.com/i/teamlogos/nfl/500/KC.png",
            "https://a.espncdn.com/i/teamlogos/NCAA/500/GONZ.png",
        )
        out = cards.prepare_display_logos(card, cache, wait_s=1)
        self.assertEqual(calls, ["https://a.espncdn.com/i/teamlogos/nfl/500/kc.png"])
        self.assertRegex(out["home"].get("logo_id") or "", r"^[a-f0-9]{64}$")
        self.assertNotIn("logo_badge", out["home"])
        self.assertTrue(os.path.isfile(cache.path_for(out["home"]["logo_id"])))
        self.assertNotIn("logo_id", out["away"])
        self.assertIs(out["away"].get("logo_badge"), True)
        league = cards.espn_teamlogo_url("https://a.espncdn.com/i/teamlogos/NFL/500/den.png")
        self.assertTrue(league)
        self.assertFalse(cards.espn_teamlogo_url(
            "https://a.espncdn.com/combiner/i?img=/i/teamlogos/nfl/500/kc.png&w=200&h=200"))
        # Kelvin sign lowercases to "k". The path must be ASCII before that.
        self.assertEqual("\u212a".lower(), "k")
        self.assertFalse(cards.espn_teamlogo_url(
            "https://a.espncdn.com/i/teamlogos/nfl/500/\u212ac.png"))
        self.assertIn("[0-9]", cards._ESPN_TEAMLOGO.pattern)
        self.assertNotIn("\\d", cards._ESPN_TEAMLOGO.pattern)
        self.assertIn("[0-9]", cards._NCAA_PATH.pattern)
        self.assertNotIn("\\d", cards._NCAA_PATH.pattern)
        self.assertIsNotNone(cards._ESPN_TEAMLOGO.fullmatch("/i/teamlogos/nfl/500/kc.png"))
        self.assertIsNone(cards._ESPN_TEAMLOGO.fullmatch("/i/teamlogos/nfl/\u0665\u0660\u0660/kc.png"))
        self.assertIsNone(cards._NCAA_PATH.fullmatch("/i/teamlogos/ncaa/\u0665\u0660\u0660/gonz.png"))


if __name__ == "__main__":
    unittest.main()
