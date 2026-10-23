#!/usr/bin/env python3
"""Display cards: schema, escaping, rotation, expiry, logos, and the signed show path."""
import base64, importlib.machinery, importlib.util, io, json, os, re, struct, sys, tempfile, threading, time, unittest, zlib
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import cards  # noqa: E402
import botreply  # noqa: E402

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import grok_station as gs  # noqa: E402
import pygame  # noqa: E402

_helper_path = os.path.join(ROOT, "tools", "grok-station-reply")
_loader = importlib.machinery.SourceFileLoader("grok_station_reply_cards", _helper_path)
_spec = importlib.util.spec_from_loader("grok_station_reply_cards", _loader)
reply_tool = importlib.util.module_from_spec(_spec)
_loader.exec_module(reply_tool)


def _b64(raw):
    return base64.b64encode(raw).decode()


def _seal(station, signer, message, bot="jules"):
    signed = signer.sign(message if isinstance(message, (bytes, bytearray)) else message.encode())
    sealed = SealedBox(station.public_key).encrypt(signed)
    return json.dumps({"v": 1, "bot": bot, "sealed": _b64(sealed)}, separators=(",", ":")).encode()


def recipe(**kw):
    card = {"type": "recipe", "v": 1, "title": "Weeknight soup", "servings": 4,
            "ingredients": ["onion", "carrots", "stock"],
            "steps": ["Chop the vegetables.", "Simmer twenty minutes.", "Season and serve."], "step": 2}
    card.update(kw)
    return card


def team(name="Harbor City", abbr="HC", **kw):
    row = {"name": name, "abbr": abbr}
    row.update(kw)
    return row


def sports(**kw):
    card = {"type": "sports", "v": 1, "league": "City league",
            "home": team(), "away": team("Ridgeview", "RV"),
            "home_score": 3, "away_score": 2, "status": "live", "period": "2nd",
            "schedule": [{"label": "Harbor City at Ridgeview", "when": "Sat 7pm"}]}
    card.update(kw)
    return card


def calendar(**kw):
    card = {"type": "calendar", "v": 1, "date": "2026-10-22",
            "events": [{"time": "9:00", "title": "Market"}, {"time": "18:30", "title": "Practice"}]}
    card.update(kw)
    return card


def home(**kw):
    card = {"type": "home", "v": 1, "title": "Stocks",
            "items": [{"label": "Northwind", "value": "12.40", "trend": "up"},
                      {"label": "Harbor", "value": "8.10", "trend": "down"}]}
    card.update(kw)
    return card


def _meta():
    nonce = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
    return {"kind": "show", "nonce": nonce, "exp_ms": int(time.time() * 1000) + 60_000}


def put(card, card_id="soup", priority=10, ttl_s=60):
    doc = {"v": 1, "op": "put", "id": card_id, "priority": priority, "ttl_s": ttl_s, "card": card}
    doc.update(_meta())
    return doc


def clear(card_id="soup"):
    doc = {"v": 1, "op": "clear", "id": card_id}
    doc.update(_meta())
    return doc


def tiny_png(w=8, h=8):
    """A complete 8-bit RGB PNG small enough for the logo cache and for pygame."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def tiny_jpeg(w=4, h=3):
    sof = bytes([0xFF, 0xC0, 0x00, 0x0B, 0x08]) + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + sof


class Schema(unittest.TestCase):
    def test_each_type_is_accepted_and_stable(self):
        for build in (recipe, sports, calendar, home):
            cleaned = cards.validate_card(build())
            self.assertEqual(cleaned, cards.validate_card(cleaned))
            self.assertNotIn("<", json.dumps(cleaned))

    def test_unknown_type_version_and_extra_keys(self):
        with self.assertRaises(cards.CardError) as cm:
            cards.validate_card({"type": "note", "v": 1})
        self.assertEqual(cm.exception.reason, "type")
        bad = recipe()
        bad["v"] = 2
        with self.assertRaises(cards.CardError) as cm:
            cards.validate_card(bad)
        self.assertEqual(cm.exception.reason, "schema")
        extra = recipe()
        extra["note"] = "nope"
        with self.assertRaises(cards.CardError) as cm:
            cards.validate_card(extra)
        self.assertEqual(cm.exception.reason, "schema")
        with self.assertRaises(cards.CardError):
            cards.validate_card(["recipe"])

    def test_numbers_enums_and_caps(self):
        for bad in (recipe(servings=True), recipe(servings=0), recipe(step=9), recipe(ingredients=[]),
                    sports(status="halftime"), sports(home_score=-1), home(items=[{"label": "A", "value": "1", "trend": "sideways"}]),
                    calendar(date="2026-13-01"), calendar(events=[{"time": "9:00", "title": "X"}] * 9)):
            with self.assertRaises(cards.CardError) as cm:
                cards.validate_card(bad)
            self.assertEqual(cm.exception.reason, "schema")
        team_bad = sports()
        team_bad["home"] = team(abbr="hc")
        with self.assertRaises(cards.CardError):
            cards.validate_card(team_bad)
        both = sports()
        both["home"] = team(logo_url="https://cdn.example.com/a.png", logo_id="ab" * 32)
        with self.assertRaises(cards.CardError):
            cards.validate_card(both)
        long = recipe(title="n" * 61)
        with self.assertRaises(cards.CardError) as cm:
            cards.validate_card(long)
        self.assertEqual(cm.exception.reason, "schema")

    def test_oversize_document_is_rejected_before_it_is_kept(self):
        huge = recipe(title="x" * 5000)
        with self.assertRaises(cards.CardError) as cm:
            cards.validate_card(huge)
        self.assertEqual(cm.exception.reason, "size")

    def test_show_put_and_clear(self):
        show = cards.validate_show(put(recipe(), ttl_s=120), 3600)
        self.assertEqual(show["op"], "put")
        self.assertNotIn("bot", show)
        cleared = cards.validate_show(clear(), 3600)
        self.assertEqual(set(cleared), {"v", "op", "id", "kind", "nonce", "exp_ms"})
        self.assertEqual(cleared["kind"], "show")
        self.assertTrue(cards.nonce_ok(cleared["nonce"]))
        short = clear()
        short["nonce"] = "aa"
        with self.assertRaises(cards.CardError):
            cards.validate_show(short, 3600)
        stamped = dict(put(recipe()), bot="jules")
        with self.assertRaises(cards.CardError):
            cards.validate_show(stamped, 3600)
        with self.assertRaises(cards.CardError) as cm:
            cards.validate_show(put(recipe(), ttl_s=0), 3600)
        self.assertEqual(cm.exception.reason, "ttl")
        with self.assertRaises(cards.CardError) as cm:
            cards.validate_show(put(recipe(), ttl_s=5000), 3600)
        self.assertEqual(cm.exception.reason, "ttl")
        with self.assertRaises(cards.CardError):
            cards.validate_show(put(recipe(), priority=True), 3600)
        with self.assertRaises(cards.CardError):
            cards.validate_show({"v": 1, "op": "drop", "id": "soup"}, 3600)

    def test_escaping_strips_markup_and_is_idempotent(self):
        cleaned = cards.escape_text("Hello <b>there</b> \u202e friend", 80)
        self.assertEqual(cleaned, "Hello there friend")
        self.assertEqual(cards.escape_text(cleaned, 80), cleaned)
        self.assertEqual(cards.escape_text("a\x00b\u0007c", 10), "abc")
        # Tags are removed. Text that was inside a tag stays, as data. An empty result is an error.
        self.assertEqual(cards.escape_text("<script>alert(1)</script>", 40), "alert(1)")
        with self.assertRaises(cards.CardError) as cm:
            cards.escape_text("<b></b>", 40)
        self.assertEqual(cm.exception.reason, "escape")
        left = cards.escape_text("see <script>alert(1)</script> now", 80)
        self.assertNotIn("<", left)
        self.assertNotIn(">", left)
        self.assertEqual(cards.escape_text(left, 80), left)
        with self.assertRaises(cards.CardError) as cm:
            cards.escape_text(12, 10)
        self.assertEqual(cm.exception.reason, "escape")
        with self.assertRaises(cards.CardError) as cm:
            cards.escape_text("a" * 61, 60)
        self.assertEqual(cm.exception.reason, "schema")
        card = cards.validate_card(recipe(title="Soup <img src=x onerror=alert(1)> pot"))
        self.assertEqual(card["title"], "Soup pot")

    def test_logo_url_shape_is_https_and_public(self):
        self.assertTrue(cards.logo_url_shape("https://cdn.example.com/a.png"))
        for bad in ("http://cdn.example.com/a.png", "https://user:pass@cdn.example.com/a.png",
                    "https://127.0.0.1/a.png", "https://cdn.example.com/a.png#x", "javascript:alert(1)"):
            self.assertFalse(cards.logo_url_shape(bad), bad)
        self.assertTrue(cards.logo_url_ok("https://cdn.example.com/a.png", ["cdn.example.com"]))
        self.assertFalse(cards.logo_url_ok("https://evil.cdn.example.com/a.png", ["cdn.example.com"]))
        self.assertFalse(cards.logo_url_ok("https://cdn.example.com.evil.test/a.png", ["cdn.example.com"]))

    def test_hosts_fail_closed(self):
        self.assertEqual(cards.hosts_from_env("CDN.Example.com, cdn.example.com"), ["cdn.example.com"])
        self.assertEqual(cards.hosts_from_env("cdn.example.com, evil.local"), [])
        self.assertEqual(cards.hosts_from_env(""), [])
        self.assertIsNone(cards.normalize_hosts(["cdn.example.com"] * 9))
        self.assertEqual(cards.parse_hosts(""), [])
        self.assertEqual(cards.parse_hosts("CDN.Example.com\nimg.example.org"), ["cdn.example.com", "img.example.org"])
        self.assertIsNone(cards.parse_hosts("not a host"))
        self.assertIsNone(cards.parse_hosts("foo.local"))
        for host in ("127.0.0.1", "localhost", "cdn.local", "cdn.lan", "cdn.internal", "printer.localhost"):
            self.assertFalse(cards.host_ok(host), host)
        self.assertFalse(cards.ip_ok("10.42.0.7"))
        self.assertFalse(cards.ip_ok("127.0.0.1"))
        self.assertTrue(cards.ip_ok("8.8.8.8"))
        self.assertEqual(cards.ttl_cap("999999"), cards.TTL_HARD)
        self.assertEqual(cards.ttl_cap("30"), cards.TTL_HARD)
        self.assertEqual(cards.ttl_cap("120"), 120)
        self.assertEqual(cards.rotate_s(""), 20)
        self.assertEqual(cards.rotate_s("4"), 20)
        self.assertEqual(cards.rotate_s("15"), 15)


class DeckTests(unittest.TestCase):
    def test_rotation_is_priority_then_older(self):
        deck = cards.Deck(rotate_s=10, ttl_max=100)
        deck.put("jules", "low", 1, 80, home(title="Low"), 0)
        deck.put("jules", "high", 5, 80, home(title="High"), 1)
        self.assertEqual(deck.current(1)["id"], "high")
        self.assertEqual(deck.current(11)["id"], "low")
        self.assertEqual(deck.current(21)["id"], "high")
        same = cards.Deck(rotate_s=10, ttl_max=100)
        same.put("jules", "older", 3, 80, home(title="Older"), 0)
        same.put("jules", "newer", 3, 80, home(title="Newer"), 1)
        self.assertEqual(same.current(1)["id"], "older")
        self.assertEqual(same.current(11)["id"], "newer")

    def test_replace_keeps_sequence_and_clear_is_per_bot(self):
        deck = cards.Deck(rotate_s=10, ttl_max=100)
        deck.put("jules", "soup", 1, 80, recipe(), 0)
        deck.put("jules", "game", 9, 80, sports(), 1)
        seq = next(it["seq"] for it in deck._items if it["id"] == "soup")
        deck.put("jules", "soup", 1, 80, recipe(step=3), 2)
        again = next(it for it in deck._items if it["id"] == "soup")
        self.assertEqual(again["seq"], seq)
        self.assertEqual(again["card"]["step"], 3)
        self.assertEqual(deck.current(2)["id"], "game")
        self.assertFalse(deck.clear("hank", "soup"))
        self.assertTrue(any(it["id"] == "soup" for it in deck._items))
        self.assertTrue(deck.clear("jules", "soup"))
        self.assertFalse(any(it["id"] == "soup" for it in deck._items))
        self.assertFalse(deck.clear("jules", "missing"))

    def test_expiry_removes_the_card(self):
        deck = cards.Deck(rotate_s=20, ttl_max=100)
        deck.put("jules", "soup", 1, 10, recipe(), 0)
        self.assertIsNotNone(deck.current(9.9))
        self.assertIsNone(deck.current(10))
        self.assertIsNone(deck.seconds_until_change(10))

    def test_caps_and_home_row(self):
        deck = cards.Deck(rotate_s=10, ttl_max=100, max_cards=4, per_bot=2)
        deck.put("jules", "a", 1, 30, home(title="A"), 0)
        deck.put("jules", "b", 1, 30, home(title="B"), 1)
        with self.assertRaises(cards.CardError):
            deck.put("jules", "c", 1, 30, home(title="C"), 2)
        deck.put("hank", "a", 1, 30, home(title="D"), 2)
        deck.put("hank", "b", 9, 30, home(title="E"), 3)
        with self.assertRaises(cards.CardError):
            deck.put("piper", "a", 1, 30, home(title="F"), 4)
        row = deck.home_row(3)
        self.assertEqual(row[0]["id"], "b")  # hank's higher priority card is the slot, and it is a home card
        self.assertLessEqual(len(row), 3)
        with self.assertRaises(cards.CardError) as cm:
            deck.put("piper", "a", 1, 500, home(), 0)
        self.assertEqual(cm.exception.reason, "ttl")

    def test_seconds_until_change_tracks_rotation(self):
        deck = cards.Deck(rotate_s=10, ttl_max=100)
        deck.put("jules", "a", 1, 100, home(), 0)
        self.assertEqual(deck.seconds_until_change(0), 10)
        self.assertEqual(deck.current(0)["id"], "a")
        self.assertAlmostEqual(deck.seconds_until_change(4), 6)


class _FakePeer:
    def __init__(self, ip):
        self.ip = ip

    def getpeername(self):
        return (self.ip, 443)

    def close(self):
        self.closed = True


class LogoTests(unittest.TestCase):
    def test_dns_rebind_to_a_private_peer_is_refused(self):
        calls = []

        def resolve(host):
            calls.append(host)
            if len(calls) == 1:
                return ["8.8.8.8"]
            return ["10.0.0.5"]

        class Rebind(cards.PinnedHTTPSConnection):
            def _open_tcp(self):
                return _FakePeer("10.0.0.5")

        with self.assertRaises(cards.CardError):
            cards.default_opener("https://cdn.example.com/a.png", resolve=resolve, connection=Rebind)
        self.assertEqual(calls, ["cdn.example.com"])
        with self.assertRaises(cards.CardError):
            cards.default_opener("https://cdn.example.com/a.png", resolve=lambda host: ["10.0.0.5"])

    def test_sniff_accepts_png_and_jpeg_only(self):
        self.assertTrue(cards.sniff_image(tiny_png()))
        self.assertTrue(cards.sniff_image(tiny_jpeg()))
        self.assertFalse(cards.sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"))
        self.assertFalse(cards.sniff_image(tiny_png(w=800, h=8)))
        self.assertFalse(cards.sniff_image(tiny_png() + b"x" * cards.LOGO_MAX))
        self.assertFalse(cards.sniff_image(b"\x89PNG\r\n\x1a\n" + b"not-really"))

    def test_allowlist_private_hosts_and_cache_limits(self):
        root = tempfile.mkdtemp(prefix="gs-logos-")
        calls = []

        def opener(url):
            calls.append(url)
            return tiny_png(), "image/png"

        cache = cards.LogoCache(root, ["cdn.example.com"], opener=opener, resolve=lambda host: True,
                                max_files=2, max_total=cards.LOGO_TOTAL)
        self.assertIsNone(cache.fetch("https://evil.example/a.png"))
        self.assertEqual(calls, [])
        self.assertFalse(os.path.isdir(root) and any(os.scandir(root)))
        blocked = cards.LogoCache(root, ["cdn.example.com"], opener=opener, resolve=lambda host: False)
        self.assertIsNone(blocked.fetch("https://cdn.example.com/a.png"))
        self.assertEqual(calls, [])
        first = cache.fetch("https://cdn.example.com/a.png")
        self.assertRegex(first, r"^[a-f0-9]{64}$")
        self.assertTrue(os.path.isfile(os.path.join(root, first)))
        self.assertEqual(cache.fetch("https://cdn.example.com/a.png"), first)
        self.assertEqual(len(calls), 1)
        second = cache.fetch("https://cdn.example.com/b.png")
        now = time.time()
        os.utime(os.path.join(root, first), (now - 30, now - 30))
        os.utime(os.path.join(root, second), (now - 10, now - 10))
        third = cache.fetch("https://cdn.example.com/c.png?x=1")
        names = [n for n in os.listdir(root) if cards.LOGO_ID.fullmatch(n)]
        self.assertLessEqual(len(names), 2)
        self.assertIn(third, names)
        self.assertNotIn(first, names)
        svg = cards.LogoCache(tempfile.mkdtemp(prefix="gs-logos-"), ["cdn.example.com"],
                              opener=lambda url: (b"<svg></svg>", "image/svg+xml"), resolve=lambda host: True)
        self.assertIsNone(svg.fetch("https://cdn.example.com/mark.svg"))
        self.assertFalse(any(os.scandir(svg.root)) if os.path.isdir(svg.root) else False)
        huge = cards.LogoCache(tempfile.mkdtemp(prefix="gs-logos-"), ["cdn.example.com"],
                               opener=lambda url: (b"x" * (cards.LOGO_MAX + 1), "image/png"), resolve=lambda host: True)
        self.assertIsNone(huge.fetch("https://cdn.example.com/big.png"))
        self.assertIsNone(cache.path_for("../" + "a" * 64))
        self.assertIsNone(cache.path_for("a" * 63))

    def test_logo_older_than_a_week_is_fetched_again(self):
        root = tempfile.mkdtemp(prefix="gs-logos-")
        calls = []

        def opener(url):
            calls.append(url)
            return tiny_png(), "image/png"

        cache = cards.LogoCache(root, ["cdn.example.com"], opener=opener, resolve=lambda host: True)
        url = "https://cdn.example.com/a.png"
        lid = cache.fetch(url)
        self.assertEqual(len(calls), 1)
        recent = time.time() - 6 * 24 * 3600
        os.utime(cache.path_for(lid), (recent, recent))
        self.assertEqual(cache.fetch(url), lid)
        self.assertEqual(len(calls), 1)
        stale = time.time() - 8 * 24 * 3600
        os.utime(cache.path_for(lid), (stale, stale))
        self.assertEqual(cache.fetch(url), lid)
        self.assertEqual(len(calls), 2)
        self.assertLess(time.time() - os.path.getmtime(cache.path_for(lid)), 60)

    def test_settle_shows_a_monogram_until_the_background_fetch_finishes(self):
        root = tempfile.mkdtemp(prefix="gs-logos-")
        release = threading.Event()
        calls = []

        def opener(url):
            calls.append(url)
            if not release.wait(2):
                raise AssertionError("settle blocked on the download")
            return tiny_png(), "image/png"

        cache = cards.LogoCache(root, ["cdn.example.com"], opener=opener, resolve=lambda host: True)
        card = sports()
        card["home"] = team(logo_url="https://cdn.example.com/hc.png")
        settled = cards.settle_logos(card, cache)
        lid = settled["home"].get("logo_id")
        self.assertRegex(lid or "", r"^[a-f0-9]{64}$")
        self.assertNotIn("logo_url", settled["home"])
        path = cache.path_for(lid)
        deadline = time.time() + 2
        while time.time() < deadline and not calls:
            time.sleep(0.01)
        self.assertEqual(calls, ["https://cdn.example.com/hc.png"])
        self.assertFalse(os.path.isfile(path))
        release.set()
        deadline = time.time() + 2
        while time.time() < deadline and not os.path.isfile(path):
            time.sleep(0.02)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(cache.fetch_later("https://cdn.example.com/hc.png"), lid)
        self.assertEqual(len(calls), 1)

    def test_settle_drops_urls_that_are_not_cached(self):
        root = tempfile.mkdtemp(prefix="gs-logos-")
        calls = []
        cache = cards.LogoCache(root, [], opener=lambda url: calls.append(url), resolve=lambda host: True)
        card = sports()
        card["home"] = team(logo_url="https://cdn.example.com/hc.png")
        settled = cards.settle_logos(card, cache)
        self.assertEqual(calls, [])
        self.assertNotIn("logo_url", settled["home"])
        self.assertNotIn("logo_id", settled["home"])


class SignedShow(unittest.TestCase):
    def setUp(self):
        self.station = PrivateKey.generate()
        self.signer = SigningKey.generate()
        self.keys = {"killed": False, "keys": {"jules": _b64(bytes(self.signer.verify_key))}}
        self.known = {"jules"}
        self.limiter = botreply.Limiter(12, 60)

    def _parse(self, doc):
        self.parsed = doc
        return cards.validate_show(doc, 86400)

    def test_signature_is_required_before_the_card_is_parsed(self):
        self.parsed = None
        raw = json.dumps(put(recipe())).encode()
        with self.assertRaises(botreply.Reject) as cm:
            botreply.ingest_show(raw, self.station, self.keys, self.known, self.limiter, self._parse)
        self.assertNotEqual(cm.exception.verdict, "ok")
        self.assertIsNone(self.parsed)
        flipped = bytearray(_seal(self.station, self.signer, json.dumps(put(recipe()), separators=(",", ":")).encode()))
        # Flip a byte inside the sealed blob so the signature cannot verify.
        outer = json.loads(flipped)
        blob = bytearray(base64.b64decode(outer["sealed"]))
        blob[20] ^= 0x01
        outer["sealed"] = _b64(bytes(blob))
        raw = json.dumps(outer).encode()
        with self.assertRaises(botreply.Reject) as cm:
            botreply.ingest_show(raw, self.station, self.keys, self.known, self.limiter, self._parse)
        self.assertIn(cm.exception.verdict, ("unseal", "signature"))
        self.assertIsNone(self.parsed)

    def test_signed_put_stamps_the_outer_bot_only(self):
        body = json.dumps(put(recipe()), separators=(",", ":"), sort_keys=True).encode()
        got = botreply.ingest_show(_seal(self.station, self.signer, body), self.station, self.keys, self.known,
                                   self.limiter, self._parse)
        self.assertEqual(got["bot"], "jules")
        self.assertEqual(got["card"]["title"], "Weeknight soup")
        self.assertNotIn("bot", self.parsed)
        # A card is not a spoken reply, and a reply is not a card.
        with self.assertRaises(botreply.Reject) as cm:
            botreply.ingest(_seal(self.station, self.signer, body), self.station, self.keys, self.known,
                            lambda _rid: None, set(), self.limiter)
        self.assertEqual(cm.exception.verdict, "schema")

    def test_helper_show_round_trip(self):
        show = {"v": 1, "op": "clear", "id": "soup"}
        raw = reply_tool.build_show("jules", show, _b64(bytes(self.signer)), botreply.station_pub_b64(self.station))
        got = botreply.ingest_show(raw, self.station, self.keys, self.known, self.limiter, self._parse)
        self.assertEqual(got["op"], "clear")
        self.assertEqual(got["id"], "soup")
        self.assertEqual(got["bot"], "jules")
        self.assertEqual(got["kind"], "show")
        self.assertTrue(cards.nonce_ok(got["nonce"]))
        self.assertGreater(got["exp_ms"], int(time.time() * 1000))
        with self.assertRaises(ValueError):
            reply_tool.post_reply("https://station.example", b"{}", path="/voice/drop")

    def test_accept_show_posts_only_after_the_signature(self):
        import relay
        posts = []
        body = json.dumps(put(sports(), card_id="game", ttl_s=90), separators=(",", ":"), sort_keys=True).encode()
        raw = _seal(self.station, self.signer, body)

        def post(show):
            posts.append(show)
            return 200

        status, resp, info = relay.accept_show(raw, self.station, self.keys, self.known, self.limiter, post, ttl_max=3600)
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"ok": True})
        self.assertEqual(posts[0]["bot"], "jules")
        self.assertEqual(posts[0]["card"]["type"], "sports")
        self.assertEqual(getattr(info, "verdict", None), "ok")
        posts.clear()
        plain = json.dumps({"v": 1, "bot": "jules", "sealed": "aaaa"}).encode()
        status, resp, info = relay.accept_show(plain, self.station, self.keys, self.known, self.limiter, post, ttl_max=3600)
        self.assertNotEqual(status, 200)
        self.assertEqual(posts, [])
        posts.clear()

        def down(show):
            posts.append(show)
            return 400

        status, resp, info = relay.accept_show(raw, self.station, self.keys, self.known, botreply.Limiter(12, 60), down, ttl_max=3600)
        self.assertEqual(status, 503)
        self.assertEqual(len(posts), 1)

    def test_replay_of_the_same_blob_is_409(self):
        import relay
        raw = _seal(self.station, self.signer, json.dumps(put(recipe()), separators=(",", ":"), sort_keys=True).encode())
        seen = botreply.ShowSeen(os.path.join(tempfile.mkdtemp(prefix="gs-seen-"), "show-seen.json"))
        posts = []

        def post(show):
            posts.append(show)
            return 200

        status, resp, info = relay.accept_show(raw, self.station, self.keys, self.known, self.limiter, post,
                                               ttl_max=3600, seen=seen)
        self.assertEqual((status, resp), (200, {"ok": True}))
        self.assertEqual(len(posts), 1)
        again, resp, info = relay.accept_show(raw, self.station, self.keys, self.known, botreply.Limiter(12, 60), post,
                                              ttl_max=3600, seen=seen)
        self.assertEqual(again, 409)
        self.assertEqual(resp["error"], "conflict")
        self.assertEqual(info.verdict, "duplicate")
        self.assertEqual(len(posts), 1)
        reloaded = botreply.ShowSeen(seen.path)
        third, _, info = relay.accept_show(raw, self.station, self.keys, self.known, botreply.Limiter(12, 60), post,
                                           ttl_max=3600, seen=reloaded)
        self.assertEqual(third, 409)
        self.assertEqual(info.verdict, "duplicate")

    def test_failed_post_does_not_burn_the_nonce(self):
        import relay
        raw = _seal(self.station, self.signer, json.dumps(clear(), separators=(",", ":"), sort_keys=True).encode())
        seen = botreply.ShowSeen(os.path.join(tempfile.mkdtemp(prefix="gs-seen-"), "show-seen.json"))
        calls = {"n": 0}

        def post(_show):
            calls["n"] += 1
            return 400 if calls["n"] == 1 else 200

        lim = botreply.Limiter(12, 60)
        status, _, _ = relay.accept_show(raw, self.station, self.keys, self.known, lim, post, ttl_max=3600, seen=seen)
        self.assertEqual(status, 503)
        status, _, _ = relay.accept_show(raw, self.station, self.keys, self.known, lim, post, ttl_max=3600, seen=seen)
        self.assertEqual(status, 200)
        status, _, info = relay.accept_show(raw, self.station, self.keys, self.known, lim, post, ttl_max=3600, seen=seen)
        self.assertEqual((status, info.verdict), (409, "duplicate"))

    def test_exp_window_and_kind_do_not_cross_reply(self):
        now = 1_700_000_000_000
        past = put(recipe())
        past["exp_ms"] = now - 1
        raw = _seal(self.station, self.signer, json.dumps(past, separators=(",", ":"), sort_keys=True).encode())
        with self.assertRaises(botreply.Reject) as cm:
            botreply.ingest_show(raw, self.station, self.keys, self.known, self.limiter, self._parse, now_ms=now)
        self.assertEqual((cm.exception.status, cm.exception.verdict), (403, "expired"))
        ahead = put(recipe())
        ahead["exp_ms"] = now + 121_000
        raw = _seal(self.station, self.signer, json.dumps(ahead, separators=(",", ":"), sort_keys=True).encode())
        with self.assertRaises(botreply.Reject) as cm:
            botreply.ingest_show(raw, self.station, self.keys, self.known, self.limiter, self._parse, now_ms=now)
        self.assertEqual((cm.exception.status, cm.exception.verdict), (403, "expired"))
        edge = put(recipe())
        edge["exp_ms"] = now + 120_000
        raw = _seal(self.station, self.signer, json.dumps(edge, separators=(",", ":"), sort_keys=True).encode())
        got = botreply.ingest_show(raw, self.station, self.keys, self.known, self.limiter, self._parse, now_ms=now)
        self.assertEqual(got["kind"], "show")
        show_body = json.dumps(put(recipe()), separators=(",", ":"), sort_keys=True).encode()
        with self.assertRaises(botreply.Reject) as cm:
            botreply.ingest(_seal(self.station, self.signer, show_body), self.station, self.keys, self.known,
                            lambda _rid: None, set(), self.limiter)
        self.assertEqual(cm.exception.verdict, "schema")
        reply = {"kind": "reply", "action_class": "answer", "request_id": "vs_" + "b" * 22, "text": "hi", "tier": 0}
        raw = _seal(self.station, self.signer, json.dumps(reply, separators=(",", ":"), sort_keys=True).encode())
        with self.assertRaises(botreply.Reject) as cm:
            botreply.ingest_show(raw, self.station, self.keys, self.known, self.limiter, self._parse)
        self.assertEqual(cm.exception.verdict, "schema")

    def test_seen_set_is_per_bot_bounded_and_mode_0600(self):
        path = os.path.join(tempfile.mkdtemp(prefix="gs-seen-"), "show-seen.json")
        store = botreply.ShowSeen(path, max_n=2)
        now = 1_000_000
        self.assertTrue(store.reserve("jules", "n1", now + 5000, now))
        self.assertFalse(store.reserve("jules", "n1", now + 5000, now))
        self.assertTrue(store.reserve("hank", "n1", now + 5000, now))
        # Full of live rows: refuse (None), never evict an unexpired nonce.
        self.assertIsNone(store.reserve("jules", "n2", now + 5000, now))
        reloaded = botreply.ShowSeen(path, max_n=2)
        self.assertTrue(reloaded.contains("jules", "n1", now))
        self.assertTrue(reloaded.contains("hank", "n1", now))
        self.assertFalse(reloaded.contains("jules", "n2", now))
        self.assertFalse(reloaded.contains("hank", "n1", now + 5001))
        # Once the rows expire there is room again.
        self.assertTrue(store.reserve("jules", "n2", now + 20000, now + 6000))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        store.release("jules", "n2", now + 6000)
        self.assertFalse(store.contains("jules", "n2", now + 6000))

    def test_full_seen_set_refuses_new_nonce(self):
        path = os.path.join(tempfile.mkdtemp(prefix="gs-seen-"), "show-seen.json")
        store = botreply.ShowSeen(path, max_n=1)
        now = int(time.time() * 1000)
        self.assertTrue(store.reserve("other", "x" * 22, now + 100000, now))
        self.assertIsNone(store.reserve("jules", "y" * 22, now + 100000, now))
        import relay
        raw = _seal(self.station, self.signer, json.dumps(put(recipe()), separators=(",", ":"), sort_keys=True).encode())
        posts = []
        status, resp, info = relay.accept_show(raw, self.station, self.keys, self.known, self.limiter,
                                               lambda show: posts.append(show) or 200, ttl_max=3600, seen=store)
        self.assertEqual(status, 503)
        self.assertEqual(info.verdict, "busy")
        self.assertEqual(posts, [])
        self.assertTrue(store.contains("other", "x" * 22, now))

    def _show(self, card, pii=True, post=None):
        import relay
        raw = _seal(self.station, self.signer, json.dumps(put(card), separators=(",", ":"), sort_keys=True).encode())
        posts = []

        def accept(show):
            posts.append(show)
            return 200

        status, resp, info = relay.accept_show(
            raw, self.station, self.keys, self.known, botreply.Limiter(30, 60),
            accept if post is None else post, ttl_max=3600, pii=pii)
        return status, resp, info, posts

    def test_show_lint_rejects_api_key_card_and_ssn(self):
        keyed = recipe(title="sk-test-abcdef1234")
        status, resp, info, posts = self._show(keyed)
        self.assertEqual(status, 422)
        self.assertEqual(resp["error"], "lint")
        self.assertEqual(info.verdict, "lint")
        self.assertEqual(posts, [])
        numbered = recipe(ingredients=["onion", "4242424242424242", "stock"])
        status, _, _, posts = self._show(numbered)
        self.assertEqual(status, 422)
        self.assertEqual(posts, [])
        ssn = home()
        ssn["items"] = [{"label": "123-45-6789", "value": "12.40", "trend": "up"}]
        status, _, _, posts = self._show(ssn, pii=False)
        self.assertEqual(status, 422)
        self.assertEqual(posts, [])

    def test_pii_lint_is_only_calendar_titles_and_home_values(self):
        mailed = recipe(title="ada@example.com")
        status, _, _, posts = self._show(mailed, pii=True)
        self.assertEqual(status, 200)
        self.assertEqual(len(posts), 1)
        day = calendar()
        day["events"] = [{"time": "9:00", "title": "ada@example.com"}]
        status, _, _, posts = self._show(day, pii=True)
        self.assertEqual(status, 422)
        self.assertEqual(posts, [])
        status, _, _, posts = self._show(day, pii=False)
        self.assertEqual(status, 200)
        self.assertEqual(len(posts), 1)
        board = home()
        board["items"] = [{"label": "Northwind", "value": "ada@example.com"}]
        status, _, _, posts = self._show(board, pii=True)
        self.assertEqual(status, 422)
        self.assertEqual(posts, [])
        status, _, _, posts = self._show(board, pii=False)
        self.assertEqual(status, 200)

    def test_logo_host_put_audits_the_host_list(self):
        import asyncio
        import relay
        tmp = tempfile.mkdtemp(prefix="gs-hosts-")
        old_file, old_gate, old_json = relay.WEBHOOKS_FILE, relay.admin_gate, relay.admin_json
        relay.WEBHOOKS_FILE = os.path.join(tmp, "webhooks.json")
        body = {"hosts": ["CDN.Example.com", "img.example.org"], "actor": "admin"}

        async def gate(_req):
            return None

        async def as_json(_req):
            return body

        relay.admin_gate, relay.admin_json = gate, as_json
        try:
            resp = asyncio.run(relay.admin_cards_put(object()))
            self.assertEqual(resp.status, 200)
            line = open(relay.audit_path()).read().strip()
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z admin - logo-hosts cdn\.example\.com,img\.example\.org$")
            body["hosts"] = ["evil.local"]
            before = open(relay.audit_path()).read()
            resp = asyncio.run(relay.admin_cards_put(object()))
            self.assertEqual(resp.status, 400)
            self.assertEqual(open(relay.audit_path()).read(), before)
            body["hosts"] = []
            body["actor"] = "nope name"
            resp = asyncio.run(relay.admin_cards_put(object()))
            self.assertEqual(resp.status, 400)
            self.assertEqual(open(relay.audit_path()).read(), before)
        finally:
            relay.WEBHOOKS_FILE, relay.admin_gate, relay.admin_json = old_file, old_gate, old_json

    def test_logo_host_file_rejects_illegal_names(self):
        import relay
        tmp = tempfile.mkdtemp(prefix="gs-hosts-")
        old = relay.WEBHOOKS_FILE
        relay.WEBHOOKS_FILE = os.path.join(tmp, "webhooks.json")
        try:
            self.assertTrue(relay.write_logo_hosts(["CDN.Example.com", "cdn.example.com"]))
            self.assertEqual(relay.read_logo_hosts(), ["cdn.example.com"])
            self.assertFalse(relay.write_logo_hosts(["evil.local"]))
            self.assertEqual(relay.read_logo_hosts(), ["cdn.example.com"])
            self.assertFalse(relay.write_logo_hosts(["cdn.example.com"] * 9))
        finally:
            relay.WEBHOOKS_FILE = old


class Station(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.display.init()
        pygame.font.init()

    def setUp(self):
        self.old_deck, self.old_logo, self.old_token = gs.DECK, gs._LOGO, gs.TOKEN
        self.root = tempfile.mkdtemp(prefix="gs-show-")
        gs.DECK = cards.Deck(rotate_s=10, ttl_max=86400)
        gs._LOGO = cards.LogoCache(self.root, ["cdn.example.com"], opener=lambda url: (_ for _ in ()).throw(AssertionError("fetch")),
                                   resolve=lambda host: False)
        gs.TOKEN = "station-token"

    def tearDown(self):
        gs.DECK, gs._LOGO, gs.TOKEN = self.old_deck, self.old_logo, self.old_token

    def _post(self, body, addr=("127.0.0.1", 9), token="station-token"):
        handler = object.__new__(gs.Handler)
        raw = json.dumps(body).encode() if not isinstance(body, (bytes, bytearray)) else body
        handler.client_address = addr
        handler.path = "/show"
        handler.headers = {"Authorization": "Bearer " + token, "Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.replies = []
        handler._reply = lambda code, obj: handler.replies.append((code, obj))
        gs.Handler.do_POST(handler)
        return handler.replies[-1]

    def test_show_requires_bearer_and_localhost_for_cards(self):
        doc = dict(put(recipe()), bot="jules")
        code, body = self._post(doc, token="nope")
        self.assertEqual(code, 401)
        code, body = self._post(doc, addr=("10.42.0.7", 9))
        self.assertEqual((code, body["error"]), (403, "localhost only"))
        code, body = self._post(doc)
        self.assertEqual(code, 200)
        self.assertEqual(gs.DECK.current(time.time())["card"]["title"], "Weeknight soup")
        code, body = self._post(dict(clear(), bot="hank"))
        self.assertEqual(code, 200)
        self.assertIsNotNone(gs.DECK.current(time.time()))
        code, body = self._post(dict(clear(), bot="jules"))
        self.assertEqual(code, 200)
        self.assertIsNone(gs.DECK.current(time.time()))
        code, body = self._post({"v": 1, "op": "put", "id": "soup", "priority": 1, "ttl_s": 30, "bot": "jules", "card": {"type": "note"}})
        self.assertEqual(code, 400)

    def test_apply_show_strips_a_logo_that_is_not_allowlisted(self):
        card = sports()
        card["away"] = team("Ridgeview", "RV", logo_url="https://cdn.example.com/rv.png")
        gs.apply_show(dict(put(card, card_id="game"), bot="jules"), gs._LOGO, gs.DECK, now=time.time())
        stored = gs.DECK.current(time.time())["card"]["away"]
        self.assertNotIn("logo_url", stored)
        self.assertNotIn("logo_id", stored)

    def test_screen_choice_and_feed_identity(self):
        self.assertEqual(gs.screen_for(None, False, None, False, {"type": "recipe"}), "feed")
        self.assertEqual(gs.screen_for(None, False, None, True, {"type": "recipe"}), "night")
        self.assertEqual(gs.screen_for(None, False, {"text": "hi"}, False, {"type": "sports"}), "session")
        self.assertEqual(gs.screen_for(None, False, None, False, {"type": "home"}), "idle")
        self.assertFalse(gs.feed_due(("session", 1, "a"), "b"))
        self.assertTrue(gs.feed_due(("idle", 1, "a"), "b"))
        gs.DECK.put("jules", "soup", 1, 60, recipe(), time.time())
        first = gs.feed_identity()
        gs.DECK.put("jules", "soup", 1, 60, recipe(step=3), time.time())
        self.assertNotEqual(first, gs.feed_identity())

    def test_draws_every_card_and_does_not_fetch(self):
        fetches = []

        def opener(url):
            fetches.append(url)
            raise AssertionError("fetch")

        gs._LOGO.opener = opener
        now = time.localtime()
        wx = type("W", (), {"data": None, "offline": lambda self: False})()
        samples = {
            "recipe": recipe(title="Soup <b>pot</b>"),
            "sports": sports(),
            "calendar": calendar(),
            "home": home(),
        }
        # validate first so drawing only ever sees escaped text
        samples = {k: cards.validate_card(v) for k, v in samples.items()}
        self.assertNotIn("<", samples["recipe"]["title"])
        edges = [
            sports(schedule=[]),
            sports(schedule=[{"label": "North side", "when": "Sun 1pm"},
                             {"label": "South side", "when": "Mon 6pm"},
                             {"label": "East side", "when": "Tue 8pm"},
                             {"label": "West side", "when": "Wed 4pm"}]),
            calendar(events=[]),
            calendar(events=[{"time": f"{h}:00", "title": f"Event {h}"} for h in range(8)]),
        ]
        for size in ((800, 480), (1920, 1080)):
            surf = pygame.Surface(size)
            layout = gs.Layout(*size)
            for kind, card in samples.items():
                if kind == "home":
                    gs.DECK = cards.Deck(rotate_s=20, ttl_max=86400)
                    gs.DECK.put("jules", "stocks", 1, 60, card, time.time())
                    gs.draw_idle(surf, layout, now, wx)
                else:
                    gs.draw_feed(surf, layout, now, card)
            for card in edges:
                gs.draw_feed(surf, layout, now, cards.validate_card(card))
        self.assertEqual(gs.friendly_date("2026-10-22"), "Thursday, October 22")
        self.assertEqual(gs.friendly_date("2026-02-31"), "2026-02-31")
        # Trend arrows use green and red. Nothing else in this small surface does.
        box = pygame.Surface((30, 20))
        box.fill(gs.BG)
        gs._trend(box, 4, 2, 14, "up")
        self.assertTrue(_has(box, gs.GREEN))
        self.assertFalse(_has(box, gs.RED))
        box.fill(gs.BG)
        gs._trend(box, 4, 2, 14, "down")
        self.assertTrue(_has(box, gs.RED))
        self.assertFalse(_has(box, gs.GREEN))
        mono = gs.team_mark({"name": "Harbor City", "abbr": "HC"}, 48)
        path = os.path.join(self.root, "ab" * 32)
        with open(path, "wb") as fh:
            fh.write(tiny_png())
        logo = gs.team_mark({"name": "Harbor City", "abbr": "HC", "logo_id": "ab" * 32}, 48)
        self.assertNotEqual(pygame.image.tostring(mono, "RGBA"), pygame.image.tostring(logo, "RGBA"))
        self.assertEqual(fetches, [])

    def test_installer_and_helper_allow_the_same_hosts(self):
        install = open(os.path.join(ROOT, "install.sh")).read()
        files = open(os.path.join(ROOT, "tools", "install-files.sh")).read()
        self.assertIn("app/cards.py", install)
        self.assertIn("app/cards.py", files)
        helper_path = os.path.join(ROOT, "admin", "grok-station-helper")
        loader = importlib.machinery.SourceFileLoader("gs_helper_cards", helper_path)
        spec = importlib.util.spec_from_loader("gs_helper_cards", loader)
        helper = importlib.util.module_from_spec(spec)
        loader.exec_module(helper)
        rule = helper.SETTINGS["display"]["GROK_STATION_LOGO_HOSTS"][1]
        rotate = helper.SETTINGS["display"]["GROK_STATION_CARD_ROTATE"][1]
        self.assertTrue(re.fullmatch(rotate, "20"))
        self.assertFalse(re.fullmatch(rotate, "4"))
        self.assertTrue(re.fullmatch(rule, ""))
        for raw in ("cdn.example.com", "CDN.Example.com, img.example.org", ""):
            hosts = cards.parse_hosts(raw)
            self.assertIsNotNone(hosts)
            self.assertTrue(re.fullmatch(rule, ",".join(hosts)))
        self.assertIn('class="inp"', open(os.path.join(ROOT, "admin", "web", "app.js")).read())
        self.assertIn("Needs attention", open(os.path.join(ROOT, "admin", "web", "app.js")).read())


def _has(surf, color):
    target = surf.map_rgb(color)
    pa = pygame.PixelArray(surf)
    try:
        w, h = surf.get_size()
        for y in range(h):
            for x in range(w):
                if pa[x, y] == target:
                    return True
        return False
    finally:
        del pa


if __name__ == "__main__":
    unittest.main()


class DefaultLogoHosts(unittest.TestCase):
    def test_espn_default_until_saved(self):
        self.assertEqual(cards.hosts_from_env(None), ["a.espncdn.com"])
        self.assertEqual(cards.hosts_from_env(""), [])  # a saved empty list stays empty
        u = "https://a.espncdn.com/i/teamlogos/nfl/500/kc.png"
        self.assertTrue(cards.logo_url_ok(u, cards.hosts_from_env(None)))
        self.assertFalse(cards.logo_url_ok("https://example.com/kc.png", cards.hosts_from_env(None)))

    def test_default_logo_is_cached_for_a_sports_card(self):
        png = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
               + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)))
               + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND")))
        c = cards.LogoCache(tempfile.mkdtemp(), cards.hosts_from_env(None), opener=lambda u: (png, "image/png"),
                            resolve=lambda h: ["23.1.2.3"])
        lid = c.fetch("https://a.espncdn.com/combiner/i?img=/i/teamlogos/nfl/500/kc.png&w=200&h=200")
        self.assertIsNotNone(lid)
        self.assertTrue(os.path.exists(c.path_for(lid)))
