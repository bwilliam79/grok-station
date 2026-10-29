#!/usr/bin/env python3
"""Status-icon checks: pure parsers, fallback glyphs, and top-right placement. No network or hardware."""
import io, json, os, sys, tempfile, time, unittest
from contextlib import redirect_stderr

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs
import pygame

ROUTE = (
    "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
    "eth0 00000000 00000000 0001 0 0 0 00000000 0 0 0\n"
)
NO_ROUTE = (
    "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
    "wlan0 02000000 00000000 0001 0 0 0 00000000 0 0 0\n"
)
CARDS_OK = " 0 [M0             ]: USB-Audio - EMEET OfficeCore M0 Plus\n"
CARDS_MISSING = " 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n"


def setUpModule():
    pygame.display.init()
    pygame.font.init()


def _ink_bounds(surf, bg):
    """Inclusive pixel bounds of anything that isn't bg, or None."""
    w, h = surf.get_size()
    bgm = surf.map_rgb(bg)
    pa = pygame.PixelArray(surf)
    minx = miny = 10 ** 9
    maxx = maxy = -1
    try:
        for y in range(h):
            for x in range(w):
                if pa[x, y] != bgm:
                    if x < minx:
                        minx = x
                    if x > maxx:
                        maxx = x
                    if y < miny:
                        miny = y
                    if y > maxy:
                        maxy = y
    finally:
        del pa
    if maxx < 0:
        return None
    return minx, miny, maxx, maxy


class NetParse(unittest.TestCase):
    def test_wifi_eth_down_and_no_route(self):
        wifi = [("lo", "up", False), ("wlan0", "up", True), ("eth0", "down", False)]
        self.assertEqual(gs.net_status(wifi, ROUTE), {"kind": "wifi", "up": True})
        eth = [("lo", "up", False), ("eth0", "up", False)]
        self.assertEqual(gs.net_status(eth, ROUTE), {"kind": "ethernet", "up": True})
        down = [("lo", "up", False), ("eth0", "down", False), ("wlan0", "down", True)]
        self.assertEqual(gs.net_status(down, ROUTE), {"kind": "none", "up": False})
        self.assertEqual(gs.net_status([("wlan0", "up", True)], NO_ROUTE), {"kind": "wifi", "up": False})
        # name prefixes count even without a wireless directory: wl* wifi, en* ethernet
        self.assertEqual(gs.net_status([("wlp1s0", "up", False)], ROUTE)["kind"], "wifi")
        self.assertEqual(gs.net_status([("enp0s3", "up", False)], ROUTE)["kind"], "ethernet")

    def test_sysfs_reader(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "lo"))
            os.makedirs(os.path.join(d, "wlan0", "wireless"))
            os.makedirs(os.path.join(d, "eth0"))
            with open(os.path.join(d, "lo", "operstate"), "w") as f:
                f.write("up\n")
            with open(os.path.join(d, "wlan0", "operstate"), "w") as f:
                f.write("up\n")
            with open(os.path.join(d, "eth0", "operstate"), "w") as f:
                f.write("down\n")
            route = os.path.join(d, "route")
            with open(route, "w") as f:
                f.write(ROUTE)
            self.assertEqual(gs.read_net(d, route), {"kind": "wifi", "up": True})


class RelayParse(unittest.TestCase):
    def test_icon_follows_handoff_not_a_lan_probe(self):
        """Green when a bot is paired and the last request did not fail. Red on timeout or POST failure.
        No paired bot shows the dim unpaired icon. The poll does not open a socket to a relay host."""
        self.assertEqual(gs.relay_icon(None), {"ok": None, "detail": ""})
        self.assertEqual(gs.relay_icon({"ok": True}), {"ok": None, "detail": ""})  # unauthenticated health
        idle = {"ok": True, "bots": {"paired": True, "ok": True}}
        down = {"ok": True, "bots": {"paired": True, "ok": False}}
        bare = {"ok": True, "bots": {"paired": False, "ok": None}}
        self.assertEqual(gs.relay_icon(idle), {"ok": True, "detail": "ok"})
        self.assertEqual(gs.relay_icon(down), {"ok": False, "detail": "down"})
        self.assertEqual(gs.relay_icon(bare), {"ok": None, "detail": ""})
        self.assertNotIn("example", gs.relay_icon({"bots": {"paired": True, "ok": False}, "host": "relay.example.invalid"})["detail"])
        calls = []

        def fetch():
            calls.append("health")
            return down

        st = gs.check_relay(fetch)
        self.assertEqual(calls, ["health"])
        self.assertEqual(st, {"ok": False, "detail": "down"})
        self.assertNotIn("relay", st["detail"])
        self.assertEqual(gs.check_relay(lambda: None), {"ok": None, "detail": "", "misses": 1})
        kept = gs.check_relay(lambda: None, {"ok": True, "detail": "ok"})
        self.assertEqual(kept["ok"], True)
        self.assertEqual(kept["detail"], "ok")
        self.assertEqual(kept["misses"], 1)
        self.assertNotIn("misses", gs.check_relay(lambda: idle))
        names = gs.status_icon_names({"net": {"kind": "wifi", "up": True}, "relay": st,
                                       "grok": {"state": "unreachable", "cause": "relay"}, "mic": {}})
        self.assertIn("grok-unreachable", names)
        self.assertFalse(any(n.startswith("relay-") for n in names))
        green = gs.status_icon_names({"net": {}, "relay": gs.relay_icon(idle),
                                       "grok": {"state": "online", "cause": None}, "mic": {}})
        self.assertIn("grok-online", green)
        self.assertFalse(any(n.startswith("relay-") for n in green))
        boot = gs.status_icon_names({"net": {}, "relay": gs.check_relay(lambda: None), "mic": {}})
        self.assertIn("grok-checking", boot)
        self.assertFalse(any(n.startswith("relay-") for n in boot))
        with open(gs.__file__) as fh:
            src = fh.read()
        relay_fn = src.split("def fetch_relay_health", 1)[1].split("\ndef read_relay", 1)[0]
        self.assertNotIn("create_connection", relay_fn)
        self.assertNotIn("parse_relay_target", src)
        self.assertIn("127.0.0.1", gs.relay_health_url())
        self.assertNotIn("example", gs.relay_health_url())


class MicParse(unittest.TestCase):
    def test_ok_stale_down_missing(self):
        now = 1_700_000_000.0

        def fresh():
            return {"level_at": now - 5, "level_dbfs": -12.0}

        self.assertEqual(gs.check_mic(now, fresh, CARDS_OK), {"ok": True, "detail": "ok"})
        self.assertEqual(gs.check_mic(now, fresh, "line with m0 only\n")["ok"], True)
        self.assertTrue(gs.check_mic(now, lambda: {"level_at": now - 30}, CARDS_OK)["ok"])  # within 30s
        stalled = gs.check_mic(now, lambda: {"level_at": now - 31, "level_dbfs": -40}, CARDS_OK, stale_s=30)
        self.assertEqual(stalled, {"ok": False, "detail": "capture stalled"})
        self.assertEqual(gs.check_mic(now, lambda: None, CARDS_OK), {"ok": False, "detail": "wake listener down"})

        def boom():
            raise OSError("down")

        self.assertEqual(gs.check_mic(now, boom, CARDS_OK)["detail"], "wake listener down")
        missing = gs.check_mic(now, fresh, CARDS_MISSING)
        self.assertEqual(missing, {"ok": False, "detail": "speakerphone missing"})
        self.assertEqual(gs.check_mic(now, fresh, CARDS_OK, match="no-such-device")["detail"], "speakerphone missing")
        self.assertTrue(gs.check_mic(now, fresh, "vendor ACME-mic\n", match="acme")["ok"])


class IconLoad(unittest.TestCase):
    def _colors(self, surf):
        teal = amber = red = False
        for y in range(surf.get_height()):
            for x in range(surf.get_width()):
                c = surf.get_at((x, y))
                if c.a < 200:
                    continue
                if c.r > 220 and c.g < 110 and c.b < 110:
                    red = True
                if c.g > 160 and c.b > 140 and c.r < 120:
                    teal = True
                if c.r > 220 and 150 < c.g < 220 and c.b < 110:
                    amber = True
        return teal, amber, red

    def test_fallback_when_file_missing(self):
        with tempfile.TemporaryDirectory() as empty:
            bad = gs.load_status_icon("mic-problem", 22, directory=empty)
            good = gs.load_status_icon("mic-ok", 22, directory=empty)
            shipped = gs.load_status_icon("wifi-connected", 22)
            missing = gs.load_status_icon("wifi-connected", 22, directory=empty)
        self.assertEqual(bad.get_size(), (22, 22))
        self.assertEqual(good.get_size(), (22, 22))
        teal, amber, red = self._colors(bad)
        self.assertTrue(amber)
        self.assertFalse(red)
        teal, amber, red = self._colors(good)
        self.assertTrue(teal)
        self.assertFalse(amber)
        self.assertFalse(red)
        relay = gs.load_status_icon("relay-disconnected", 22, directory=empty)
        teal, amber, red = self._colors(relay)
        self.assertTrue(red)
        self.assertFalse(amber)
        self.assertNotEqual(pygame.image.tobytes(shipped, "RGBA"), pygame.image.tobytes(missing, "RGBA"))
        svg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "assets", "status", "relay-disconnected.svg")
        with open(svg_path) as fh:
            svg = fh.read()
        self.assertIn("#FF5A52", svg)
        self.assertNotIn("#F5C04A", svg)


class IconDraw(unittest.TestCase):
    def test_only_top_right_on_1080p(self):
        w, h = 1920, 1080
        surf = pygame.Surface((w, h))
        surf.fill(gs.BG)
        L = gs.Layout(w, h)
        st = {
            "net": {"kind": "wifi", "up": True},
            "relay": {"ok": True, "detail": "ok"},
            "mic": {"ok": False, "detail": "capture stalled"},
            "at": 0,
        }
        rect = gs.draw_status_icons(surf, L, status=st)
        region = pygame.Rect(w // 2, 0, w // 2, h // 5)
        self.assertGreaterEqual(rect.right, w - max(6, round(16 * L.s)) - 1)  # in the corner
        self.assertGreaterEqual(rect.height, 28)
        self.assertTrue(region.contains(rect), f"{rect} outside {region}")
        bounds = _ink_bounds(surf, gs.BG)
        self.assertIsNotNone(bounds)
        minx, miny, maxx, maxy = bounds
        self.assertGreaterEqual(minx, region.left)
        self.assertGreaterEqual(miny, region.top)
        self.assertLess(maxx, region.right)
        self.assertLess(maxy, region.bottom)
        # the Grok icon is always drawn, so a relay value does not change the row width
        unpaired = dict(st, relay={"ok": None, "detail": ""})
        surf2 = pygame.Surface((w, h))
        surf2.fill(gs.BG)
        rect2 = gs.draw_status_icons(surf2, L, status=unpaired)
        self.assertEqual(rect2.width, rect.width)
        self.assertEqual(rect2.right, rect.right)
        plain = gs.status_icon_names(st)
        self.assertNotIn("update-available", plain)
        wider = gs.draw_status_icons(pygame.Surface((w, h)), L, status=dict(st, update=True))
        self.assertGreater(wider.width, rect.width)
        self.assertEqual(wider.right, rect.right)
        self.assertIn("update-available", gs.status_icon_names(dict(st, update=True)))

    def test_clock_sits_left_of_the_icons(self):
        surf = pygame.Surface((1920, 1080))
        surf.fill(gs.BG)
        L = gs.Layout(1920, 1080)
        now = time.strptime("2026-10-18 19:42", "%Y-%m-%d %H:%M")
        prev = gs.status_snapshot()
        gs.store_status({
            "net": {"kind": "ethernet", "up": True},
            "relay": {"ok": False, "detail": "down"},
            "mic": {"ok": True, "detail": "ok"},
            "at": 1,
        })
        try:
            gs.draw_topbar_clock(surf, L, now)
        finally:
            gs.store_status(prev)
        self.assertIsNotNone(gs.LAST_STATUS_RECT)
        self.assertFalse(gs.LAST_STATUS_RECT.colliderect(gs.LAST_TOPBAR_RECT))
        self.assertLessEqual(gs.LAST_TOPBAR_RECT.right, gs.LAST_STATUS_RECT.left)


class UpdateIcon(unittest.TestCase):
    def test_only_while_a_newer_version_is_cached(self):
        sha = "a" * 40
        self.assertFalse(gs.update_available({}, "0.12.1"))
        self.assertFalse(gs.update_available({"error": True, "available": "9.0.0", "sha": sha}, "0.12.1"))
        self.assertFalse(gs.update_available({"available": "9.0.0", "sha": "abc", "error": False}, "0.12.1"))
        self.assertFalse(gs.update_available({"available": sha, "sha": sha, "error": False}, "0.12.1"))
        self.assertFalse(gs.update_available({"available": "0.12.1", "sha": sha, "error": False}, "0.12.1"))
        self.assertFalse(gs.update_available({"available": "0.12.0", "sha": sha, "error": False}, "0.12.1"))
        self.assertTrue(gs.update_available({"available": "0.12.2", "sha": sha, "error": False}, "0.12.1"))
        self.assertTrue(gs.update_available({"available": "0.12.2", "sha": sha, "error": False}, "dev"))
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "update-check.json")
            ver = os.path.join(d, "VERSION")
            open(ver, "w").write("0.12.1\n")
            self.assertFalse(gs.read_update_available(os.path.join(d, "missing.json"), ver))
            open(cache, "w").write(json.dumps({"available": "0.12.1", "sha": sha, "error": False}))
            self.assertFalse(gs.read_update_available(cache, ver))
            open(cache, "w").write(json.dumps({"available": "0.12.2", "sha": sha, "error": False}))
            self.assertTrue(gs.read_update_available(cache, ver))
        names = gs.status_icon_names({"net": {}, "relay": {}, "mic": {}})
        self.assertNotIn("update-available", names)
        self.assertEqual(gs.status_icon_names({"net": {}, "relay": {}, "mic": {}, "update": False})[-1], "grok-checking")
        shown = gs.status_icon_names({"net": {"kind": "wifi", "up": True}, "grok": {"state": "online", "cause": None},
                                       "mic": {"ok": True}, "update": True})
        self.assertEqual(shown[-1], "update-available")
        self.assertEqual(shown[:3], ["wifi-connected", "grok-online", "mic-ok"])
        self.assertFalse(any(n.startswith("relay-") for n in shown))

    def test_icon_matches_the_set_and_makes_no_sound(self):
        svg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "assets", "status", "update-available.svg")
        svg = open(svg_path).read()
        self.assertIn('viewBox="0 0 24 24"', svg)
        self.assertIn('stroke="#3FD3B6"', svg)
        self.assertIn('stroke-width="2"', svg)
        self.assertIn('stroke-linecap="round"', svg)
        icon = gs.load_status_icon("update-available", 24)
        self.assertEqual(icon.get_size(), (24, 24))
        teal = False
        for y in range(24):
            for x in range(24):
                c = icon.get_at((x, y))
                if c.a >= 200 and c.g > 160 and c.b > 140 and c.r < 120:
                    teal = True
        self.assertTrue(teal)
        with tempfile.TemporaryDirectory() as empty:
            fallback = gs.load_status_icon("update-available", 24, directory=empty)
        self.assertEqual(fallback.get_size(), (24, 24))
        src = open(gs.__file__).read()
        self.assertNotIn("aplay", src.split("def read_update_available", 1)[1].split("def collect_status", 1)[0])
        prev = gs.status_snapshot()
        try:
            self.assertTrue(gs.store_status(dict(prev, update=not prev.get("update"))))
        finally:
            gs.store_status(prev)


if __name__ == "__main__":
    unittest.main()


class RelayIconPairedTests(__import__("unittest").TestCase):
    def setUp(self):
        import importlib, os, sys
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
        self.g = importlib.import_module("grok_station")

    def test_paired_no_history_is_ok_and_not_its_own_icon(self):
        icon = self.g.relay_icon({"ok": True, "key": True, "bots": {"paired": True, "ok": True}})
        self.assertEqual(icon, {"ok": True, "detail": "ok"})
        names = self.g.status_icon_names({"net": {}, "relay": icon, "grok": {"state": "online", "cause": None}})
        self.assertIn("grok-online", names)
        self.assertFalse(any(n.startswith("relay-") for n in names))

    def test_paired_unknown_ok_still_green(self):
        self.assertEqual(self.g.relay_icon({"ok": True, "bots": {"paired": True, "ok": None}})["ok"], True)

    def test_paired_timeout_is_a_relay_fault_on_the_grok_icon(self):
        icon = self.g.relay_icon({"ok": True, "bots": {"paired": True, "ok": False}})
        self.assertEqual(icon["ok"], False)
        names = self.g.status_icon_names({"net": {}, "relay": icon, "grok": {"state": "unreachable", "cause": "relay"}})
        self.assertIn("grok-unreachable", names)
        self.assertNotIn("relay-disconnected", names)

    def test_zero_paired_is_not_a_cluster_icon(self):
        icon = self.g.relay_icon({"ok": True, "bots": {"paired": False, "ok": None}})
        self.assertIsNone(icon["ok"])
        names = self.g.status_icon_names({"net": {}, "relay": icon})
        self.assertIn("grok-checking", names)
        self.assertFalse(any(n.startswith("relay-") for n in names))

    def test_empty_status_shows_checking_not_a_relay_icon(self):
        names = self.g.status_icon_names({})
        self.assertEqual(names[0][:5], "wifi-")
        self.assertIn("grok-checking", names)
        self.assertFalse(any(n.startswith("relay-") for n in names))

    def test_unpaired_icon_is_dim_not_red(self):
        import os
        svg = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "status", "relay-unpaired.svg")).read()
        self.assertNotIn("#FF5A52", svg)
        self.assertNotIn("#3FD3B6", svg)
        self.assertIn("#A89B8C", svg)
        empty = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-such-dir")
        self.assertIsNotNone(self.g.load_status_icon("relay-unpaired", 22, directory=empty))

    def test_failed_poll_keeps_paired_state_then_faults(self):
        prev = {"ok": True, "detail": "ok"}
        one = self.g.check_relay(lambda: None, prev)
        self.assertEqual(one["ok"], True)
        self.assertEqual(one["misses"], 1)
        two = self.g.check_relay(lambda: None, one)
        self.assertEqual(two["ok"], True)
        self.assertEqual(two["misses"], 2)
        three = self.g.check_relay(lambda: None, two)
        self.assertEqual(three["ok"], False)
        self.assertEqual(three["detail"], "down")
        self.assertGreaterEqual(three["misses"], 3)
        again = self.g.check_relay(lambda: {"bots": {"paired": True, "ok": True}}, three)
        self.assertEqual(again, {"ok": True, "detail": "ok"})
        self.assertNotIn("misses", again)
        # a bool is not a count, so it does not skip ahead to a fault
        skipped = self.g.check_relay(lambda: None, {"ok": True, "detail": "ok", "misses": True})
        self.assertEqual(skipped["ok"], True)
        self.assertEqual(skipped["misses"], 1)
        unpaired = {"ok": None, "detail": "", "misses": 9}
        still = self.g.check_relay(lambda: None, unpaired)
        self.assertIsNone(still["ok"])
        self.assertGreaterEqual(still["misses"], 9)
        down = self.g.check_relay(lambda: None, {"ok": False, "detail": "down"})
        self.assertEqual(down["ok"], False)
        self.assertEqual(down["misses"], 1)


def _closed_sock():
    class Sock:
        def close(self):
            pass
    return Sock()


def _rgb(surf, pred):
    for y in range(surf.get_height()):
        for x in range(surf.get_width()):
            c = surf.get_at((x, y))
            if c.a >= 200 and pred(c):
                return True
    return False


class GrokIcons(unittest.TestCase):
    def test_panel_size_and_cluster_order(self):
        L = gs.Layout(2560, 1706)
        self.assertEqual(gs.status_icon_px(L.s), 59)
        self.assertEqual(max(1, int(round(8 * L.s))), 11)
        self.assertEqual(L(36), 48)
        self.assertEqual(gs.status_icon_px(1), 44)
        self.assertEqual(gs.status_icon_px(0.5), 28)
        names = gs.status_icon_names({
            "net": {"kind": "ethernet", "up": False},
            "grok": {"state": "dim", "cause": None},
            "mic": {"ok": False, "detail": "wake listener down"},
            "update": True,
        })
        self.assertEqual(names, ["ethernet-disconnected", "grok-dim", "mic-problem", "update-available"])
        self.assertFalse(any(n.startswith("relay-") for n in names))
        # the voice listener stays on the mic icon; it does not move the Grok icon
        quiet = gs.status_icon_names({
            "net": {"kind": "wifi", "up": True},
            "grok": {"state": "online", "cause": None},
            "mic": {"ok": False},
        })
        self.assertEqual(quiet, ["wifi-connected", "grok-online", "mic-problem"])
        self.assertEqual(gs.grok_icon_name({"state": "teal"}), "grok-checking")
        self.assertEqual(gs.grok_icon_name(None), "grok-checking")
        self.assertEqual(gs.status_icon_names({"net": {"kind": "wifi", "up": True}, "mic": {}})[1], "grok-checking")

    def test_svg_shapes_and_fallback_pixels(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "status")

        def svg(name):
            return open(os.path.join(root, name + ".svg")).read()

        online, slash, bang, checking, dim = (svg(n) for n in (
            "grok-online", "grok-no-internet", "grok-unreachable", "grok-checking", "grok-dim"))
        for text in (online, slash, bang, checking, dim):
            self.assertIn('viewBox="0 0 24 24"', text)
            self.assertIn('stroke-width="2"', text)
            self.assertIn('stroke-linecap="round"', text)
            self.assertIn('stroke-linejoin="round"', text)
            self.assertNotIn("<animate", text)
            self.assertNotIn("<text", text)
        self.assertIn('stroke="#3FD3B6"', online)
        self.assertIn('stroke="#F5C04A"', slash)
        self.assertIn('stroke="#0D0B09"', slash)
        self.assertIn('stroke-width="5"', slash)
        self.assertIn('fill="#F5C04A"', bang)
        self.assertIn('stroke="#0D0B09"', bang)
        self.assertIn('stroke="#A89B8C"', checking)
        self.assertNotIn('opacity="0.75"', checking)
        self.assertIn('opacity="0.75"', dim)
        self.assertIn('stroke="#A89B8C"', dim)
        self.assertNotIn("#3FD3B6", dim)
        self.assertNotIn("#F5C04A", dim)
        self.assertEqual(len({online, slash, bang, checking, dim}), 5)
        loaded = gs.load_status_icon("grok-online", 48)
        self.assertTrue(_rgb(loaded, lambda c: c.g > 160 and c.b > 140 and c.r < 120))
        self.assertTrue(_rgb(gs.load_status_icon("grok-no-internet", 48), lambda c: c.r > 220 and 150 < c.g < 220 and c.b < 110))
        self.assertTrue(_rgb(gs.load_status_icon("grok-unreachable", 48), lambda c: c.r > 220 and 150 < c.g < 220 and c.b < 110))
        with tempfile.TemporaryDirectory() as empty:
            dim_fb = gs.load_status_icon("grok-dim", 36, directory=empty)
            on_fb = gs.load_status_icon("grok-online", 36, directory=empty)
            slash_fb = gs.load_status_icon("grok-no-internet", 36, directory=empty)
            bang_fb = gs.load_status_icon("grok-unreachable", 36, directory=empty)
            chk_fb = gs.load_status_icon("grok-checking", 36, directory=empty)
            unknown = gs.load_status_icon("grok-nope", 36, directory=empty)
        dim_c, teal, amber, bg, muted = (0x81, 0x77, 0x6B), (0x3F, 0xD3, 0xB6), (0xF5, 0xC0, 0x4A), (0x0D, 0x0B, 0x09), (0xA8, 0x9B, 0x8C)
        exact = lambda surf, rgb: _rgb(surf, lambda c: (c.r, c.g, c.b) == rgb)
        self.assertTrue(exact(dim_fb, dim_c))
        self.assertTrue(exact(on_fb, teal))
        self.assertTrue(exact(slash_fb, amber))
        self.assertTrue(exact(slash_fb, bg))
        self.assertTrue(exact(bang_fb, amber))
        self.assertTrue(exact(bang_fb, bg))
        self.assertTrue(exact(chk_fb, muted))
        self.assertFalse(exact(unknown, teal))


class GrokState(unittest.TestCase):
    NOW = 1_000_000.0
    NET = {"kind": "wifi", "up": True}

    def _p(self, at=None, internet=True, grok=True, inet_fails=0, grok_fails=0, shown=None, interval=60):
        p = {"at": self.NOW if at is None else at, "interval": interval, "internet": internet, "grok": grok,
             "internet_fails": inet_fails, "grok_fails": grok_fails}
        if shown is not None:
            p["shown"] = shown
        return p

    def test_every_state(self):
        ok = {"ok": True, "detail": "ok"}
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(), self.NOW), {"state": "online", "cause": None})
        self.assertEqual(gs.grok_state(self.NET, {"ok": None}, self._p(), self.NOW), {"state": "online", "cause": None})
        self.assertEqual(gs.grok_state({"up": False}, {"ok": False}, self._p(internet=False, inet_fails=4), self.NOW),
                         {"state": "dim", "cause": None})
        self.assertEqual(gs.grok_state(self.NET, ok, {}, self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(internet=False, grok=None, inet_fails=2), self.NOW)["state"],
                         "no-internet")
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(grok=False, grok_fails=2), self.NOW),
                         {"state": "unreachable", "cause": "service"})
        self.assertEqual(gs.grok_state(self.NET, {"ok": False}, self._p(), self.NOW),
                         {"state": "unreachable", "cause": "relay"})
        self.assertEqual(gs.grok_state(self.NET, {"ok": False}, self._p(grok=False, grok_fails=2), self.NOW),
                         {"state": "unreachable", "cause": "both"})
        # internet down wins over a relay fault
        self.assertEqual(gs.grok_state(self.NET, {"ok": False}, self._p(internet=False, grok=None, inet_fails=2), self.NOW)["state"],
                         "no-internet")

    def test_stale_unknown_and_raised_are_checking(self):
        ok = {"ok": True}
        fresh = self.NOW - 180
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(at=fresh), self.NOW)["state"], "online")
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(at=fresh - 0.01), self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state(self.NET, ok, {"internet": True, "grok": True}, self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state(None, ok, self._p(), self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state({"up": 1}, ok, self._p(), self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state({"up": "yes"}, ok, self._p(), self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state(self.NET, {"ok": "yes"}, self._p(), self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state(self.NET, [], self._p(), self.NOW)["state"], "checking")
        stamped = self._p()
        stamped["at"] = True
        self.assertEqual(gs.grok_state(self.NET, ok, stamped, self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(interval="fast"), self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(interval=True), self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(), None)["state"], "checking")
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(), True)["state"], "checking")
        bad_fails = self._p(internet=False, inet_fails=True, shown="online")
        self.assertEqual(gs.grok_state(self.NET, ok, bad_fails, self.NOW)["state"], "checking")
        uncounted = self._p(internet=False, inet_fails=0, shown="online")
        self.assertEqual(gs.grok_state(self.NET, ok, uncounted, self.NOW)["state"], "checking")

        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")

        self.assertEqual(gs.grok_state(Boom(up=True), ok, self._p(), self.NOW)["state"], "checking")
        # net down is decided before a bad probe or relay is read
        self.assertEqual(gs.grok_state({"up": False}, Boom(), Boom(), self.NOW), {"state": "dim", "cause": None})

    def test_debounce_and_recovery(self):
        ok = {"ok": True}
        one = self._p(internet=False, grok=None, inet_fails=1, shown="online")
        self.assertEqual(gs.grok_state(self.NET, ok, one, self.NOW)["state"], "online")
        boot = self._p(internet=False, grok=None, inet_fails=1)
        self.assertEqual(gs.grok_state(self.NET, ok, boot, self.NOW)["state"], "checking")
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(internet=False, grok=None, inet_fails=1, shown="checking"), self.NOW)["state"],
                         "checking")
        two = self._p(internet=False, grok=None, inet_fails=2, shown="online")
        self.assertEqual(gs.grok_state(self.NET, ok, two, self.NOW)["state"], "no-internet")
        held = self._p(grok=False, grok_fails=1, shown="online")
        self.assertEqual(gs.grok_state(self.NET, ok, held, self.NOW)["state"], "online")
        service = self._p(grok=False, grok_fails=2, shown="online")
        self.assertEqual(gs.grok_state(self.NET, ok, service, self.NOW), {"state": "unreachable", "cause": "service"})
        pending_relay = self._p(internet=False, grok=None, inet_fails=1, shown="online")
        self.assertEqual(gs.grok_state(self.NET, {"ok": False}, pending_relay, self.NOW),
                         {"state": "unreachable", "cause": "relay"})
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(shown="no-internet"), self.NOW)["state"], "online")
        self.assertEqual(gs.grok_state(self.NET, ok, self._p(shown="unreachable"), self.NOW)["state"], "online")


class GrokProbes(unittest.TestCase):
    def test_targets_are_literals_and_a_name_is_not_resolved(self):
        import ipaddress
        hosts = [h for h, port in gs.net_probe_targets("")]
        self.assertEqual(hosts[0], "1.1.1.1")
        self.assertIn("8.8.8.8", hosts)
        for host, port in gs.net_probe_targets("example.invalid:443"):
            ipaddress.ip_address(host)
            self.assertNotEqual(host, "example.invalid")
        self.assertEqual(gs.net_probe_targets("8.8.8.8:443"), [("8.8.8.8", 443)])
        self.assertEqual(gs.net_probe_targets("1.1.1.1:notaport")[0], ("1.1.1.1", 443))
        calls = []

        def connect(addr, timeout):
            calls.append(addr[0])
            return _closed_sock()

        self.assertTrue(gs.probe_internet(connect, targets=[("example.invalid", 443), ("1.1.1.1", 443)]))
        self.assertEqual(calls, ["1.1.1.1"])

    def test_connect_is_closed_and_failures_do_not_log_an_address(self):
        closed = []

        class Sock:
            def close(self):
                closed.append(1)

        calls = []

        def connect(addr, timeout):
            calls.append((addr, timeout))
            if addr[0] == "1.1.1.1" and len(calls) == 1:
                raise OSError("down")
            return Sock()

        saved = set(gs._warned)
        gs._warned.clear()
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                self.assertTrue(gs.probe_internet(connect, timeout=2, targets=[("1.1.1.1", 443), ("8.8.8.8", 443)]))
                self.assertEqual(closed, [1])
                self.assertEqual(calls[0][1], 2)
                self.assertNotIn("OSError", buf.getvalue())

                def fail(addr, timeout):
                    raise TimeoutError("1.1.1.1 timed out")

                self.assertFalse(gs.probe_internet(fail, targets=[("1.1.1.1", 443), ("8.8.8.8", 443)]))
                self.assertFalse(gs.probe_internet(fail, targets=[("8.8.8.8", 443)]))
        finally:
            gs._warned.clear()
            gs._warned.update(saved)
        text = buf.getvalue()
        self.assertIn("TimeoutError", text)
        self.assertEqual(text.count("TimeoutError"), 1)
        self.assertNotIn("1.1.1.1", text)
        self.assertNotIn("8.8.8.8", text)

    def test_grok_http_rules_and_no_url_in_the_log(self):
        self.assertTrue(gs.probe_grok(lambda u, t: 200, url="https://api.x.ai/"))
        self.assertTrue(gs.probe_grok(lambda u, t: 401, url="https://api.x.ai/"))
        self.assertTrue(gs.probe_grok(lambda u, t: 403, url="https://api.x.ai/"))
        self.assertTrue(gs.probe_grok(lambda u, t: 404, url="https://api.x.ai/"))
        self.assertTrue(gs.probe_grok(lambda u, t: 302, url="https://api.x.ai/"))
        self.assertFalse(gs.probe_grok(lambda u, t: 500, url="https://api.x.ai/"))
        self.assertFalse(gs.probe_grok(lambda u, t: 503, url="https://api.x.ai/"))
        calls = []
        self.assertFalse(gs.probe_grok(lambda u, t: calls.append(u) or 200, url="http://api.x.ai/"))
        self.assertIsNone(gs.https_probe_url("https://user:pw@api.x.ai/"))
        self.assertFalse(gs.probe_grok(lambda u, t: calls.append(u) or 200, url="https://user:pw@api.x.ai/"))
        self.assertEqual(calls, [])
        saved = set(gs._warned)
        gs._warned.clear()
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                self.assertFalse(gs.probe_grok(lambda u, t: (_ for _ in ()).throw(OSError("https://api.x.ai/")), url="https://api.x.ai/"))
                self.assertFalse(gs.probe_grok(lambda u, t: (_ for _ in ()).throw(OSError("again")), url="https://api.x.ai/"))
        finally:
            gs._warned.clear()
            gs._warned.update(saved)
        text = buf.getvalue()
        self.assertIn("OSError", text)
        self.assertEqual(text.count("OSError"), 1)
        self.assertNotIn("http", text)
        self.assertNotIn("api.x.ai", text)

    def test_fetch_sends_no_authorization_and_a_redirect_is_not_followed(self):
        import urllib.error
        import urllib.request
        captured = {}

        class Resp:
            status = 204

            def read(self, n):
                captured["n"] = n
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        real = urllib.request.build_opener

        def fake_build(*handlers):
            captured["handlers"] = handlers
            opener = real(*handlers)

            def _open(req, timeout=None):
                captured["headers"] = {k.lower(): v for k, v in req.header_items()}
                captured["timeout"] = timeout
                return Resp()

            opener.open = _open
            return opener

        urllib.request.build_opener = fake_build
        try:
            self.assertEqual(gs.fetch_grok_https("https://api.x.ai/", 3), 204)
        finally:
            urllib.request.build_opener = real
        self.assertEqual(captured["n"], 1024)
        self.assertEqual(captured["timeout"], 3)
        self.assertNotIn("authorization", captured["headers"])
        self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) for h in captured["handlers"]))
        self.assertTrue(any(isinstance(h, gs._NoRedirect) for h in captured["handlers"]))
        req = urllib.request.Request("https://api.x.ai/")
        try:
            gs._NoRedirect().http_error_302(req, io.BytesIO(b"x"), 302, "Found", {"Location": "https://api.x.ai/v1"})
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
            self.assertNotIn("v1", str(e))
        else:
            self.fail("redirect was swallowed")

    def test_net_down_does_not_probe_and_polling_follows_the_state(self):
        self.assertEqual(gs.grok_poll_s("10"), 30.0)
        self.assertEqual(gs.grok_poll_s("45"), 45.0)
        self.assertEqual(gs.grok_poll_s("nope"), 60.0)
        self.assertEqual(gs.grok_probe_every("online", 60, 12), 60)
        self.assertEqual(gs.grok_probe_every("checking", 60, 12), 12)
        self.assertEqual(gs.grok_probe_every("no-internet", 60, 12), 12)
        self.assertEqual(gs.grok_probe_every("unreachable", 60, 12), 12)
        self.assertEqual(gs.grok_probe_every("dim", 60, 12), 12)
        self.assertFalse(gs.probe_due({"at": 1000, "shown": "online"}, 1059, 60, 12))
        self.assertTrue(gs.probe_due({"at": 1000, "shown": "online"}, 1060, 60, 12))
        self.assertFalse(gs.probe_due({"at": 1000, "shown": "checking"}, 1011, 60, 12))
        self.assertTrue(gs.probe_due({"at": 1000, "shown": "checking"}, 1012, 60, 12))
        self.assertTrue(gs.probe_due({}, 0, 60, 12))
        self.assertTrue(gs.probe_due({"at": True, "shown": "online"}, 5000, 60, 12))
        calls = []
        prev = {"at": 10, "internet": True, "grok": True, "internet_fails": 0, "grok_fails": 0, "shown": "online", "interval": 60}
        out = gs.sample_probe(prev, {"up": False}, 99, lambda *a: calls.append("c"), lambda *a: calls.append("g"))
        self.assertEqual(calls, [])
        self.assertEqual(out["at"], 10)
        self.assertIs(out["internet"], True)
        self.assertIs(out["grok"], True)

        def down(addr, timeout):
            calls.append("c")
            raise OSError("down")

        def grok_up(url, timeout):
            calls.append("g")
            return 200

        missed = gs.sample_probe({"grok_fails": 1}, {"up": True}, 50, down, grok_up)
        self.assertEqual(calls, ["c", "c"])  # primary, then the public fallback; the Grok fetch is not called
        self.assertNotIn("g", calls)
        self.assertIs(missed["internet"], False)
        self.assertIsNone(missed["grok"])
        self.assertEqual(missed["internet_fails"], 1)
        self.assertEqual(missed["grok_fails"], 1)
        ok = gs.sample_probe({"internet_fails": 2, "grok_fails": 2}, {"up": True}, 50, lambda a, t: _closed_sock(), lambda u, t: 401)
        self.assertIs(ok["internet"], True)
        self.assertIs(ok["grok"], True)
        self.assertEqual(ok["internet_fails"], 0)
        self.assertEqual(ok["grok_fails"], 0)
        bad = gs.sample_probe({}, {"up": True}, 50, lambda a, t: _closed_sock(), lambda u, t: 500)
        self.assertIs(bad["grok"], False)
        self.assertEqual(bad["grok_fails"], 1)

    def test_tick_debounces_and_skips_a_down_network(self):
        calls = []
        relay = {"ok": True, "detail": "ok"}
        net = {"kind": "wifi", "up": True}

        def connect(addr, timeout):
            calls.append(addr[0])
            raise OSError("down")

        def fetch(url, timeout):
            calls.append("grok")
            return 200

        probe, grok = gs.tick_grok({}, net, relay, 1000, connect, fetch)
        self.assertEqual(calls, ["1.1.1.1", "8.8.8.8"])
        self.assertEqual(grok["state"], "checking")
        self.assertEqual(probe["internet_fails"], 1)
        self.assertEqual(probe["shown"], "checking")
        probe, grok = gs.tick_grok(probe, net, relay, 1120, connect, fetch)
        self.assertEqual(grok["state"], "no-internet")
        self.assertEqual(probe["internet_fails"], 2)
        self.assertNotIn("grok", calls)

        def up(addr, timeout):
            calls.append("up")
            return _closed_sock()

        probe, grok = gs.tick_grok(probe, net, relay, 1240, up, lambda u, t: 204)
        self.assertEqual(grok, {"state": "online", "cause": None})
        self.assertEqual(probe["shown"], "online")
        quiet = []
        held = {"at": 2000, "interval": 60, "internet": True, "grok": True, "internet_fails": 0, "grok_fails": 0, "shown": "online"}
        probe, grok = gs.tick_grok(held, {"up": False}, relay, 5000, lambda *a: quiet.append("c"), lambda *a: quiet.append("g"))
        self.assertEqual(quiet, [])
        self.assertEqual(probe["at"], 2000)
        self.assertEqual(grok["state"], "dim")
        probe, grok = gs.tick_grok(probe, net, relay, 2001, lambda *a: quiet.append("c"), lambda *a: quiet.append("g"))
        self.assertEqual(quiet, [])
        self.assertEqual(grok["state"], "online")


class GrokSnapshot(unittest.TestCase):
    def setUp(self):
        self.prev = gs.status_snapshot()

    def tearDown(self):
        gs.store_status(self.prev)

    def test_snapshot_is_enums_only(self):
        gs.store_status({
            "net": {"kind": "wifi", "up": True, "addr": "1.1.1.1"},
            "relay": {"ok": True, "detail": "ok", "misses": 2},
            "mic": {"ok": True, "detail": "ok"},
            "update": False,
            "grok": {"state": "online", "cause": "relay", "url": "https://api.x.ai/"},
            "probe": {"url": "https://api.x.ai/", "host": "api.x.ai", "ip": "1.1.1.1"},
            "at": 5,
        })
        snap = gs.status_snapshot()
        blob = json.dumps(snap)
        for banned in ("http", "api.x.ai", "1.1.1.1", "8.8.8.8", "misses", "probe", "url", "host"):
            self.assertNotIn(banned, blob)
        self.assertEqual(snap["grok"], {"state": "online", "cause": None})
        self.assertEqual(set(snap), {"net", "relay", "mic", "update", "grok", "at"})
        self.assertEqual(set(snap["relay"]), {"ok", "detail"})
        self.assertEqual(set(snap["net"]), {"kind", "up"})
        gs.store_status(dict(gs.status_snapshot(), grok={"state": "teal", "cause": "https://api.x.ai/"}))
        # status_snapshot drops probe; store the bad grok on top of a full dict
        gs.store_status({
            "net": {"kind": "wifi", "up": True}, "relay": {"ok": True, "detail": "ok"},
            "mic": {"ok": None, "detail": ""}, "update": False,
            "grok": {"state": "teal", "cause": "https://api.x.ai/"}, "at": 1,
        })
        self.assertEqual(gs.status_snapshot()["grok"], {"state": "checking", "cause": None})
        gs.store_status({
            "net": {"kind": "wifi", "up": True}, "relay": {"ok": False, "detail": "down", "misses": 3},
            "mic": {"ok": None, "detail": ""}, "update": False,
            "grok": {"state": "unreachable", "cause": "both"}, "at": 2,
        })
        self.assertEqual(gs.status_snapshot()["grok"], {"state": "unreachable", "cause": "both"})
        self.assertNotIn("misses", json.dumps(gs.status_snapshot()))

    def test_read_relay_counts_misses_from_the_private_status(self):
        gs.store_status({
            "net": {"kind": "wifi", "up": True},
            "relay": {"ok": True, "detail": "ok", "misses": 2},
            "mic": {"ok": None, "detail": ""},
            "update": False,
            "grok": {"state": "checking", "cause": None},
            "probe": {"url": "https://api.x.ai/"},
            "at": 1,
        })
        old = gs.fetch_relay_health
        gs.fetch_relay_health = lambda timeout=2: None
        try:
            got = gs.read_relay()
        finally:
            gs.fetch_relay_health = old
        self.assertEqual(got["ok"], False)
        self.assertEqual(got["detail"], "down")
        self.assertEqual(got["misses"], 3)
        self.assertNotIn("misses", gs.status_snapshot()["relay"])
        self.assertNotIn("http", json.dumps(gs.status_snapshot()))


def _near(pixel, color, tol):
    return pixel.a > 180 and abs(pixel.r - color.r) <= tol and abs(pixel.g - color.g) <= tol and abs(pixel.b - color.b) <= tol


class VoiceHint(unittest.TestCase):
    def test_segments_colours_quotes_and_l36(self):
        L = gs.Layout(2560, 1706)
        self.assertEqual(L(36), 48)
        px = L(36)
        f = gs.font("body", px)
        self.assertEqual(gs.TIMER_HINT, "Say \u201cstop\u201d to dismiss")
        self.assertEqual(gs.voice_hint_plain([("\u201cstop\u201d", "phrase")]), "\u201cstop\u201d")
        self.assertEqual(gs.voice_hint_plain([('"stop"', "phrase")]), "\u201cstop\u201d")
        self.assertEqual(gs.hint_spans([("Practice", "text")])[0], ("Practice", gs.TEXT))
        self.assertNotIn("\u201c", gs.hint_spans([("Practice", "text")])[0][0])
        self.assertEqual(gs.hint_spans([("Hello", "nope")])[0][1], gs.MUTED)
        plain = gs.voice_hint_plain(gs.RECIPE_HINT_SEGMENTS)
        for phrase in ("next step", "previous step", "close recipe"):
            self.assertIn("\u201c" + phrase + "\u201d", plain)
        surf = pygame.Surface((900, 90))
        surf.fill(gs.BG)
        rect = gs.draw_voice_hint(surf, gs.TIMER_HINT_SEGMENTS, 10, 8, px=px)
        self.assertEqual(rect.h, f.get_height())
        spans = [(t, c) for t, c in gs.hint_spans(gs.TIMER_HINT_SEGMENTS) if t]
        x = 10
        for text, color in spans:
            w = f.size(text)[0]
            hit = False
            for yy in range(rect.top, rect.bottom):
                for xx in range(x + 1, min(x + w - 1, surf.get_width())):
                    if _near(surf.get_at((xx, yy)), color, 30):
                        hit = True
                        break
                if hit:
                    break
            self.assertTrue(hit, text)
            x += w
        self.assertEqual(spans[0][1], gs.MUTED)
        self.assertEqual(spans[1][1], gs.TEXT)

    def test_long_title_keeps_the_muted_tail(self):
        title = "Practice " * 40
        segments = [("Next:", "muted"), (" " + title + " · 6:30", "text"), (" · and 3 more", "muted")]
        surf = pygame.Surface((560, 80))
        surf.fill(gs.BG)
        rect = gs.draw_voice_hint(surf, segments, 0, 0, px=48, max_w=500)
        self.assertLessEqual(rect.right, 500)
        f = gs.font("body", 48)
        tail_w = f.size(" · and 3 more")[0]
        text_hit = muted_hit = False
        for x in range(max(0, rect.right - tail_w + 8), rect.right):
            for y in range(rect.top, rect.bottom):
                c = surf.get_at((x, y))
                if _near(c, gs.TEXT, 24):
                    text_hit = True
                if _near(c, gs.MUTED, 24):
                    muted_hit = True
        self.assertTrue(muted_hit)
        self.assertFalse(text_hit)

    def test_recipe_timer_and_calendar_use_the_helper(self):
        L = gs.Layout(2560, 1706)
        surf = pygame.Surface((L.w, L.h))
        surf.fill(gs.BG)
        calls = []
        real = gs.draw_voice_hint

        def wrap(surface, segments, x, y, px=36, max_w=None):
            calls.append((list(segments), px))
            return real(surface, segments, x, y, px=px, max_w=max_w)

        old_chips, old_epoch = list(gs.TIMER_CHIPS), gs.TIMER_EPOCH
        gs.draw_voice_hint = wrap
        try:
            gs.draw_recipe(surf, L, {"v": 1, "type": "recipe", "title": "Soup", "servings": 4,
                                      "ingredients": ["onion"], "steps": ["Chop."], "step": 1})
            self.assertEqual(calls[-1][0], list(gs.RECIPE_HINT_SEGMENTS))
            self.assertEqual(calls[-1][1], L(36))
            events = [{"time": "6:30", "title": "Practice"}] + [{"time": "7:00", "title": "Dinner"}] * 7
            gs.draw_calendar(surf, L, {"v": 1, "type": "calendar", "date": "2026-10-22", "events": events},
                             pygame.Rect(L(128), L(800), L.w - L(256), L(700)))
            segs, px = calls[-1]
            self.assertEqual(px, L(36))
            self.assertEqual(segs[0], ("Next:", "muted"))
            self.assertEqual(segs[1][1], "text")
            self.assertIn("Practice", segs[1][0])
            self.assertNotIn("\u201c", segs[1][0])
            self.assertEqual(segs[-1][1], "muted")
            self.assertIn("more", segs[-1][0])
            gs.draw_calendar(surf, L, {"v": 1, "type": "calendar", "date": "2026-10-22", "events": []},
                             pygame.Rect(L(128), L(800), L.w - L(256), L(400)))
            self.assertEqual(calls[-1][0], [("Nothing on this day", "muted")])
            self.assertEqual(calls[-1][1], L(36))
            gs.TIMER_EPOCH = 1_700_000_000
            gs.TIMER_CHIPS[:] = [{"id": "aaaa01", "label": "pasta", "ends_at_epoch": gs.TIMER_EPOCH - 1,
                                  "duration": 60, "ringing": True}]
            gs.draw_timers(surf, L, time.strptime("2026-10-18 19:42", "%Y-%m-%d %H:%M"))
            self.assertEqual(calls[-1][0], list(gs.TIMER_HINT_SEGMENTS))
            self.assertEqual(calls[-1][1], L(36))
        finally:
            gs.draw_voice_hint = real
            gs.TIMER_EPOCH = old_epoch
            gs.TIMER_CHIPS[:] = old_chips
