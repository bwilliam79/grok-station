"""Named timers: parsing, order, cancel, ringing, relay persistence, monotonic clocks, display payload.

No network and no framebuffer. The display checks render with the dummy SDL driver.
"""
import json, os, sys, tempfile, unittest, urllib.parse

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "wake"))
import tools as T  # noqa: E402
import grok_station as gs  # noqa: E402
import grok_wake as gw  # noqa: E402
import pygame  # noqa: E402


def setUpModule():
    pygame.display.init()
    pygame.font.init()


def _id_for(timers, label):
    for k, t in timers.items():
        if t.get("label") == label:
            return k
    raise AssertionError(label)


class ParseNames(unittest.TestCase):
    def test_phrases(self):
        self.assertEqual(T.parse_timer_ref("stop"), ("any", None))
        self.assertEqual(T.parse_timer_ref("stop the timer"), ("any", None))
        self.assertEqual(T.parse_timer_ref(""), ("any", None))
        self.assertEqual(T.parse_timer_ref(None), ("any", None))
        self.assertEqual(T.parse_timer_ref("please dismiss it"), ("any", None))
        self.assertEqual(T.parse_timer_ref("stop the pasta timer"), ("name", "pasta"))
        self.assertEqual(T.parse_timer_ref("pasta timer"), ("name", "pasta"))
        self.assertEqual(T.parse_timer_ref("the oven"), ("name", "oven"))
        self.assertEqual(T.parse_timer_ref("Pasta"), ("name", "pasta"))
        self.assertEqual(T.parse_timer_ref("2"), ("index", 2))
        self.assertEqual(T.parse_timer_ref("#3"), ("index", 3))
        self.assertEqual(T.parse_timer_ref("timer 2"), ("index", 2))
        self.assertEqual(T.parse_timer_ref("second"), ("index", 2))
        self.assertEqual(T.parse_timer_ref("the second one"), ("index", 2))
        self.assertEqual(T.parse_timer_ref("stop 1"), ("index", 1))
        self.assertEqual(T.parse_timer_ref("dismiss the pasta timer"), ("name", "pasta"))

    def test_stored_names(self):
        timers = {}
        out, _ = T.set_timer({"seconds": 60, "label": "pasta timer"}, timers, 0)
        self.assertEqual(out["label"], "pasta")
        out, _ = T.set_timer({"seconds": 60, "label": "pasta <script>"}, timers, 0)
        self.assertEqual(out["label"], "pasta script")
        out, _ = T.set_timer({"seconds": 60}, timers, 0)
        self.assertEqual(out["label"], "Timer")
        out, _ = T.set_timer({"seconds": 60, "label": "timer"}, timers, 0)
        self.assertEqual(out["label"], "Timer 2")


class OrderCancelExtend(unittest.TestCase):
    def test_order_ringing_then_soonest(self):
        timers, now = {}, 1000.0
        T.set_timer({"seconds": 600, "label": "pasta"}, timers, now)
        T.set_timer({"seconds": 90, "label": "tea"}, timers, now)
        T.set_timer({"seconds": 30, "label": "eggs"}, timers, now)
        eggs = _id_for(timers, "eggs")
        timers[eggs]["ringing"] = True
        timers[eggs]["due"] = now
        labels = [t["label"] for t in T.list_timers(timers, now + 10)["timers"]]
        self.assertEqual(labels, ["eggs", "tea", "pasta"])
        self.assertEqual(T.list_timers(timers, now + 10)["timers"][0]["left"], "ringing")
        self.assertEqual(T.list_timers(timers, now + 10)["timers"][1]["index"], 2)

    def test_cancel_by_name_and_stop_and_index(self):
        timers, now = {}, 1000.0
        T.set_timer({"seconds": 600, "label": "pasta timer"}, timers, now)
        T.set_timer({"seconds": 90, "label": "tea"}, timers, now)
        T.set_timer({"seconds": 30, "label": "eggs"}, timers, now)
        eggs = _id_for(timers, "eggs")
        timers[eggs]["ringing"] = True
        timers[eggs]["due"] = now
        out, msg = T.cancel_timer({"label": "stop"}, timers, now + 5)
        self.assertEqual(out["cancelled"], "eggs")  # the one on the ringing screen, not the soonest still running
        self.assertEqual(msg["type"], "timer_cancel")
        self.assertNotIn(eggs, timers)
        out, msg = T.cancel_timer({"label": "stop the pasta timer"}, timers, now + 5)
        self.assertEqual(out["cancelled"], "pasta")
        self.assertEqual(msg["type"], "timer_cancel")
        out, msg = T.cancel_timer({"index": 1}, timers, now + 5)
        self.assertEqual(out["cancelled"], "tea")
        self.assertEqual(timers, {})

    def test_named_timer_beats_the_same_index_phrase(self):
        timers, now = {}, 1000.0
        T.set_timer({"seconds": 30, "label": "Timer 2"}, timers, now)  # sooner, so index 1
        T.set_timer({"seconds": 90, "label": "pasta"}, timers, now)    # index 2
        # A bare number is the index. "timer 2" is the stored name, which wins over index 2.
        hits, err = T.resolve_timer({"label": "2"}, timers, now)
        self.assertIsNone(err)
        self.assertEqual(timers[hits[0]]["label"], "pasta")
        out, msg = T.cancel_timer({"label": "timer 2"}, timers, now)
        self.assertEqual(out["cancelled"], "Timer 2")
        self.assertNotEqual(msg["id"], _id_for(timers, "pasta"))
        self.assertIn(_id_for(timers, "pasta"), timers)
        out, _ = T.cancel_timer({"label": "1"}, timers, now)
        self.assertEqual(out["cancelled"], "pasta")

    def test_extend_by_name_clears_ringing(self):
        timers, now = {}, 1000.0
        T.set_timer({"seconds": 30, "label": "pasta"}, timers, now)
        tid = _id_for(timers, "pasta")
        timers[tid]["ringing"] = True
        timers[tid]["due"] = now - 5
        out, msg = T.extend_timer({"seconds": 120, "label": "the pasta timer"}, timers, now)
        self.assertTrue(out["ok"])
        self.assertEqual(out["label"], "pasta")
        self.assertEqual(msg["type"], "timer_set")
        self.assertEqual(msg["id"], tid)
        self.assertEqual(msg["seconds"], 120)
        self.assertFalse(timers[tid]["ringing"])
        self.assertEqual(timers[tid]["due"], now + 120)
        listed = T.list_timers(timers, now)["timers"]
        self.assertEqual(listed[0]["left"], "2 minutes")
        self.assertFalse(listed[0]["ringing"])

    def test_ambiguous_name_asks(self):
        timers, now = {}, 1000.0
        T.set_timer({"seconds": 30, "label": "pasta"}, timers, now)
        T.set_timer({"seconds": 60, "label": "pasta"}, timers, now)
        out, msg = T.cancel_timer({"label": "pasta"}, timers, now)
        self.assertFalse(out["ok"])
        self.assertIsNone(msg)
        self.assertEqual(sorted(out["labels"]), ["pasta", "pasta"])

    def test_just_finished_is_still_stoppable(self):
        timers, now = {}, 1000.0
        T.set_timer({"seconds": 30, "label": "pasta"}, timers, now)
        T.set_timer({"seconds": 600, "label": "tea"}, timers, now)
        listed = T.list_timers(timers, now + 40)["timers"]
        self.assertEqual(listed[0]["label"], "pasta")
        self.assertEqual(listed[0]["left"], "ringing")
        self.assertTrue(listed[0]["ringing"])
        self.assertEqual(listed[1]["label"], "tea")
        out, msg = T.cancel_timer({"label": "stop"}, timers, now + 40)
        self.assertEqual(out["cancelled"], "pasta")
        self.assertEqual(msg["type"], "timer_cancel")
        self.assertIn(_id_for(timers, "tea"), timers)

    def test_hour_late_frees_the_slot(self):
        timers, now = {}, 1000.0
        T.set_timer({"seconds": 30, "label": "pasta"}, timers, now)
        tid = _id_for(timers, "pasta")
        timers[tid]["due"] = now - 4000
        dropped, changed = T.note_due(timers, now)
        self.assertEqual(dropped, [tid])
        self.assertTrue(changed)
        self.assertEqual(timers, {})
        out, msg = T.set_timer({"seconds": 60, "label": "tea"}, timers, now)
        self.assertTrue(out["ok"])
        self.assertEqual(msg["label"], "tea")


class Persistence(unittest.TestCase):
    def test_round_trip_shrinks_by_wall_time(self):
        path = os.path.join(tempfile.mkdtemp(), "timers.json")
        now = 5000.0
        timers = {"ab12cd": {"label": "pasta", "due": now + 100, "duration": 600, "ringing": False}}
        self.assertTrue(T.save_timer_state(path, timers, now, wall_now=1_700_000_000))
        loaded = T.load_timer_state(path, 1_700_000_030)
        self.assertAlmostEqual(loaded["ab12cd"]["remain"], 70, places=3)
        self.assertEqual(loaded["ab12cd"]["label"], "pasta")
        self.assertFalse(loaded["ab12cd"]["ringing"])
        restored = T.restore_timers(path, now=0, wall_now=1_700_000_030)
        self.assertAlmostEqual(restored["ab12cd"]["due"], 70, places=3)

    def test_bad_file_and_expired_are_dropped(self):
        path = os.path.join(tempfile.mkdtemp(), "timers.json")
        self.assertEqual(T.load_timer_state(path, 0), {})
        with open(path, "w") as f:
            f.write("not json")
        self.assertEqual(T.load_timer_state(path, 0), {})
        now = 10.0
        timers = {
            "ab12cd": {"label": "pasta", "due": now - 5, "duration": 30, "ringing": False},
            "cd34ef": {"label": "oven", "due": now - 1, "duration": 30, "ringing": True},
            "../x": {"label": "nope", "due": now + 10, "duration": 10, "ringing": False},
        }
        T.save_timer_state(path, timers, now, wall_now=100)
        loaded = T.load_timer_state(path, 100)
        self.assertEqual(list(loaded), ["cd34ef"])
        self.assertTrue(loaded["cd34ef"]["ringing"])

    def test_monotonic_clock_ignores_a_wall_jump(self):
        state = {"w": 1000.0, "m": 50.0}
        clock = T.MonoClock(wall=lambda: state["w"], mono=lambda: state["m"])
        due = clock.now() + 100
        state["w"] += 4000  # NTP steps the wall clock forward
        state["m"] += 10    # ten real seconds
        self.assertAlmostEqual(due - clock.now(), 90, places=3)

    def test_just_finished_is_saved_as_ringing(self):
        path = os.path.join(tempfile.mkdtemp(), "timers.json")
        timers, now = {}, 5000.0
        T.set_timer({"seconds": 30, "label": "pasta"}, timers, now)
        tid = _id_for(timers, "pasta")
        T.list_timers(timers, now + 40)
        self.assertTrue(timers[tid]["ringing"])
        self.assertTrue(T.save_timer_state(path, timers, now + 40, wall_now=100))
        loaded = T.load_timer_state(path, 100)
        self.assertTrue(loaded[tid]["ringing"])
        self.assertGreater(loaded[tid]["remain"], -3600)

    def test_restore_messages_hand_ringing_back_as_zero(self):
        now = 1000.0
        timers = {
            "ab12cd": {"label": "pasta", "due": now - 5, "duration": 30, "ringing": True},
            "cd34ef": {"label": "oven", "due": now + 90, "duration": 180, "ringing": False},
        }
        msgs = T.timer_restore_messages(timers, now)
        by_id = {m["id"]: m for m in msgs}
        self.assertEqual(by_id["ab12cd"], {"type": "timer_set", "id": "ab12cd", "seconds": 0, "label": "pasta"})
        self.assertEqual(by_id["cd34ef"]["seconds"], 90)
        self.assertEqual(by_id["cd34ef"]["label"], "oven")


class WakeMonotonic(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "timers.json")
        self.old = (gw.TIMERS_FILE, dict(gw.TIMERS), gw.LIVE["play"], gw.LIVE["session"], gw.time.time, gw.time.monotonic,
                    gw.subprocess.run, gw.time.sleep, gw.urllib.request.urlopen)
        gw.TIMERS_FILE = self.path
        gw.TIMERS.clear()
        gw.LIVE["play"] = "dummy"
        gw.LIVE["session"] = False
        gw.subprocess.run = lambda *a, **k: None
        gw.time.sleep = lambda s: None
        gw.urllib.request.urlopen = lambda *a, **k: type("R", (), {"read": lambda self: b"{}"})()
        self.wall = {"w": 1_700_000_000.0, "m": 100.0}
        gw.time.time = lambda: self.wall["w"]
        gw.time.monotonic = lambda: self.wall["m"]

    def tearDown(self):
        (gw.TIMERS_FILE, saved, gw.LIVE["play"], gw.LIVE["session"], gw.time.time, gw.time.monotonic,
         gw.subprocess.run, gw.time.sleep, gw.urllib.request.urlopen) = self.old
        gw.TIMERS.clear()
        gw.TIMERS.update(saved)

    def test_wall_jump_does_not_ring_early_and_file_tracks_monotonic(self):
        gw.timer_event({"type": "timer_set", "id": "ab12cd", "seconds": 100, "label": "pasta"})
        self.wall["w"] += 5000
        self.wall["m"] += 10
        self.assertGreater(gw._timer_left(gw.TIMERS["ab12cd"]), 80)
        gw.timers_tick()
        self.assertFalse(gw.TIMERS["ab12cd"].get("ringing"))
        gw.timers_save()
        saved = json.load(open(self.path))
        self.assertNotIn("mono_due", saved["ab12cd"])
        self.assertAlmostEqual(saved["ab12cd"]["due"], self.wall["w"] + 90, delta=1)
        self.wall["m"] += 100
        gw.timers_tick()
        self.assertTrue(gw.TIMERS["ab12cd"].get("ringing"))

    def test_replace_at_the_cap_and_reload(self):
        for i in range(gw.MAX_STATION_TIMERS):
            gw.timer_event({"type": "timer_set", "id": f"aa{i:04d}", "seconds": 60, "label": "tea"})
        gw.timer_event({"type": "timer_set", "id": "bb0001", "seconds": 60, "label": "extra"})
        self.assertNotIn("bb0001", gw.TIMERS)
        gw.timer_event({"type": "timer_set", "id": "aa0000", "seconds": 30, "label": "tea"})
        self.assertIn("aa0000", gw.TIMERS)
        self.assertEqual(len(gw.TIMERS), gw.MAX_STATION_TIMERS)
        self.assertAlmostEqual(gw._timer_left(gw.TIMERS["aa0000"]), 30, delta=1)
        gw.TIMERS.clear()
        gw.timers_load()
        self.assertEqual(len(gw.TIMERS), gw.MAX_STATION_TIMERS)
        self.assertIn("aa0000", gw.TIMERS)
        q = json.loads(urllib.parse.unquote(gw.timers_query()))
        self.assertTrue(any(item.get("duration") == 30 for item in q))

    def test_query_keeps_an_overdue_timer_for_stop(self):
        gw.timer_event({"type": "timer_set", "id": "ab12cd", "seconds": 5, "label": "pasta"})
        self.wall["m"] += 10
        q = json.loads(urllib.parse.unquote(gw.timers_query()))
        self.assertEqual(q[0]["id"], "ab12cd")
        self.assertTrue(q[0]["ringing"])
        self.assertEqual(q[0]["left"], 0)
        self.assertFalse(gw.TIMERS["ab12cd"].get("ringing"))  # the chime is still owed

    def test_zero_seconds_restores_ringing_and_stops_at_the_cap(self):
        gw.timer_event({"type": "timer_set", "id": "ab12cd", "seconds": 0, "label": "pasta"})
        self.assertTrue(gw.TIMERS["ab12cd"]["ringing"])
        self.assertLessEqual(gw._timer_left(gw.TIMERS["ab12cd"]), 0)
        for i in range(gw.MAX_STATION_TIMERS - 1):
            gw.timer_event({"type": "timer_set", "id": f"cc{i:04d}", "seconds": 30, "label": "tea"})
        gw.timer_event({"type": "timer_set", "id": "bb0001", "seconds": 0, "label": "extra"})
        self.assertNotIn("bb0001", gw.TIMERS)
        self.assertEqual(len(gw.TIMERS), gw.MAX_STATION_TIMERS)


class DisplayPayload(unittest.TestCase):
    def setUp(self):
        self.old_epoch = gs.TIMER_EPOCH
        self.old_chips = list(gs.TIMER_CHIPS)
        gs.TIMER_EPOCH = None
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        gs.TIMER_EPOCH = self.old_epoch
        gs.TIMER_CHIPS[:] = self.old_chips

    def _chips(self):
        epoch = 1_700_000_000
        return epoch, [
            {"id": "aaaa01", "label": "pasta", "ends_at_epoch": epoch + 462, "duration": 600, "ringing": False},
            {"id": "aaaa02", "label": "oven", "ends_at_epoch": epoch + 810, "duration": 1800, "ringing": False},
            {"id": "aaaa03", "label": "tea", "ends_at_epoch": epoch + 90, "duration": 120, "ringing": False},
            {"id": "aaaa04", "label": "eggs", "ends_at_epoch": epoch + 50, "duration": 60, "ringing": False},
            {"id": "aaaa05", "label": "rice", "ends_at_epoch": epoch + 3700, "duration": 4000, "ringing": False},
        ]

    def test_rings_overflow_and_order(self):
        epoch, chips = self._chips()
        payload = gs.timer_screen(chips, epoch)
        self.assertEqual(payload["mode"], "rings")
        self.assertEqual([r["title"] for r in payload["rings"]], ["Eggs", "Tea", "Pasta", "Oven"])
        self.assertEqual(payload["rings"][2]["text"], "7:42")
        self.assertEqual(payload["rings"][3]["text"], "13:30")
        self.assertEqual(payload["rings"][2]["color"], "teal")
        self.assertAlmostEqual(payload["rings"][2]["fraction"], 462 / 600, places=3)
        self.assertEqual([c["title"] for c in payload["chips"]], ["Rice"])
        self.assertEqual(payload["chips"][0]["text"], "1:01:40")
        self.assertIsNone(payload["headline"])
        self.assertNotIn("red", json.dumps(payload))

    def test_ringing_is_amber_and_others_are_chips(self):
        epoch, chips = self._chips()
        chips[0]["ringing"] = True
        chips[0]["ends_at_epoch"] = epoch - 5
        payload = gs.timer_screen(chips, epoch)
        self.assertEqual(payload["mode"], "ringing")
        self.assertEqual(len(payload["rings"]), 1)
        self.assertEqual(payload["rings"][0]["color"], "amber")
        self.assertEqual(payload["rings"][0]["text"], "0:00")
        self.assertEqual(payload["rings"][0]["fraction"], 1.0)
        self.assertEqual(payload["headline"], "Pasta\nis done")
        self.assertIn("stop", payload["hint"])
        self.assertNotIn("Pasta", [c["title"] for c in payload["chips"]])
        self.assertEqual(len(payload["chips"]), 4)
        self.assertNotIn("red", json.dumps(payload))

    def test_monotonic_anchor_ignores_the_wall_clock(self):
        chip = {"id": "abcd12", "label": "pasta", "ends_at_epoch": 1_700_000_100, "ringing": False, "duration": 100}
        gs.anchor_timer_chips([chip], wall=1_700_000_000, mono=10)
        old = gs.time.monotonic
        gs.time.monotonic = lambda: 15.0
        try:
            self.assertAlmostEqual(gs.chip_left(chip, None), 95, places=3)
            self.assertAlmostEqual(gs.chip_left(chip, 1_700_000_050), 50, places=3)
        finally:
            gs.time.monotonic = old

    def test_screen_choice(self):
        self.assertEqual(gs.screen_for(None, False, None, False, timers=True), "timers")
        self.assertEqual(gs.screen_for(None, False, None, True, timers=True), "night")
        self.assertEqual(gs.screen_for(None, False, {"text": "hi"}, False, timers=True), "session")
        self.assertEqual(gs.screen_for(None, False, None, False), "idle")
        self.assertIs(gs.SCREENS["timers"], gs.draw_timers)

    def _red_pixels(self, surf):
        red = 0
        w, h = surf.get_size()
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                c = surf.get_at((x, y))
                if c.r > 200 and c.g < 100 and c.b < 100:
                    red += 1
        return red

    def test_draw_has_no_red_and_the_ring_pulses(self):
        epoch, chips = self._chips()
        gs.TIMER_CHIPS[:] = [dict(c) for c in chips[:2]]
        now = __import__("time").strptime("2026-10-19 17:05", "%Y-%m-%d %H:%M")
        size = (1280, 800)
        surf = pygame.Surface(size)
        gs.draw_timers(surf, gs.Layout(*size), now, epoch)
        self.assertEqual(self._red_pixels(surf), 0)
        self.assertIsNotNone(gs.LAST_CLOCK_RECT)
        gs.TIMER_CHIPS[0]["ringing"] = True
        gs.TIMER_CHIPS[0]["ends_at_epoch"] = epoch - 3
        seen = []
        real = gs.font

        class RecordingFont:
            def __init__(self, inner):
                self._inner = inner

            def render(self, text, *a, **k):
                seen.append(str(text))
                return self._inner.render(text, *a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        gs.font = lambda kind, px: RecordingFont(real(kind, px))
        try:
            a = pygame.Surface(size)
            gs.draw_timers(a, gs.Layout(*size), now, float(epoch))
            b = pygame.Surface(size)
            gs.draw_timers(b, gs.Layout(*size), now, float(epoch) + 0.5)
        finally:
            gs.font = real
        self.assertEqual(self._red_pixels(a), 0)
        self.assertEqual(self._red_pixels(b), 0)
        self.assertNotEqual(bytes(a.get_buffer().raw), bytes(b.get_buffer().raw))
        blob = "\n".join(seen)
        self.assertIn("Pasta", blob)
        self.assertIn("is done", blob)
        self.assertIn("0:00", blob)
        self.assertIn("stop", blob)
        self.assertIn("Oven", blob)

    def test_past_due_without_a_flag_is_the_ringing_screen(self):
        epoch, chips = self._chips()
        chips[2]["ringing"] = False
        chips[2]["ends_at_epoch"] = epoch - 2  # tea, countdown finished, chime not posted yet
        payload = gs.timer_screen(chips, epoch)
        self.assertEqual(payload["mode"], "ringing")
        self.assertEqual(payload["rings"][0]["title"], "Tea")
        self.assertEqual(payload["rings"][0]["color"], "amber")
        self.assertEqual(payload["rings"][0]["text"], "0:00")
        self.assertEqual(payload["headline"], "Tea\nis done")
        self.assertNotIn("Tea", [c["title"] for c in payload["chips"]])
        self.assertNotIn("red", json.dumps(payload))

    def test_rings_stay_in_the_safe_area_at_each_size(self):
        epoch, chips = self._chips()
        now = __import__("time").strptime("2026-10-19 17:05", "%Y-%m-%d %H:%M")
        sizes = ((800, 480), (1024, 768), (1280, 800), (1920, 1080))
        for size in sizes:
            side, top, bot = gs.safe_insets(*size)
            safe = pygame.Rect(side, top, size[0] - 2 * side, size[1] - top - bot)
            for ringing in (False, True):
                gs.TIMER_CHIPS[:] = [dict(c) for c in chips]
                if ringing:
                    gs.TIMER_CHIPS[0]["ringing"] = True
                    gs.TIMER_CHIPS[0]["ends_at_epoch"] = epoch - 3
                surf = pygame.Surface(size)
                payload = gs.draw_timers(surf, gs.Layout(*size), now, epoch)
                self.assertIsNotNone(gs.LAST_TOPBAR_RECT, size)
                self.assertFalse(gs.LAST_TOPBAR_RECT.colliderect(gs.LAST_TIMER_RECT) if payload["chips"] else False, size)
                if payload["chips"]:
                    self.assertTrue(safe.contains(gs.LAST_TIMER_RECT), size)
                    self.assertLessEqual(gs.LAST_TOPBAR_RECT.bottom, gs.LAST_TIMER_RECT.top, size)
                self.assertGreaterEqual(gs.LAST_CLOCK_RECT.right, size[0] - side - 2, size)
                self.assertLessEqual(gs.LAST_CLOCK_RECT.right, size[0] - side + 1, size)
                self.assertEqual(self._red_pixels(surf), 0, size)
