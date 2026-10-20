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
    def test_env_parse_and_none(self):
        calls = []

        def connect(host, port, timeout):
            calls.append((host, port, timeout))
            return True

        self.assertEqual(gs.check_relay({}, "", connect), {"ok": None, "detail": ""})
        self.assertEqual(calls, [])
        self.assertIsNone(gs.parse_relay_target(""))
        self.assertEqual(gs.parse_relay_target("wss://relay.example.invalid/ws"), ("relay.example.invalid", 443))
        self.assertEqual(gs.parse_relay_target("ws://relay.example.invalid/ws"), ("relay.example.invalid", 80))
        env = {"GROK_RELAY_URL": "wss://relay.example.invalid/ws"}
        wake = "GROK_RELAY_TOKEN=secret-token\nGROK_RELAY_URL=ws://other.example.invalid:8791/ws\n"
        st = gs.check_relay(env, wake, connect)
        self.assertEqual(st, {"ok": True, "detail": "ok"})
        self.assertEqual(calls[-1], ("relay.example.invalid", 443, 3))
        self.assertNotIn("example", st["detail"])
        self.assertNotIn("secret", st["detail"])
        calls.clear()
        st = gs.check_relay({}, "export GROK_RELAY_URL=ws://relay.example.invalid:8791/ws\nOTHER=1\n", connect)
        self.assertEqual(st["ok"], True)
        self.assertEqual(calls[-1], ("relay.example.invalid", 8791, 3))
        gs.check_relay({}, "GROK_RELAY_URL='ws://relay.example.invalid/ws'\n", connect)
        self.assertEqual(calls[-1], ("relay.example.invalid", 80, 3))
        self.assertEqual(gs.env_value(wake, "GROK_RELAY_URL"), "ws://other.example.invalid:8791/ws")
        self.assertEqual(gs.env_value("GROK_RELAY_TOKEN=secret-token\n", "GROK_RELAY_URL"), "")
        down = gs.check_relay({"GROK_RELAY_URL": "wss://relay.example.invalid/ws"}, "", lambda *a: False)
        self.assertEqual(down, {"ok": False, "detail": "down"})
        self.assertNotIn("relay", down["detail"])


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
        self.assertNotEqual(pygame.image.tobytes(shipped, "RGBA"), pygame.image.tobytes(missing, "RGBA"))


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
