#!/usr/bin/env python3
"""Status-icon checks: pure parsers, fallback glyphs, and top-right placement. No network or hardware."""
import os, sys, tempfile, time, unittest

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
        No paired bot stays hidden. The poll does not open a socket to a relay host."""
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
        self.assertEqual(gs.check_relay(lambda: None), {"ok": None, "detail": ""})
        kept = gs.check_relay(lambda: None, {"ok": True, "detail": "ok"})
        self.assertEqual(kept, {"ok": True, "detail": "ok"})
        names = gs.status_icon_names({"net": {"kind": "wifi", "up": True}, "relay": st, "mic": {}})
        self.assertIn("relay-disconnected", names)
        self.assertNotIn("relay-connected", names)
        green = gs.status_icon_names({"net": {}, "relay": gs.relay_icon(idle), "mic": {}})
        self.assertIn("relay-connected", green)
        hidden = gs.status_icon_names({"net": {}, "relay": gs.relay_icon(bare), "mic": {}})
        self.assertFalse(any(n.startswith("relay") for n in hidden))
        with open(gs.__file__) as fh:
            src = fh.read()
        self.assertNotIn("create_connection", src)
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
        # an unconfigured relay is not drawn, so the row is narrower
        hidden = dict(st, relay={"ok": None, "detail": ""})
        surf2 = pygame.Surface((w, h))
        surf2.fill(gs.BG)
        rect2 = gs.draw_status_icons(surf2, L, status=hidden)
        self.assertLess(rect2.width, rect.width)
        self.assertEqual(rect2.right, rect.right)

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


if __name__ == "__main__":
    unittest.main()


class RelayIconPairedTests(__import__("unittest").TestCase):
    def setUp(self):
        import importlib, os, sys
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
        self.g = importlib.import_module("grok_station")

    def test_paired_no_history_green(self):
        st = {"net": {}, "relay": self.g.relay_icon({"ok": True, "key": True, "bots": {"paired": True, "ok": True}})}
        self.assertIn("relay-connected", self.g.status_icon_names(st))

    def test_paired_unknown_ok_still_green(self):
        self.assertEqual(self.g.relay_icon({"ok": True, "bots": {"paired": True, "ok": None}})["ok"], True)

    def test_paired_timeout_red(self):
        st = {"net": {}, "relay": self.g.relay_icon({"ok": True, "bots": {"paired": True, "ok": False}})}
        self.assertIn("relay-disconnected", self.g.status_icon_names(st))

    def test_zero_paired_hidden(self):
        st = {"net": {}, "relay": self.g.relay_icon({"ok": True, "bots": {"paired": False, "ok": None}})}
        names = self.g.status_icon_names(st)
        self.assertNotIn("relay-connected", names)
        self.assertNotIn("relay-disconnected", names)

    def test_failed_poll_keeps_paired_state(self):
        prev = {"ok": True, "detail": "ok"}
        self.assertEqual(self.g.check_relay(lambda: None, prev)["ok"], True)
