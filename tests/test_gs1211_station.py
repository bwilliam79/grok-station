#!/usr/bin/env python3
"""0.12.11 station: softer thinking tone (classic kept), score tile shows at once with logos filling in,
college logo URLs, 15 s score tile, and the corner badge returns to idle. No hardware, no network."""
import array, os, sys, tempfile, threading, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import cards  # noqa: E402
import sounds  # noqa: E402
from test_cards import tiny_png  # noqa: E402

SRC = open(os.path.join(ROOT, "app", "grok_station.py"), encoding="utf-8").read()


def _peak(pcm):
    a = array.array("h"); a.frombytes(pcm)
    return max(abs(x) for x in a), len(a)


class ThinkingTone(unittest.TestCase):
    def test_default_is_one_soft_low_pulse(self):
        soft_peak, soft_n = _peak(sounds.wait_tone_pcm(0.10))
        old_peak, _ = _peak(sounds.wait_tone_pcm(0.10, "classic"))
        self.assertLess(soft_peak, old_peak * 0.5)
        self.assertLessEqual(soft_n / sounds.WAIT_TONE_SR, 0.5)
        self.assertLess(sounds.WAIT_TONE_SOFT_FREQ, 392.0)
        self.assertGreaterEqual(sounds.wait_tone_every("soft"), 5.0)

    def test_classic_chime_still_available(self):
        self.assertEqual(sounds.wait_tone_every("classic"), 2.5)
        self.assertEqual(sounds.wait_tone_style("nonsense"), "soft")
        self.assertEqual(sounds.wait_tone_style("classic"), "classic")


class _SlowOpener:
    def __init__(self, delay):
        self.delay, self.calls = delay, []

    def __call__(self, url):
        self.calls.append(url)
        time.sleep(self.delay)
        return tiny_png(), "image/png"


def _card(home_url, away_url):
    return {"type": "sports", "v": 1, "league": "NCAAM", "home": {"name": "Arkansas", "abbr": "ARK", "logo_url": home_url},
            "away": {"name": "Gonzaga", "abbr": "GONZ", "logo_url": away_url}, "home_score": 70, "away_score": 68,
            "status": "live", "period": "2nd",
            "schedule": [{"label": "Arkansas at Gonzaga", "when": "Sat 7pm"}]}


class ScoreTileLogos(unittest.TestCase):
    def test_tile_does_not_wait_for_logos_and_fills_in(self):
        root = tempfile.mkdtemp()
        cache = cards.LogoCache(root, ["a.espncdn.com"], opener=_SlowOpener(0.6), resolve=lambda h: True)
        landed = threading.Event()
        t0 = time.monotonic()
        out = cards.prepare_display_logos(_card("https://a.espncdn.com/i/teamlogos/ncaa/500/8.png",
                                                "https://a.espncdn.com/i/teamlogos/ncaa/500/2250.png"),
                                          cache, on_ready=landed.set)
        self.assertLess(time.monotonic() - t0, 0.3)  # shown now, not after the download
        self.assertTrue(out["away"].get("logo_badge"))  # initials until the logo lands
        self.assertTrue(landed.wait(3))
        end = time.monotonic() + 3
        while time.monotonic() < end and not os.path.isfile(cache.path_for(out["away"]["logo_id"])):
            time.sleep(0.05)  # each side calls on_ready when its own file lands
        self.assertTrue(os.path.isfile(cache.path_for(out["away"]["logo_id"])))

    def test_college_abbreviation_url_falls_back_to_initials(self):
        self.assertTrue(cards.logo_known_missing("https://a.espncdn.com/i/teamlogos/ncaa/500/gonz.png"))
        self.assertFalse(cards.logo_known_missing("https://a.espncdn.com/i/teamlogos/ncaa/500/2250.png"))
        self.assertFalse(cards.logo_known_missing("https://a.espncdn.com/i/teamlogos/nfl/500/kc.png"))
        cache = cards.LogoCache(tempfile.mkdtemp(), ["a.espncdn.com"], opener=_SlowOpener(0), resolve=lambda h: True)
        out = cards.prepare_display_logos(_card("https://a.espncdn.com/i/teamlogos/ncaa/500/ark.png",
                                                "https://a.espncdn.com/i/teamlogos/ncaa/500/gonz.png"), cache)
        self.assertTrue(out["home"]["logo_badge"] and out["away"]["logo_badge"])
        self.assertEqual(cache.opener.calls, [])

    def test_big_college_logo_fits_the_cache(self):
        self.assertGreaterEqual(cards.LOGO_MAX, 110 * 1024)  # 500 px college logos run to ~105 KiB


class ScoreTileTimeout(unittest.TestCase):
    def test_score_tile_leaves_15_s_after_it_showed(self):
        self.assertEqual(cards.SPORTS_TILE_S, 15)
        self.assertEqual(cards.sports_idle_span(100.0, 105.0), 10.0)
        self.assertEqual(cards.sports_idle_span(100.0, 130.0), 0.0)
        self.assertIn("sports_idle=(cards.sports_idle_span(", SRC)


class BadgeReturnsToIdle(unittest.TestCase):
    def test_turn_over_clears_listening(self):
        import grok_station as gs
        out = gs.turn_over({"from": "Rhea", "voice": True, "state": "listening", "text": "x"})
        self.assertNotIn(out.get("state"), gs.BADGE_STATES)
        self.assertEqual(out["from"], "Rhea")

    def test_idle_with_a_tile_uses_it(self):
        i = SRC.index("if msg and TILE:\n                    now_s = time.time()")
        self.assertIn("msg = turn_over(msg)", SRC[i:i + 300])


if __name__ == "__main__":
    unittest.main()
