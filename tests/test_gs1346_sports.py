"""0.13.46: team marks are 112 px at 1280x720, beside a name and score that stay at base size.

Frames are saved for live, final, and scheduled cards at 1280x720 and 2560x1440.
GS_FRAME_DIR overrides the directory. Otherwise the frames go in a temp directory.
"""
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1342_tiles as tiles  # noqa: E402

gs = tiles.gs
pygame = tiles.pygame

SIZES = ((1280, 720), (2560, 1440))
NOW = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.timezone.utc)


def _center_line(painted, panel):
    """The pregame time, drawn with font.render so it has no word."""
    cands = []
    for word, _ink, placed in painted:
        if word is not None or placed.w <= placed.h:
            continue
        if abs(placed.centerx - panel.centerx) <= 8:
            cands.append(placed)
    return cands[-1] if cands else None


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
    return painted, panel, surf


class SportsFrames(unittest.TestCase):
    def setUp(self):
        env = os.environ.get("GS_FRAME_DIR")
        if env:
            os.makedirs(env, exist_ok=True)
            self.art = env
        else:
            self._tmp = tempfile.TemporaryDirectory(prefix="gs1346-frames-")
            self.addCleanup(self._tmp.cleanup)
            self.art = self._tmp.name

    def test_logos_and_pregame_type_at_both_frames(self):
        expect = {(1280, 720): 112, (2560, 1440): 224}
        for size in SIZES:
            self.assertEqual(gs.Layout(*size)(gs.SPORTS_LOGO_PX), expect[size], size)
            live = tiles._sports("live", True, True)
            final = tiles._sports("final", True, True)
            timed = tiles._sports("scheduled", True, True)
            timed["starts_at"] = "2026-11-19T19:15:00Z"
            tbd = tiles._sports("scheduled", True, True)
            old = gs._LOGO
            try:
                frames = {}
                used = {}
                real_px = gs._pregame_px

                def watching_px(score_px, label, _real=real_px):
                    used[label] = score_px
                    return _real(score_px, label)

                for name, card, now in (
                    ("live", live, None),
                    ("final", final, None),
                    ("scheduled", timed, NOW),
                    ("tbd", tbd, NOW),
                ):
                    gs._pregame_px = watching_px
                    try:
                        painted, panel, surf = _paint(size, card, now)
                    finally:
                        gs._pregame_px = real_px
                    frames[name] = (painted, panel, surf)
                    path = os.path.join(self.art, "sports-%s-%dx%d.png" % (name, size[0], size[1]))
                    pygame.image.save(surf, path)
                    self.assertTrue(os.path.isfile(path), path)
            finally:
                gs._LOGO = old
            for name in ("live", "final"):
                painted, panel, _surf = frames[name]
                tiles.LogoSize()._check(painted, panel, expect[size])
            for name in ("scheduled", "tbd"):
                painted, panel, _surf = frames[name]
                logos = [item for item in painted if item[0] is None and item[2].w == item[2].h and item[2].h >= 8]
                self.assertGreaterEqual(len(logos), 2, name)
                for item in logos:
                    self.assertEqual((item[2].w, item[2].h), (expect[size], expect[size]))
                    self.assertTrue(panel.contains(item[2]))
                names = [item for item in painted if item[0] in ("Kansas City", "Denver")]
                for name_item in names:
                    logo = min(logos, key=lambda item: abs(item[2].centerx - name_item[2].centerx))
                    self.assertLessEqual(abs(name_item[2].centery - logo[2].centery), 2, name)
            layout = gs.Layout(*size)
            base_name = gs.font("display", layout(gs.SPORTS_NAME_PX)).get_height()
            base_score = gs.font("display", layout(gs.SPORTS_SCORE_PX)).get_height()
            score_px = layout(gs.SPORTS_SCORE_PX)
            self.assertEqual(score_px, 80 if size == (1280, 720) else 160)
            for name in ("live", "final", "scheduled"):
                painted, panel, _surf = frames[name]
                names = [item for item in painted if item[0] in ("Kansas City", "Denver")]
                self.assertEqual(len(names), 2, name)
                for item in names:
                    self.assertGreaterEqual(item[2].h, base_name, (name, item[0], item[2].h, base_name))
                if name != "scheduled":
                    name_bottom = max(item[2].bottom for item in names)
                    scores = [item for item in painted
                              if item[0] is None and item[2].top >= name_bottom - 1
                              and item[2].w != item[2].h and abs(item[2].centerx - panel.centerx) > 40]
                    self.assertGreaterEqual(len(scores), 2, name)
                    for item in scores:
                        self.assertGreaterEqual(item[2].h, base_score, (name, item[2].h, base_score))
            clock_label, _ink = gs._pregame_when(timed, NOW)
            clock = _center_line(frames["scheduled"][0], frames["scheduled"][1])
            tbd_placed = _center_line(frames["tbd"][0], frames["tbd"][1])
            self.assertIsNotNone(clock, [item[0] for item in frames["scheduled"][0]])
            self.assertIsNotNone(tbd_placed)
            self.assertEqual(used[clock_label], score_px)
            self.assertEqual(used["Time TBD"], score_px)
            kick = max(8, int(round(score_px * gs.PREGAME_RATIO)))
            tbd_px = max(8, int(round(score_px * gs.PREGAME_TBD_RATIO)))
            self.assertEqual(gs._pregame_px(score_px, clock_label), kick)
            self.assertEqual(gs._pregame_px(score_px, "Time TBD"), tbd_px)
            self.assertEqual(clock.h, gs.font("display", kick).get_height())
            self.assertEqual(tbd_placed.h, gs.font("display", tbd_px).get_height())
            if size == (1280, 720):
                self.assertEqual(kick, 60)
            # The kickoff time and Time TBD stay clear of the names and the schedule line.
            for painted, panel, _surf in (frames["scheduled"], frames["tbd"]):
                names = [item for item in painted if item[0] in ("Kansas City", "Denver")]
                center = [item for item in painted if item[0] is None and item[2].w > item[2].h
                          and abs(item[2].centerx - panel.centerx) <= 8]
                sched = [item for item in painted if item[0] and "Kansas City at Denver" in item[0]]
                self.assertEqual(len(names), 2)
                self.assertTrue(center)
                self.assertTrue(sched)
                for item in center + names + sched:
                    self.assertTrue(panel.contains(item[2]), (item[0], item[2], panel))
                for item in center:
                    for name in names:
                        self.assertFalse(item[2].colliderect(name[2]), (item[0], name[0]))
                    for row in sched:
                        self.assertFalse(item[2].colliderect(row[2]), (item[0], row[0]))


def _rgba(canvas_w, canvas_h, mark_w, mark_h):
    """A PNG with a red mark centered in a transparent canvas."""
    import struct
    import zlib
    raw = []
    x0 = (canvas_w - mark_w) // 2
    y0 = (canvas_h - mark_h) // 2
    for y in range(canvas_h):
        row = bytearray([0])
        for x in range(canvas_w):
            if x0 <= x < x0 + mark_w and y0 <= y < y0 + mark_h:
                row.extend(b"\xff\x00\x00\xff")
            else:
                row.extend(b"\x00\x00\x00\x00")
        raw.append(bytes(row))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", canvas_w, canvas_h, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(raw))) + chunk(b"IEND", b"")


class LogoBox(unittest.TestCase):
    def test_a_wide_mark_fits_the_box_and_a_square_stays_square(self):
        size = (1280, 720)
        layout = gs.Layout(*size)
        self.assertEqual((layout(gs.SPORTS_LOGO_BOX_W), layout(gs.SPORTS_LOGO_BOX_H)), (168, 112))
        root = tempfile.TemporaryDirectory(prefix="gs1346-mark-")
        self.addCleanup(root.cleanup)
        wide_id = "b" * 64
        square_id = "c" * 64
        with open(os.path.join(root.name, wide_id), "wb") as fh:
            fh.write(_rgba(460, 200, 230, 100))
        with open(os.path.join(root.name, square_id), "wb") as fh:
            fh.write(_rgba(40, 40, 40, 40))
        card = tiles._sports("live", True, False)
        card["home"]["logo_id"] = wide_id
        card["home"]["logo_badge"] = False
        card["away"]["logo_id"] = square_id
        card["away"]["logo_badge"] = False
        old = gs._LOGO
        gs._LOGO = gs.cards.LogoCache(root.name, [])
        try:
            painted, panel, _surf = _paint(size, card)
        finally:
            gs._LOGO = old
        names = [item for item in painted if item[0] in ("Kansas City", "Denver")]
        logos = [item for item in painted if item[0] is None and (item[2].w, item[2].h) in ((168, 73), (112, 112))]
        self.assertEqual(len(logos), 2, [(item[2].size) for item in painted if item[0] is None])
        home_name = min(names, key=lambda item: item[2].centerx)
        away_name = max(names, key=lambda item: item[2].centerx)
        home = min(logos, key=lambda item: item[2].centerx)
        away = max(logos, key=lambda item: item[2].centerx)
        self.assertEqual(home[2].w, 168)
        self.assertLess(home[2].h, 112)
        self.assertEqual(home[2].h, 73)
        self.assertEqual((away[2].w, away[2].h), (112, 112))
        inset = layout(48)
        slot_w = layout(gs.SPORTS_LOGO_BOX_W)
        gap = layout(gs.SPORTS_LOGO_GAP)
        self.assertEqual(home[2].left, panel.left + inset)
        self.assertLessEqual(abs(away[2].centerx - (panel.right - inset - slot_w // 2)), 1)
        self.assertEqual(home_name[2].left, panel.left + inset + slot_w + gap)
        self.assertEqual(away_name[2].right, panel.right - inset - slot_w - gap)
        for item in logos + names:
            self.assertTrue(panel.contains(item[2]), item[2])
            self.assertFalse(home[2].colliderect(home_name[2]))
            self.assertFalse(away[2].colliderect(away_name[2]))


if __name__ == "__main__":
    unittest.main()
