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
        self._tmp = tempfile.TemporaryDirectory(prefix="gs1346-logo-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._drain_teams)
        self.root = self._tmp.name
        self.lines = []
        self.cache = cards.LogoCache(
            self.root, ["a.espncdn.com"], opener=lambda url: (_png(), "image/png"),
            resolve=lambda host: True, log=self.lines.append)
        with cards._TEAM_LOCK:
            cards._TEAM_REFRESH.clear()
            cards._TEAM_FAIL_AT.clear()
            cards._TEAM_PARSED.clear()

    def _drain_teams(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and cards._TEAM_REFRESH:
            time.sleep(0.02)

    def _seed(self):
        path = cards._teams_path(self.root, "football", "college-football")
        os.makedirs(self.root, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(self.raw)
        return path

    def _teams(self, url):
        self.calls.append(url)
        return self.raw

    def _prepare(self, card, **kw):
        return cards.prepare_display_logos(card, self.cache, wait_s=1, **kw)

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
        self._seed()
        self.cache.team_refresh = True
        for name, abbr in (("Central Arkansas", "CARK"), ("C Arkansas", "CARK"), ("cark", "CARK")):
            card = self._prepare(_card((name, abbr), ("Arkansas", "ARK")), opener=self._teams)
            self.assertEqual(card["home"].get("logo_id"), cards.logo_id_of(UCA), name)
            self.assertNotIn("logo_url", card["home"])
            self.assertNotIn("logo none: home", self.lines)
        self.assertEqual(self.calls, [])
        for line in self.lines:
            self.assertNotIn("http", line)
            self.assertNotIn("espn", line)

    def test_unknown_team_falls_back_to_the_abbreviation(self):
        self._seed()
        card = self._prepare(_card(("Central Arkansas", "CARK"), ("Atlantis", "ZZZ")), card_id="game-1")
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
        self._seed()
        self.cache.team_refresh = True
        card = self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")), opener=self._teams)
        self.assertEqual(card["home"].get("logo_id"), cards.logo_id_of(UCA))
        self.assertEqual(self.calls, [])

    def test_a_failed_write_fetches_once_and_then_backs_off(self):
        def boom(path, data):
            raise OSError("full")

        real = cards._write_teams
        cards._write_teams = boom
        self.cache.team_refresh = True
        path = cards._teams_path(self.root, "football", "college-football")
        try:
            self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")), opener=self._teams)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and path not in cards._TEAM_FAIL_AT:
                time.sleep(0.02)
            self.assertIn(path, cards._TEAM_FAIL_AT)
            self.assertEqual(len(self.calls), 1)
            self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")), opener=self._teams)
            time.sleep(0.3)
            self.assertEqual(len(self.calls), 1)
        finally:
            cards._write_teams = real

    def test_a_parsed_index_is_not_read_again_for_the_same_file(self):
        self._seed()
        reads = []
        real = cards._read_teams

        def wrap(path):
            reads.append(path)
            return real(path)

        cards._read_teams = wrap
        try:
            cards.load_team_index(self.cache, "football", "college-football", "ncaa", True)
            cards.load_team_index(self.cache, "football", "college-football", "ncaa", True)
        finally:
            cards._read_teams = real
        self.assertEqual(len(reads), 1)

    def test_logo_none_is_one_line_per_card_and_side(self):
        self._seed()
        card = _card(("Central Arkansas", "CARK"), ("Atlantis", "ZZZ"))
        self._prepare(card, card_id="game-1")
        self._prepare(card, card_id="game-1")
        self.assertEqual(self.lines.count("logo none: away"), 1)
        self._prepare(card, card_id="game-2")
        self.assertEqual(self.lines.count("logo none: away"), 2)

    def test_a_cold_list_returns_before_the_download(self):
        """The show path must not wait on the teams list. The relay gives /show 3 seconds."""
        gate = threading.Event()
        calls = []

        def slow(url):
            calls.append(threading.current_thread())
            gate.wait(2)
            return self.raw

        self.cache.team_refresh = True
        try:
            started = time.monotonic()
            card = self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")), opener=slow)
            elapsed = time.monotonic() - started
        finally:
            gate.set()
        self.assertTrue(card["home"].get("logo_badge"))
        self.assertLess(elapsed, 0.5)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not calls:
            time.sleep(0.02)
        self.assertTrue(calls)
        self.assertIsNot(calls[0], threading.main_thread())

    def test_a_failed_fetch_backs_off_for_about_ten_minutes(self):
        calls = []

        def fail(url):
            calls.append(url)
            return None

        self.cache.team_refresh = True
        path = cards._teams_path(self.root, "football", "college-football")
        self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")), opener=fail)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and path not in cards._TEAM_FAIL_AT:
            time.sleep(0.02)
        self.assertIn(path, cards._TEAM_FAIL_AT)
        self.assertEqual(len(calls), 1)
        self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")), opener=fail)
        time.sleep(0.3)
        self.assertEqual(len(calls), 1)

    def test_an_empty_allowlist_does_not_fetch(self):
        calls = []
        self.cache.hosts = []
        self.cache.team_refresh = True
        self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")),
                      opener=lambda url: calls.append(url) or self.raw)
        time.sleep(0.3)
        self.assertEqual(calls, [])
        self.cache.hosts = ["example.com"]
        self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")),
                      opener=lambda url: calls.append(url) or self.raw)
        time.sleep(0.3)
        self.assertEqual(calls, [])

    def test_a_nested_array_is_one_fetch_and_the_cache_does_not_raise(self):
        nested = (b"[" * 10000) + b"0" + (b"]" * 10000)
        calls = []

        def opener(url):
            calls.append(url)
            return nested

        self.cache.team_refresh = True
        path = cards._teams_path(self.root, "football", "college-football")
        card = _card(("Central Arkansas", "CARK"), ("Arkansas", "ARK"))
        self._prepare(card, opener=opener)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and path not in cards._TEAM_FAIL_AT:
            time.sleep(0.02)
        self.assertIn(path, cards._TEAM_FAIL_AT)
        self.assertEqual(len(calls), 1)
        self._prepare(card, opener=opener)
        time.sleep(0.3)
        self.assertEqual(len(calls), 1)
        os.makedirs(self.root, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(nested)
        self.assertIsNone(cards._read_teams(path))
        self.assertEqual(cards.load_team_index(self.cache, "football", "college-football", "ncaa", True), {})

    def test_a_dripping_body_aborts_inside_the_deadline_and_backs_off(self):
        class Drip:
            def read1(self, n):
                time.sleep(0.2)
                return b"x"

        calls = []
        elapsed = []
        old = cards.TEAM_FETCH_S
        cards.TEAM_FETCH_S = 0.45

        def opener(url):
            calls.append(url)
            started = time.monotonic()
            got = cards._read_capped(Drip(), cards.TEAM_LIST_MAX, started + cards.TEAM_FETCH_S)
            elapsed.append(time.monotonic() - started)
            return got

        self.cache.team_refresh = True
        path = cards._teams_path(self.root, "football", "college-football")
        try:
            self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")), opener=opener)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and path not in cards._TEAM_FAIL_AT:
                time.sleep(0.02)
        finally:
            cards.TEAM_FETCH_S = old
        self.assertEqual(len(calls), 1)
        self.assertLess(elapsed[0], 0.45 + 0.35)
        self.assertIn(path, cards._TEAM_FAIL_AT)
        self._prepare(_card(("Central Arkansas", "CARK"), ("Arkansas", "ARK")), opener=opener)
        time.sleep(0.3)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
