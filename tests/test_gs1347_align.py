"""0.13.47: a wide mark and a tall mark share one text column, one name line, and one score line.

The slot is fixed. The art is centered in it. The name and the score do not follow the art.
Frames use real ESPN marks downloaded into a temp directory. The overlay is drawn here, not
by the station.
"""
import hashlib
import os
import struct
import sys
import tempfile
import time
import unittest
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1342_tiles as tiles  # noqa: E402

gs = tiles.gs
pygame = tiles.pygame

SIZES = ((1280, 720), (2560, 1440))
ART = "/opt/cursor/artifacts/gs1347"
LOGOS = {
    "arkansas": "https://a.espncdn.com/i/teamlogos/ncaa/500/8.png",
    "uca": "https://a.espncdn.com/i/teamlogos/ncaa/500/2110.png",
    "kc": "https://a.espncdn.com/i/teamlogos/nfl/500/kc.png",
    "den": "https://a.espncdn.com/i/teamlogos/nfl/500/den.png",
}


def _rgba(canvas_w, canvas_h, mark_w, mark_h, color):
    """A PNG with one opaque mark centered in a transparent canvas."""
    raw = []
    x0 = (canvas_w - mark_w) // 2
    y0 = (canvas_h - mark_h) // 2
    for y in range(canvas_h):
        row = bytearray([0])
        for x in range(canvas_w):
            if x0 <= x < x0 + mark_w and y0 <= y < y0 + mark_h:
                row.extend(color)
            else:
                row.extend(b"\x00\x00\x00\x00")
        raw.append(bytes(row))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", canvas_w, canvas_h, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(raw))) + chunk(b"IEND", b"")


def _paint(size, card, now=None):
    layout = gs.Layout(*size)
    panel = gs.bottom_panel_rect(layout)
    surf = pygame.Surface(size)
    surf.fill(gs.BG)
    labels = []
    painted = []
    real_line, real_show = gs._line, gs.show

    def watching_line(surf, role, px, text, color, width):
        img = real_line(surf, role, px, text, color, width)
        labels.append((img, str(text)))
        return img

    def watching_show(surf, img, dest, area=None, special_flags=0):
        before = len(gs.PAINT_MARKS)
        result = real_show(surf, img, dest, area, special_flags)
        if len(gs.PAINT_MARKS) <= before:
            return result
        if isinstance(dest, pygame.Rect):
            dx, dy = dest.x, dest.y
        else:
            dx, dy = int(dest[0]), int(dest[1])
        word = next((text for img0, text in labels if img0 is img), None)
        placed = pygame.Rect(dx, dy, img.get_width(), img.get_height())
        painted.append((word, gs.PAINT_MARKS[-1][0], placed))
        return result

    gs.PAINT_MARKS = []
    gs._line = watching_line
    gs.show = watching_show
    try:
        gs.draw_sports(surf, layout, card, panel, now=now)
    finally:
        gs._line = real_line
        gs.show = real_show
        gs.PAINT_MARKS = None
    return painted, panel, surf, layout


def _card(home, away, home_id, away_id, status="live", scores=(24, 17)):
    card = tiles._sports(status, True, False)
    card["home"] = {"name": home, "abbr": "H", "logo_id": home_id}
    card["away"] = {"name": away, "abbr": "A", "logo_id": away_id}
    card["home_score"], card["away_score"] = scores
    card["league"] = "NCAAF"
    card["period"] = "Q2"
    return card


def _column(size):
    layout = gs.Layout(*size)
    panel = gs.bottom_panel_rect(layout)
    inset = layout(48)
    slot_w = layout(gs.SPORTS_LOGO_BOX_W)
    slot_h = layout(gs.SPORTS_LOGO_BOX_H)
    gap = layout(gs.SPORTS_LOGO_GAP)
    text_left = panel.left + inset + slot_w + gap
    text_right = panel.right - inset - slot_w - gap
    return panel, inset, slot_w, slot_h, text_left, text_right


class Align(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="gs1347-mark-")
        self.addCleanup(self.root.cleanup)
        self.wide = "d" * 64
        self.tall = "e" * 64
        with open(os.path.join(self.root.name, self.wide), "wb") as fh:
            fh.write(_rgba(460, 200, 230, 100, b"\xc4\x1e\x3a\xff"))
        with open(os.path.join(self.root.name, self.tall), "wb") as fh:
            fh.write(_rgba(80, 220, 70, 200, b"\x8c\x1d\x40\xff"))
        self.old = gs._LOGO
        gs._LOGO = gs.cards.LogoCache(self.root.name, [])
        self.addCleanup(self._restore)

    def _restore(self):
        gs._LOGO = self.old

    def _logos(self, painted, panel, inset, slot_w):
        home_cx = panel.left + inset + slot_w // 2
        away_cx = panel.right - inset - slot_w // 2
        return [item for item in painted
                if item[0] is None and (abs(item[2].centerx - home_cx) <= 2 or abs(item[2].centerx - away_cx) <= 2)]

    def test_wide_and_tall_share_the_column_and_the_baselines(self):
        for size in SIZES:
            panel, inset, slot_w, _slot_h, text_left, text_right = _column(size)
            layout = gs.Layout(*size)
            home_name, away_name = "Arkansas", "Central Arkansas"
            card = _card(home_name, away_name, self.wide, self.tall, scores=(128, 105))
            swapped = _card(home_name, away_name, self.tall, self.wide, scores=(128, 105))
            painted, panel, _surf, _layout = _paint(size, card)
            other, _panel, _surf2, _layout2 = _paint(size, swapped)
            name_px = layout(gs.SPORTS_NAME_PX)
            score_px = layout(gs.SPORTS_SCORE_PX)
            name_f = gs.font("display", name_px)
            score_f = gs.font("display", score_px)
            names = [item for item in painted if item[0] in (home_name, away_name)]
            self.assertEqual(len(names), 2, [item[0] for item in painted])
            home = min(names, key=lambda item: item[2].centerx)
            away = max(names, key=lambda item: item[2].centerx)
            self.assertEqual(home[2].left, text_left, size)
            self.assertEqual(away[2].right, text_right, size)
            other_names = [item for item in other if item[0] in (home_name, away_name)]
            other_home = min(other_names, key=lambda item: item[2].centerx)
            other_away = max(other_names, key=lambda item: item[2].centerx)
            self.assertEqual(other_home[2].left, text_left, size)
            self.assertEqual(other_away[2].right, text_right, size)
            home_base = home[2].y + name_f.get_ascent()
            away_base = away[2].y + name_f.get_ascent()
            self.assertEqual(home_base, away_base, size)
            scores = [item for item in painted
                      if item[0] is None and item[2].h == score_f.get_height()
                      and abs(item[2].centerx - panel.centerx) > 40]
            self.assertGreaterEqual(len(scores), 2, size)
            home_score = min(scores, key=lambda item: item[2].centerx)
            away_score = max(scores, key=lambda item: item[2].centerx)
            self.assertEqual(home_score[2].left, text_left, size)
            self.assertEqual(away_score[2].right, text_right, size)
            score_base = home_score[2].y + score_f.get_ascent()
            self.assertEqual(away_score[2].y + score_f.get_ascent(), score_base, size)
            dash = [item for item in painted
                    if item[0] is None and abs(item[2].centerx - panel.centerx) <= 2
                    and item[2].h == score_f.get_height()]
            self.assertEqual(len(dash), 1, size)
            self.assertEqual(dash[0][2].y + score_f.get_ascent(), score_base, size)
            self.assertLessEqual(abs(dash[0][2].centerx - panel.centerx), 1, size)
            logos = self._logos(painted, panel, inset, slot_w)
            self.assertEqual(len(logos), 2, [(item[2].size, item[2].centerx) for item in painted if item[0] is None])
            home_art = min(logos, key=lambda item: item[2].centerx)
            away_art = max(logos, key=lambda item: item[2].centerx)
            slot_cx = panel.left + inset + slot_w // 2
            away_cx = panel.right - inset - slot_w // 2
            self.assertLessEqual(abs(home_art[2].centerx - slot_cx), 1, size)
            self.assertLessEqual(abs(away_art[2].centerx - away_cx), 1, size)
            name_em = name_f.get_ascent() - name_f.get_descent()
            score_em = score_f.get_ascent() - score_f.get_descent()
            block = name_em + max(1, layout(8)) + score_em
            block_cy = home[2].y + block // 2
            self.assertLessEqual(abs(home_art[2].centery - block_cy), 1, size)
            self.assertLessEqual(abs(away_art[2].centery - block_cy), 1, size)
            self.assertLessEqual(abs(home_art[2].centery - away_art[2].centery), 1, size)
            live = [item for item in painted if item[0] == "LIVE"]
            nxt = [item for item in painted if item[0] == "NEXT GAME"]
            period = [item for item in painted if item[0] == "Q2"]
            when = [item for item in painted if item[0] == "Sun 1:00 PM"]
            self.assertEqual(len(live), 1)
            self.assertEqual(len(nxt), 1)
            self.assertEqual(live[0][2].left, panel.left + inset, size)
            self.assertEqual(nxt[0][2].left, panel.left + inset, size)
            self.assertEqual(live[0][2].left, panel.left + inset)
            self.assertEqual(period[0][2].right, panel.right - inset, size)
            self.assertEqual(when[0][2].right, panel.right - inset, size)
            for item in painted:
                self.assertTrue(panel.contains(item[2]), (size, item[0], item[2], panel))
            bodies = [item[2] for item in (home, away, home_score, away_score, dash[0], home_art, away_art)]
            for i, a in enumerate(bodies):
                for b in bodies[i + 1:]:
                    self.assertFalse(a.colliderect(b), (size, a, b))

    def test_pregame_time_sits_on_the_slot_center(self):
        import datetime
        moment = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.timezone.utc)
        for size in SIZES:
            panel, inset, slot_w, _slot_h, text_left, text_right = _column(size)
            layout = gs.Layout(*size)
            card = _card("Arkansas", "Central Arkansas", self.wide, self.tall, status="scheduled")
            card["starts_at"] = "2026-11-21T19:15:00Z"
            card["home_score"] = card["away_score"] = None
            card["period"] = ""
            painted, panel, _surf, _layout = _paint(size, card, now=moment)
            names = [item for item in painted if item[0] in ("Arkansas", "Central Arkansas")]
            self.assertEqual(len(names), 2, size)
            home = min(names, key=lambda item: item[2].centerx)
            away = max(names, key=lambda item: item[2].centerx)
            self.assertEqual(home[2].left, text_left, size)
            self.assertEqual(away[2].right, text_right, size)
            name_f = gs.font("display", layout(gs.SPORTS_NAME_PX))
            self.assertEqual(home[2].y + name_f.get_ascent(), away[2].y + name_f.get_ascent(), size)
            logos = self._logos(painted, panel, inset, slot_w)
            self.assertEqual(len(logos), 2, size)
            art_cy = min(logos, key=lambda item: item[2].centerx)[2].centery
            name_em = name_f.get_ascent() - name_f.get_descent()
            self.assertLessEqual(abs((home[2].y + name_em // 2) - art_cy), 1, size)
            clocks = [item for item in painted
                      if item[0] is None and abs(item[2].centerx - panel.centerx) <= 2 and item[2].w > item[2].h]
            self.assertEqual(len(clocks), 1, [item[0] for item in painted])
            self.assertLessEqual(abs(clocks[0][2].centery - art_cy), 1, size)
            self.assertLessEqual(abs(clocks[0][2].centerx - panel.centerx), 1, size)
            for item in names + logos + clocks:
                self.assertTrue(panel.contains(item[2]), (size, item[2]))
            for item in names + logos:
                self.assertFalse(clocks[0][2].colliderect(item[2]), (size, item[0], item[2]))


def _download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "grok-station-test"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a png: %s" % url)
    with open(path, "wb") as fh:
        fh.write(data)


def _overlay(surf, guide):
    """Slot boxes, column lines, the center line, the baselines, and the label line. Test only."""
    slots = guide["slots"]
    red = (255, 64, 64)
    blue = (64, 160, 255)
    gold = (255, 196, 64)
    for slot in slots:
        pygame.draw.rect(surf, red, slot, 2)
    top = min(slot.top for slot in slots) - 24
    bot = max(slot.bottom for slot in slots) + 36
    for x in guide["text"]:
        pygame.draw.line(surf, blue, (x, top), (x, bot), 1)
    cy = guide["center_y"]
    pygame.draw.line(surf, blue, (slots[0].left, cy), (slots[1].right, cy), 1)
    for base in guide["baselines"]:
        if base is None:
            continue
        pygame.draw.line(surf, gold, (slots[0].left, base), (slots[1].right, base), 1)
    x = guide["label_x"]
    pygame.draw.line(surf, red, (x, slots[0].top - 70), (x, slots[0].bottom), 1)


class Frames(unittest.TestCase):
    def test_real_logos_and_the_overlay(self):
        os.makedirs(ART, exist_ok=True)
        root = tempfile.TemporaryDirectory(prefix="gs1347-espn-")
        self.addCleanup(root.cleanup)
        ids = {}
        for key, url in LOGOS.items():
            lid = hashlib.sha256(gs.cards.canonical_logo_url(url).encode()).hexdigest()
            _download(url, os.path.join(root.name, lid))
            ids[key] = lid
        old = gs._LOGO
        gs._LOGO = gs.cards.LogoCache(root.name, ["a.espncdn.com"])
        try:
            live = {
                ("ark", (1280, 720)): ("Arkansas", "Central Arkansas", ids["arkansas"], ids["uca"], "NCAAF", (31, 17)),
                ("ark", (2560, 1440)): ("Arkansas", "Central Arkansas", ids["arkansas"], ids["uca"], "NCAAF", (31, 17)),
                ("kc", (1280, 720)): ("Kansas City", "Denver", ids["kc"], ids["den"], "NFL", (27, 14)),
                ("kc", (2560, 1440)): ("Kansas City", "Denver", ids["kc"], ids["den"], "NFL", (27, 14)),
            }
            for (tag, size), (home, away, hid, aid, league, scores) in live.items():
                card = _card(home, away, hid, aid, scores=scores)
                card["league"] = league
                self._frame("live-%s" % tag, size, card, overlay=(tag == "ark" and size == (1280, 720)))
            pre = _card("Arkansas", "Central Arkansas", ids["arkansas"], ids["uca"], status="scheduled")
            pre["league"] = "NCAAF"
            pre["starts_at"] = "2026-11-21T19:15:00Z"
            pre["home_score"] = pre["away_score"] = None
            pre["period"] = ""
            self._frame("pregame-ark", (1280, 720), pre, overlay=False)
        finally:
            gs._LOGO = old

    def _frame(self, name, size, card, overlay):
        layout = gs.Layout(*size)
        surf = pygame.Surface(size)
        surf.fill(gs.BG)
        zone = gs.zone_view(None, card)
        gs.draw_zone(surf, layout, time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M"), tiles._Wx(), True, zone)
        path = os.path.join(ART, "%s-%dx%d.png" % (name, size[0], size[1]))
        pygame.image.save(surf, path)
        self.assertTrue(os.path.isfile(path), path)
        self.assertGreater(os.path.getsize(path), 1000, path)
        if overlay:
            self.assertIsNotNone(gs.LAST_SPORTS_GUIDE)
            self.assertIsNotNone(gs.LAST_SPORTS_GUIDE["slots"])
            _overlay(surf, gs.LAST_SPORTS_GUIDE)
            over = os.path.join(ART, "overlay-ark-%dx%d.png" % (size[0], size[1]))
            pygame.image.save(surf, over)
            self.assertGreater(os.path.getsize(over), 1000, over)


if __name__ == "__main__":
    unittest.main()
