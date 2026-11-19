"""0.13.46: a college side with no logo_url is resolved from the ESPN teams list.

The list is a recorded fixture. These tests do not open a socket.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import cards  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "espn-ncaaf-teams.json")
UCA = "https://a.espncdn.com/i/teamlogos/ncaa/500/2110.png"


def _png():
    """A 1x1 PNG the logo cache will store. No network."""
    import struct
    import zlib
    raw = b"\x00\x00\x00\x00"

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _card(home, away, league="NCAAF", home_url=None, away_url=None):
    def team(name, abbr, url):
        row = {"name": name, "abbr": abbr}
        if url is not None:
            row["logo_url"] = url
        return row
    return {
        "v": 1, "type": "sports", "league": league,
        "home": team(*home, home_url) if False else team(home[0], home[1], home_url),
        "away": team(away[0], away[1], away_url),
        "home_score": 10, "away_score": 7, "status": "final", "period": "Final",
        "schedule": [],
    }


class ResolveCollegeLogo(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE, "rb") as fh:
            self.raw = fh.read()
        self.calls = []
        self.root = tempfile.mkdtemp(prefix="gs1346-logo-")
        self.lines = []
        self.cache = cards.LogoCache(
            self.root, ["a.espncdn.com"], opener=lambda url: (_png(), "image/png"),
            resolve=lambda host: True, log=self.lines.append)
        self.cache.team_opener = self._open

    def _open(self, url):
        self.calls.append(url)
        self.assertNotIn("espncdn.com/i/", url)
        self.assertTrue(url.startswith("https://site.api.espn.com/"))
        return self.raw

    def _prepare(self, card):
        return cards.prepare_display_logos(card, self.cache, wait_s=0)

    def test_name_alias_and_abbreviation_resolve_to_2110(self):
        # The recorded list has no "UCA" alias. shortDisplayName and nickname are
        # the alias fields it does carry ("C Arkansas").
        doc = json.loads(self.raw)
        index = cards.build_team_index(doc, "ncaa", True)
        self.assertEqual(index["central arkansas"], UCA)
        self.assertEqual(index["c arkansas"], UCA)
        self.assertEqual(index["cark"], UCA)
        self.assertNotIn("bears", index)  # fourteen schools share that name
        self.assertNotIn("uca", index)
        for name, abbr in (("Central Arkansas", "CARK"), ("C Arkansas", "CARK"), ("cark", "CARK")):
            card = self._prepare(_card((name, abbr), ("Arkansas", "ARK")))
            self.assertEqual(card["home"].get("logo_id"), cards.logo_id_of(UCA), name)
            self.assertNotIn("logo_url", card["home"])
            self.assertNotIn("logo none: home", self.lines)
        self.assertTrue(self.calls)
        for line in self.lines:
            self.assertNotIn("http", line)
            self.assertNotIn("espn", line)

    def test_unknown_team_falls_back_to_the_abbreviation(self):
        card = self._prepare(_card(("Central Arkansas", "CARK"), ("Atlantis", "ZZZ")))
        self.assertEqual(card["home"].get("logo_id"), cards.logo_id_of(UCA))
        self.assertTrue(card["away"].get("logo_badge"))
        self.assertNotIn("logo_id", card["away"])
        self.assertIn("logo none: away", self.lines)
        self.assertNotIn("logo none: home", self.lines)

    def test_an_explicit_logo_url_wins(self):
        explicit = "https://a.espncdn.com/i/teamlogos/ncaa/500/8.png"
        card = self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK"), home_url=explicit))
        self.assertEqual(card["home"].get("logo_id"), cards.logo_id_of(explicit))
        self.assertNotEqual(card["home"].get("logo_id"), cards.logo_id_of(UCA))

    def test_a_fresh_cache_is_used_offline(self):
        path = cards._teams_path(self.root, "football", "college-football")
        os.makedirs(self.root, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(self.raw)
        os.utime(path, None)
        card = self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")))
        self.assertEqual(card["home"].get("logo_id"), cards.logo_id_of(UCA))
        self.assertEqual(self.calls, [])

    def test_a_failed_refresh_keeps_the_stale_list(self):
        path = cards._teams_path(self.root, "football", "college-football")
        os.makedirs(self.root, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(self.raw)
        old = cards.TEAM_LIST_AGE
        cards.TEAM_LIST_AGE = -1
        self.cache.team_opener = lambda url: None
        try:
            card = self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")))
        finally:
            cards.TEAM_LIST_AGE = old
        self.assertEqual(card["home"].get("logo_id"), cards.logo_id_of(UCA))

    def test_a_cold_list_returns_before_the_download(self):
        """The show path must not wait on the teams list. The relay gives /show 3 seconds."""
        gate = threading.Event()
        calls = []
        real = cards.fetch_teams_bytes

        def slow(url, opener=None):
            calls.append(threading.current_thread())
            gate.wait(2)
            return self.raw

        self.cache.team_opener = None
        self.cache.team_refresh = True
        cards.fetch_teams_bytes = slow
        try:
            started = time.monotonic()
            index = cards.load_team_index(self.cache, "football", "college-football", "ncaa", True)
            elapsed = time.monotonic() - started
        finally:
            gate.set()
            cards.fetch_teams_bytes = real
        self.assertEqual(index, {})
        self.assertLess(elapsed, 0.5)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not calls:
            time.sleep(0.02)
        self.assertTrue(calls)
        self.assertIsNot(calls[0], threading.main_thread())


if __name__ == "__main__":
    unittest.main()
