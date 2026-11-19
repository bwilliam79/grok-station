"""0.13.47: each team is a centered stack. Status sits in the middle column.

Line boxes are the CSS sizes: name 38 px, score 84 px, at 1280x720, doubled at 2560x1440.
Frames go to GS_FRAME_DIR when that is set, otherwise a temp directory.
"""
import datetime
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
NOW = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.timezone.utc)
LOGOS = {
    "arkansas": "https://a.espncdn.com/i/teamlogos/ncaa/500/8.png",
    "uca": "https://a.espncdn.com/i/teamlogos/ncaa/500/2110.png",
    "kc": "https://a.espncdn.com/i/teamlogos/nfl/500/kc.png",
    "den": "https://a.espncdn.com/i/teamlogos/nfl/500/den.png",
}


def _px(layout, at1280):
    """The spec pixel at 1280, doubled at 2560, through Layout."""
    return layout(int(round(at1280 * 3 / 2)))


def _rgba(canvas_w, canvas_h, mark_w, mark_h, color):
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
        word = next((text for img0, text in reversed(labels) if img0 is img), None)
        placed = pygame.Rect(dx, dy, img.get_width(), img.get_height())
        painted.append((word, gs.PAINT_MARKS[-1][0], placed))
        return result

    gs.PAINT_MARKS = []
    gs._line = watching_line
    gs.show = watching_show
    try:
        gs.draw_sports(surf, layout, card, panel, now=now or NOW)
    finally:
        gs._line = real_line
        gs.show = real_show
        gs.PAINT_MARKS = None
    return painted, panel, surf, layout


def _card(home, away, status="live", scores=(31, 17), league="NCAAF", period="Q2"):
    card = tiles._sports(status, True, False)
    card["home"] = {"name": home, "abbr": "H"}
    card["away"] = {"name": away, "abbr": "A"}
    card["home_score"], card["away_score"] = scores
    card["league"] = league
    card["period"] = period
    if status == "scheduled":
        card["home_score"] = card["away_score"] = None
        card["period"] = ""
    return card


def _near(got, want, tol=1):
    return abs(got - want) <= tol


def _same(pixel, color):
    return all(abs(int(pixel[i]) - int(color[i])) <= 18 for i in range(3))


def _color_box(surf, color, limit):
    """Bounding box of pixels close to color inside limit, or None."""
    rect = limit.clip(surf.get_rect())
    xs, ys = [], []
    for y in range(rect.top, rect.bottom):
        for x in range(rect.left, rect.right):
            if _same(surf.get_at((x, y)), color):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return pygame.Rect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


class Stack(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="gs1347-mark-")
        self.addCleanup(self.root.cleanup)
        self.wide = "d" * 64
        self.tall = "e" * 64
        self.square = "f" * 64
        with open(os.path.join(self.root.name, self.wide), "wb") as fh:
            fh.write(_rgba(460, 200, 230, 100, b"\xc4\x1e\x3a\xff"))
        with open(os.path.join(self.root.name, self.tall), "wb") as fh:
            fh.write(_rgba(80, 220, 70, 200, b"\x8c\x1d\x40\xff"))
        with open(os.path.join(self.root.name, self.square), "wb") as fh:
            fh.write(_rgba(40, 40, 40, 40, b"\x22\x55\xaa\xff"))
        self.old = gs._LOGO
        gs._LOGO = gs.cards.LogoCache(self.root.name, [])
        self.addCleanup(setattr, gs, "_LOGO", self.old)

    def _live(self, size, home_id, away_id, home="Arkansas", away="Central Arkansas", scores=(128, 105)):
        card = _card(home, away, scores=scores)
        card["home"]["logo_id"] = home_id
        card["away"]["logo_id"] = away_id
        return _paint(size, card)

    def test_columns_line_boxes_and_shared_rows(self):
        for size in SIZES:
            layout = gs.Layout(*size)
            painted, panel, _surf, _layout = self._live(size, self.wide, self.tall)
            box_w, box_h = _px(layout, 120), _px(layout, 72)
            top = _px(layout, 12)
            gap_logo, gap_score = _px(layout, 8), _px(layout, 4)
            name_line, score_line = _px(layout, 38), _px(layout, 84)
            col_w = _px(layout, 360)
            cx = (panel.left + panel.w // 4, panel.left + panel.w // 2, panel.left + (panel.w * 3) // 4)
            self.assertEqual(gs.LAST_SPORTS_GUIDE["columns"], cx, size)
            logo_top = panel.top + top
            name_top = logo_top + box_h + gap_logo
            score_top = name_top + name_line + gap_score
            self.assertEqual(gs.LAST_SPORTS_GUIDE["logo"], (logo_top, logo_top + box_h), size)
            self.assertEqual(gs.LAST_SPORTS_GUIDE["lines"], (name_top, score_top), size)
            strip = panel.bottom - _px(layout, 72)
            self.assertLessEqual(score_top + score_line, strip, size)
            name_f = gs.font("display", _px(layout, 32))
            score_f = gs.font("display", _px(layout, 80))
            name_base = gs._css_baseline(name_top, name_line, name_f)
            score_base = gs._css_baseline(score_top, score_line, score_f)
            names = [item for item in painted if item[0] in ("Arkansas", "Central Arkansas")]
            self.assertEqual(sorted(item[0] for item in names), ["Arkansas", "Central Arkansas"])
            self.assertEqual(gs.LAST_SPORTS_GUIDE["names"], ("Arkansas", "Central Arkansas"), size)
            home = min(names, key=lambda item: item[2].centerx)
            away = max(names, key=lambda item: item[2].centerx)
            self.assertTrue(_near(home[2].centerx, cx[0]), (size, home[2].centerx, cx[0]))
            self.assertTrue(_near(away[2].centerx, cx[2]), (size, away[2].centerx, cx[2]))
            self.assertEqual(home[2].y, away[2].y, size)
            self.assertEqual(home[2].y + name_f.get_ascent(), name_base, size)
            self.assertLessEqual(home[2].width, col_w, size)
            self.assertEqual(home[2].width, name_f.size("Arkansas")[0], size)
            self.assertEqual(away[2].width, name_f.size("Central Arkansas")[0], size)
            self.assertNotIn("\u2026", gs.LAST_SPORTS_GUIDE["names"][1])
            scores = [item for item in painted if item[0] is None and item[2].h == score_f.get_height()
                      and abs(item[2].centerx - cx[1]) > 30]
            self.assertGreaterEqual(len(scores), 2, size)
            left_score = min(scores, key=lambda item: item[2].centerx)
            right_score = max(scores, key=lambda item: item[2].centerx)
            self.assertTrue(_near(left_score[2].centerx, cx[0]), size)
            self.assertTrue(_near(right_score[2].centerx, cx[2]), size)
            self.assertEqual(left_score[2].y, right_score[2].y, size)
            self.assertEqual(left_score[2].y + score_f.get_ascent(), score_base, size)
            logos = [item for item in painted if item[0] is None and item[2].bottom <= name_top + 1
                     and (_near(item[2].centerx, cx[0], 2) or _near(item[2].centerx, cx[2], 2))]
            self.assertEqual(len(logos), 2, [(item[2].size, item[2].centerx) for item in painted if item[0] is None])
            for item, column in zip(sorted(logos, key=lambda item: item[2].centerx), (cx[0], cx[2])):
                box = pygame.Rect(column - box_w // 2, logo_top, box_w, box_h)
                self.assertTrue(box.contains(item[2]), (size, item[2], box))
                self.assertTrue(_near(item[2].centerx, box.centerx) and _near(item[2].centery, box.centery), size)
            dashes = [item for item in painted if item[0] is None and item[2].h == score_f.get_height()
                      and _near(item[2].centerx, cx[1], 4)]
            self.assertEqual(len(dashes), 1, size)
            self.assertEqual(dashes[0][2].h, score_f.get_height(), size)
            self.assertFalse(dashes[0][2].colliderect(left_score[2]) or dashes[0][2].colliderect(right_score[2]))
            dash_ink, digit_ink = dashes[0][1], left_score[1]
            other_ink = right_score[1]
            self.assertTrue(_near(dash_ink.centerx, cx[1]), (size, dash_ink.centerx, cx[1]))
            self.assertTrue(_near(dash_ink.centery, digit_ink.centery), (size, dash_ink.centery, digit_ink.centery))
            self.assertTrue(_near(dash_ink.centery, other_ink.centery), size)
            self.assertEqual(_surf.get_at((dash_ink.centerx, dash_ink.centery))[:3], tuple(gs.MUTED)[:3])
            # Painted digit ink, and the 84 px line box, stop at the next-game border.
            for word in ("128", "00"):
                img = score_f.render(word, True, gs.TEXT)
                ink = img.get_bounding_rect()
                ink_bottom = score_base - score_f.get_ascent() + ink.bottom
                self.assertLess(ink_bottom, strip, (size, word, ink_bottom, strip))
            self.assertLessEqual(score_top + score_line, strip, size)
            self.assertEqual(layout(gs.SPORTS_NAME_PX), _px(layout, 32), size)
            self.assertEqual(layout(gs.SPORTS_SCORE_PX), _px(layout, 80), size)
            self.assertEqual(layout(gs.SPORTS_KICK_PX), _px(layout, 60), size)
            self.assertEqual(layout(gs.SPORTS_STATUS_PX), _px(layout, 22), size)
            self.assertEqual(layout(gs.SPORTS_LEAGUE_PX), _px(layout, 18), size)
            self.assertEqual(gs.LAST_SPORTS_GUIDE["status"], "LIVE \u00b7 Q2", size)
            self.assertNotIn("\u25cf", gs.LAST_SPORTS_GUIDE["status"])
            header = [item for item in painted if item[0] in ("LIVE", "FINAL", "Q2", "NCAAF", "NFL")
                      and item[2].top < logo_top and item[2].left <= panel.left + _px(layout, 40)]
            self.assertEqual(header, [], size)
            stat = [item for item in painted if item[0] is None and _near(item[2].centerx, cx[1], 2)
                    and item[2].top >= logo_top and item[2].bottom <= logo_top + box_h and item[2].w > 40]
            self.assertGreaterEqual(len(stat), 1, size)

    def test_live_dot_is_a_filled_disc(self):
        for size in SIZES:
            layout = gs.Layout(*size)
            painted, panel, surf, _layout = self._live(size, self.wide, self.tall, scores=(31, 17))
            cx = panel.left + panel.w // 2
            logo_top, logo_bot = gs.LAST_SPORTS_GUIDE["logo"]
            band = pygame.Rect(panel.left, logo_top, panel.w, logo_bot - logo_top)
            teal = []
            for y in range(band.top, band.bottom):
                for x in range(band.left, band.right):
                    if _same(surf.get_at((x, y)), gs.LISTENING):
                        teal.append((x, y))
            self.assertTrue(teal, size)
            xs = [p[0] for p in teal]
            ys = [p[1] for p in teal]
            group = pygame.Rect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
            self.assertTrue(_near(group.centerx, cx), (size, group.centerx, cx))
            # The dot is the leftmost run of teal columns, before the word.
            cols = []
            x = min(xs)
            while x <= max(xs) and not any(px == x for px, _py in teal):
                x += 1
            run = []
            while x <= max(xs) and any(px == x for px, _py in teal):
                run.append(x)
                x += 1
            self.assertTrue(run, size)
            dot = [p for p in teal if p[0] in run]
            left, right = min(p[0] for p in dot), max(p[0] for p in dot)
            top, bot = min(p[1] for p in dot), max(p[1] for p in dot)
            mid = ((left + right) // 2, (top + bot) // 2)
            self.assertTrue(_same(surf.get_at(mid), gs.LISTENING), (size, surf.get_at(mid)))
            for corner in ((left, top), (right, top), (left, bot), (right, bot)):
                self.assertFalse(_same(surf.get_at(corner), gs.LISTENING), (size, corner, surf.get_at(corner)))
            self.assertEqual(gs.LAST_SPORTS_GUIDE["status"], "LIVE \u00b7 Q2", size)
            self.assertNotIn("\u25cf", gs.LAST_SPORTS_GUIDE["status"])
            del painted

    def test_badge_uses_the_logo_box_and_the_fixed_column(self):
        for size in SIZES:
            layout = gs.Layout(*size)
            card = _card("Arkansas", "Central Arkansas", scores=(24, 10))
            card["home"]["logo_badge"] = True
            card["away"]["logo_id"] = self.square
            painted, panel, _surf, _layout = _paint(size, card)
            box_w, box_h = _px(layout, 120), _px(layout, 72)
            logo_top = panel.top + _px(layout, 12)
            cx = panel.left + panel.w // 4
            badges = [item for item in painted if item[0] is None and item[2].w == box_h and item[2].h == box_h
                      and _near(item[2].centerx, cx, 2)]
            self.assertEqual(len(badges), 1, size)
            box = pygame.Rect(cx - box_w // 2, logo_top, box_w, box_h)
            self.assertTrue(box.contains(badges[0][2]), (size, badges[0][2], box))
            self.assertTrue(_near(badges[0][2].centerx, box.centerx) and _near(badges[0][2].centery, box.centery))
            names = [item for item in painted if item[0] == "Arkansas"]
            self.assertEqual(len(names), 1, size)
            self.assertTrue(_near(names[0][2].centerx, cx), size)

    def test_pregame_day_kickoff_and_tbd(self):
        for size in SIZES:
            layout = gs.Layout(*size)
            later = _card("Kansas City", "Denver", status="scheduled", league="NFL")
            later["starts_at"] = "2026-11-22T19:00:00Z"  # Sunday 1:00 PM in the station zone
            today = _card("Arkansas", "Central Arkansas", status="scheduled")
            today["starts_at"] = "2026-11-19T19:15:00-06:00"
            today["home"]["logo_id"] = self.wide
            today["away"]["logo_id"] = self.tall
            tbd = _card("Arkansas", "Denver", status="scheduled")
            for card, status, kick in (
                (later, "SCHEDULED \u00b7 SUN", "1:00 PM"),
                (today, "SCHEDULED", "7:15 PM"),
                (tbd, "SCHEDULED", "Time TBD"),
            ):
                painted, panel, _surf, _layout = _paint(size, card)
                self.assertEqual(gs.LAST_SPORTS_GUIDE["status"], status, (size, status))
                self.assertEqual(gs.LAST_SPORTS_GUIDE["kick"], kick, (size, kick))
                box_h = _px(layout, 72)
                name_line = _px(layout, 38)
                gap = _px(layout, 8)
                strip = panel.bottom - _px(layout, 72)
                main_h = strip - panel.top
                stack_h = box_h + gap + name_line
                logo_top = panel.top + max(0, (main_h - stack_h) // 2)
                self.assertEqual(gs.LAST_SPORTS_GUIDE["logo"][0], logo_top, (size, status))
                score_top = panel.top + _px(layout, 12) + box_h + gap + name_line + _px(layout, 4)
                score_f = gs.font("display", _px(layout, 80))
                score_base = gs._css_baseline(score_top, _px(layout, 84), score_f)
                kick_f = gs.font("display", _px(layout, 60))
                kicks = [item for item in painted if item[0] is None and item[2].h == kick_f.get_height()
                         and _near(item[2].centerx, panel.left + panel.w // 2)]
                self.assertEqual(len(kicks), 1, (size, status, [(item[2].size, item[2].centerx) for item in painted if item[0] is None]))
                self.assertEqual(kicks[0][2].y + kick_f.get_ascent(), score_base, (size, status))
                if kick == "Time TBD":
                    self.assertEqual(gs._pregame_when(card, NOW)[1], gs.MUTED)
                else:
                    self.assertEqual(gs._pregame_when(card, NOW)[1], gs.TEXT)
                self.assertNotIn("SUN", gs.LAST_SPORTS_GUIDE["status"] if status == "SCHEDULED" else "")
                names = [item for item in painted if item[0] in ("Kansas City", "Denver", "Arkansas", "Central Arkansas")]
                cx_l = panel.left + panel.w // 4
                cx_r = panel.left + (panel.w * 3) // 4
                self.assertTrue(_near(min(names, key=lambda item: item[2].centerx)[2].centerx, cx_l))
                self.assertTrue(_near(max(names, key=lambda item: item[2].centerx)[2].centerx, cx_r))


def _download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "grok-station-test"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("not a png")
    with open(path, "wb") as fh:
        fh.write(data)


def _overlay(surf, guide, panel):
    red = (255, 64, 64)
    blue = (64, 160, 255)
    gold = (255, 196, 64)
    top = guide["logo"][0] - 8
    bot = guide["strip_top"]
    for x in guide["columns"]:
        pygame.draw.line(surf, blue, (x, top), (x, bot), 1)
    for y in guide["logo"]:
        pygame.draw.line(surf, red, (panel.left, y), (panel.right, y), 1)
    for y in guide["baselines"]:
        pygame.draw.line(surf, gold, (panel.left, y), (panel.right, y), 1)


class Frames(unittest.TestCase):
    """Numeric checks plus optional PNG dumps. The dump directory is GS_FRAME_DIR or a temp dir."""

    def test_real_logos_match_the_columns(self):
        env = os.environ.get("GS_FRAME_DIR")
        if env:
            os.makedirs(env, exist_ok=True)
            art = env
            cleanup = None
        else:
            tmp = tempfile.TemporaryDirectory(prefix="gs1347-frames-")
            self.addCleanup(tmp.cleanup)
            art = tmp.name
            cleanup = tmp
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
            specs = (
                ("live-ark", (1280, 720), _card("Arkansas", "Central Arkansas", scores=(31, 17)), ids["arkansas"], ids["uca"], False),
                ("live-ark", (2560, 1440), _card("Arkansas", "Central Arkansas", scores=(31, 17)), ids["arkansas"], ids["uca"], False),
                ("live-kc", (1280, 720), _card("Kansas City", "Denver", scores=(27, 14), league="NFL"), ids["kc"], ids["den"], False),
                ("live-kc", (2560, 1440), _card("Kansas City", "Denver", scores=(27, 14), league="NFL"), ids["kc"], ids["den"], False),
            )
            for name, size, card, home, away, _overlay_flag in specs:
                card["home"]["logo_id"] = home
                card["away"]["logo_id"] = away
                self._frame(art, name, size, card, overlay=False)
                self.assertEqual(len(gs.LAST_SPORTS_GUIDE["columns"]), 3)
                panel = gs.bottom_panel_rect(gs.Layout(*size))
                cols = gs.LAST_SPORTS_GUIDE["columns"]
                self.assertEqual(cols[0], panel.left + panel.w // 4)
                self.assertEqual(cols[1], panel.left + panel.w // 2)
                self.assertEqual(cols[2], panel.left + (panel.w * 3) // 4)
            pre = _card("Kansas City", "Denver", status="scheduled", league="NFL")
            pre["starts_at"] = "2026-11-22T19:00:00Z"
            pre["home"]["logo_id"] = ids["kc"]
            pre["away"]["logo_id"] = ids["den"]
            self._frame(art, "pregame-kc-notoday", (1280, 720), pre, overlay=False)
            self.assertEqual(gs.LAST_SPORTS_GUIDE["status"], "SCHEDULED \u00b7 SUN")
            self.assertEqual(gs.LAST_SPORTS_GUIDE["kick"], "1:00 PM")
            today = _card("Arkansas", "Central Arkansas", status="scheduled")
            today["starts_at"] = "2026-11-19T19:15:00-06:00"
            today["home"]["logo_id"] = ids["arkansas"]
            today["away"]["logo_id"] = ids["uca"]
            self._frame(art, "pregame-ark-today", (1280, 720), today, overlay=False)
            self.assertEqual(gs.LAST_SPORTS_GUIDE["status"], "SCHEDULED")
            self.assertEqual(gs.LAST_SPORTS_GUIDE["kick"], "7:15 PM")
            tbd = _card("Arkansas", "Denver", status="scheduled")
            tbd["home"]["logo_id"] = ids["arkansas"]
            tbd["away"]["logo_id"] = ids["den"]
            self._frame(art, "pregame-tbd", (1280, 720), tbd, overlay=False)
            self.assertEqual(gs.LAST_SPORTS_GUIDE["kick"], "Time TBD")
            self.assertEqual(gs.LAST_SPORTS_GUIDE["status"], "SCHEDULED")
            long = _card("Arkansas", "Central Arkansas", status="scheduled")
            long["starts_at"] = "2026-11-22T19:00:00Z"
            long["home"]["logo_id"] = ids["arkansas"]
            long["away"]["logo_id"] = ids["uca"]
            self._frame(art, "pregame-longname", (1280, 720), long, overlay=False)
            self.assertEqual(gs.LAST_SPORTS_GUIDE["names"][1], "Central Arkansas")
            live = _card("Arkansas", "Central Arkansas", scores=(31, 17))
            live["home"]["logo_id"] = ids["arkansas"]
            live["away"]["logo_id"] = ids["uca"]
            self._frame(art, "overlay-live-ark", (1280, 720), live, overlay=True)
        finally:
            gs._LOGO = old
        del cleanup

    def _frame(self, art, name, size, card, overlay):
        layout = gs.Layout(*size)
        surf = pygame.Surface(size)
        surf.fill(gs.BG)
        zone = gs.zone_view(None, card)
        clock = time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M")
        gs.draw_zone(surf, layout, clock, tiles._Wx(), True, zone)
        panel = gs.LAST_ZONE_RECT
        self.assertIsNotNone(panel)
        if overlay:
            _overlay(surf, gs.LAST_SPORTS_GUIDE, panel)
        path = os.path.join(art, "%s-%dx%d.png" % (name, size[0], size[1]))
        pygame.image.save(surf, path)
        self.assertGreater(os.path.getsize(path), 1000, path)


if __name__ == "__main__":
    unittest.main()
