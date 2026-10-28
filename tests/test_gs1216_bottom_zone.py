"""0.12.16 bottom zone (Noor's SPEC rev2): score tiles and bot states use the bottom of the main screen, the bot
status moves to the upper left while a tile is up, a recipe stays full screen with tiles as chips, and the reply
window scrolls like a teleprompter. The scenario tests drive the real main() loop with scripted events and a
fake clock, and check what each repaint drew."""
import json, os, sys, time, unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import grok_station as gs  # noqa: E402
import cards  # noqa: E402
import pygame  # noqa: E402

T0 = time.mktime(time.strptime("2026-10-18 14:00", "%Y-%m-%d %H:%M"))  # daytime: no night clock
SPORTS = {"v": 1, "type": "sports", "league": "City league", "home": {"name": "Harbor City", "abbr": "HC"},
          "away": {"name": "Ridgeview", "abbr": "RV"}, "home_score": 3, "away_score": 2, "status": "live",
          "period": "2nd", "schedule": [{"label": "Harbor City at Ridgeview", "when": "Sat 7pm"}]}
CALENDAR = {"v": 1, "type": "calendar", "date": "2026-10-22", "events": [{"time": "6:30", "title": "Practice"}]}
RECIPE = {"v": 1, "type": "recipe", "title": "Weeknight soup", "servings": 4, "ingredients": ["onion", "stock"],
          "steps": ["Chop the vegetables.", "Simmer twenty minutes.", "Season and serve."], "step": 1}
LONG = ("Tomorrow looks mild: partly sunny, a high of 71 and a low of 52, with a light breeze in the afternoon. "
        "No rain expected, so it is a good day for a walk by the harbor, and the evening should stay clear enough "
        "to see the stars over the ridge before the clouds roll back in on Sunday morning.")


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class Stop(Exception):
    pass


class FakeTime:
    """time with a settable clock; everything else is the real module."""

    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t

    def localtime(self, secs=None):
        return time.localtime(self.t if secs is None else secs)

    def sleep(self, _s):
        pass

    def __getattr__(self, name):
        return getattr(time, name)


class Harness:
    """Runs gs.main() on a 2560x1706 surface. Each script step returns the next event; every repaint is logged."""

    def __init__(self, size=(2560, 1706), env=None):
        self.size, self.env = size, env or {}
        self.clock = FakeTime(T0)
        self.frames = []
        self.script = []

    # --- events
    def state(self, st=None, **kw):
        body = dict(kw)
        if st:
            body["state"] = st
        return lambda: pygame.event.Event(gs.STATE, **gs.state_event(body))

    def put(self, bot, cid, card, ttl=600):
        def step():
            gs.deck().put(bot, cid, 10, ttl, json.loads(json.dumps(card)), self.clock.t)
            tile = {"bot": bot, "id": cid, "card": gs.deck().get(bot, cid), "expires": gs.deck().expiry(bot, cid)}
            return pygame.event.Event(gs.FEED, tile=tile)
        return step

    def recipe_cmd(self, cmd):
        def step():
            result = cards.apply_recipe_command(gs.deck(), cmd, self.clock.t)
            if result.get("closed"):
                return pygame.event.Event(gs.FEED, tile=None, recipe_closed=True)
            page = gs.deck().get(result["bot"], result["id"])
            return pygame.event.Event(gs.FEED, tile={"bot": result["bot"], "id": result["id"], "card": page,
                                                     "step_cmd": True}, recipe_cmd=True)
        return step

    def advance(self, s):
        def step():
            self.clock.t += s
            return pygame.event.Event(pygame.NOEVENT)
        return step

    def call(self, fn):
        def step():
            fn()
            return pygame.event.Event(pygame.NOEVENT)
        return step

    # --- run
    def run(self, *steps):
        self.script = list(steps)
        h = self
        real = {k: getattr(gs, k) for k in ("Display", "Weather", "ThreadingHTTPServer", "status_loop", "refresh_dim",
                                              "read_setup", "FADE", "SNAP", "time", "DECK", "CHIPS", "night_face")}
        real_wait = pygame.event.wait

        class Disp(real["Display"]):
            def __init__(self):
                super().__init__(fb=None, size=h.size)

            def show(self, painter, *args, fade=False, snapshot=True):
                painter(self.screen, self.L, *args)
                h.frames.append(h.snap(painter, args, self.screen))

        class Wx:
            data = {"temp": 64, "code": 2, "day": False, "hi": 71, "lo": 52}

            def offline(self, now=None):
                return False

            def loop(self):
                pass

        class Srv:
            def __init__(self, *a, **k):
                pass

            def serve_forever(self):
                pass

        def wait(_ms):
            ev = pygame.event.poll()
            if ev.type != pygame.NOEVENT:
                return ev
            if not h.script:
                raise Stop
            return h.script.pop(0)()

        old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        gs.Display, gs.Weather, gs.ThreadingHTTPServer = Disp, Wx, Srv
        gs.status_loop, gs.refresh_dim, gs.read_setup = (lambda: None), (lambda force=False: None), (lambda: None)
        gs.FADE, gs.SNAP, gs.time = False, "", self.clock
        gs.night_face = lambda now: False
        gs.DECK, gs.CHIPS = cards.Deck(rotate_s=20, ttl_max=86400), gs.ChipTray()
        self.deck, self.chips = gs.DECK, gs.CHIPS
        gs.TILE.clear()
        gs.TIMER_CHIPS[:] = []
        pygame.event.wait = wait
        pygame.event.clear()
        try:
            gs.main()
        except Stop:
            pass
        finally:
            pygame.event.wait = real_wait
            for k, v in real.items():
                setattr(gs, k, v)
            gs.TILE.clear()
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.frames = self.frames[1:]  # the first frame is main()'s startup paint
        return self.frames

    def snap(self, painter, args, screen):
        zone = next((a for a in args if isinstance(a, dict) and "tile" in a and "voice" in a), None)
        chips = next((a for a in args if isinstance(a, list)), None)
        tele = next((a for a in args if isinstance(a, gs.Teleprompter)), None)
        return {"painter": painter.__name__, "zone": zone, "chips": [c["card"]["type"] for c in chips or []],
                "zone_rect": gs.LAST_ZONE_RECT, "badge": gs.LAST_BADGE_RECT, "portrait": gs.LAST_PORTRAIT_RECT,
                "clock": gs.LAST_CLOCK_RECT, "date": gs.LAST_DATE_RECT, "weather": gs.LAST_WEATHER_RECT,
                "topbar": gs.LAST_TOPBAR_RECT, "chip_rects": list(gs.LAST_CHIP_RECTS),
                "tele": tele, "tele_rect": (gs.LAST_TELE or {}).get("rect"), "t": self.clock.t}


def clock_ink_top(size):
    """Top of the clock digits' ink on the idle screen (the badge must stay above it)."""
    surf = pygame.Surface(size)
    L = gs.Layout(*size)
    gs.draw_idle(surf, L, time.localtime(T0), type("W", (), {"data": None, "offline": lambda s: False})(), True)
    r = gs.LAST_CLOCK_RECT
    box = surf.subsurface(r).get_bounding_rect(min_alpha=1)
    for y in range(r.top, r.bottom):
        if any(tuple(surf.get_at((x, y)))[:3] != tuple(gs.BG)[:3] for x in range(r.left, r.right, 3)):
            return y
    return r.top + box.top


class Scenarios(unittest.TestCase):
    def assert_clear_of_top(self, f):
        for name in ("clock", "date", "weather"):
            if f[name] is not None:
                self.assertFalse(f["zone_rect"].colliderect(f[name]), name)

    def test_idle_has_an_empty_zone(self):
        h = Harness()
        h.run(h.advance(61))
        frames = h.frames
        self.assertEqual(frames[-1]["painter"], "draw_idle")
        self.assertIsNone(frames[0]["zone_rect"])

    def test_score_tile_uses_only_the_bottom_zone(self):
        h = Harness()
        frames = h.run(h.put("jules", "game", SPORTS))
        f = frames[-1]
        self.assertEqual(f["painter"], "draw_zone")
        self.assertEqual(f["zone"]["tile"]["type"], "sports")
        z = f["zone_rect"]
        L = gs.Layout(2560, 1706)
        self.assertEqual((z.left, z.width), (128, 2304))       # spec x 128-2432
        self.assertTrue(780 <= z.top <= 830, z)                # spec y 800 (date bottom + L(80))
        self.assertEqual(z.bottom, gs.content_limit(L))        # spec: bottom is content_limit()
        self.assert_clear_of_top(f)
        self.assertIsNone(f["badge"])

    def test_zone_bottom_moves_up_for_timers(self):
        h = Harness()
        timers = [{"id": "aaaa01", "label": "pasta", "ends_at_epoch": T0 + 400, "duration": 600}]
        frames = h.run(h.put("jules", "game", SPORTS), h.state(None, timers=timers), h.advance(1))
        last = [f for f in frames if f["painter"] == "draw_zone"][-1]
        self.assertLess(last["zone_rect"].bottom, frames[0]["zone_rect"].bottom)
        self.assertEqual(last["zone_rect"].bottom, gs.timer_strip_rect(gs.Layout(2560, 1706)).top
                         - max(6, round(0.008 * 1706)))

    def test_each_bot_state_fills_the_zone(self):
        h = Harness()
        frames = h.run(h.state("listening"), h.state("thinking", heard="what's on tomorrow"),
                       h.state("speaking", heard="what's on tomorrow", text="Practice at 6:30."))
        states = [f["zone"]["state"] for f in frames if f["painter"] == "draw_zone" and f["zone"]["voice"]]
        self.assertEqual(states, ["listening", "thinking", "speaking"])
        for f in frames:
            self.assertTrue(f["zone_rect"].contains(f["portrait"]))  # the avatar sits inside the zone card
            self.assertIsNone(f["badge"])
            self.assert_clear_of_top(f)
            self.assertGreater(f["clock"].height, 300)  # the clock keeps its size (300 px @1080 spec)

    def test_tile_during_a_reply_moves_status_to_the_upper_left_and_back(self):
        h = Harness()
        frames = h.run(h.state("speaking", text=LONG), h.put("jules", "game", SPORTS),
                       h.state("listening"), h.state("thinking"), h.state("speaking", text="Anything else?"))
        tile_up = frames[1]
        self.assertEqual(tile_up["painter"], "draw_zone")
        self.assertEqual(tile_up["zone"]["tile"]["type"], "sports")
        self.assertEqual(tile_up["zone"]["badge"], "speaking")
        self.assertIsNone(tile_up["tele_rect"])  # reply text hidden while a tile is up, no caption
        b = tile_up["badge"]
        self.assertLess(b.bottom, clock_ink_top((2560, 1706)))  # the badge never touches the clock digits
        self.assertLess(b.left, 200)
        self.assertEqual(frames[2]["zone"]["badge"], "listening")
        self.assertEqual(frames[3]["zone"]["badge"], "thinking")
        back = frames[4]  # the stale tile clears when the response starts: the bot expands back into the zone
        self.assertIsNone(back["zone"]["tile"])
        self.assertEqual(back["zone"]["state"], "speaking")
        self.assertIsNone(back["badge"])

    def test_recipe_stays_full_screen_and_a_tile_becomes_a_chip(self):
        h = Harness()
        frames = h.run(h.put("jules", "soup", RECIPE), h.state("listening"), h.recipe_cmd("next"),
                       h.put("mira", "game", SPORTS))
        f = frames[-1]
        self.assertEqual(f["painter"], "draw_feed")
        self.assertEqual(f["chips"], ["sports"])
        self.assertIsNotNone(h.deck.get("jules", "soup"))  # the score card did not end the recipe
        chip = f["chip_rects"][0]
        L = gs.Layout(2560, 1706)
        self.assertLessEqual(chip.right, f["topbar"].left - L(36) + 1)  # left of the clock
        self.assertLess(chip.bottom, L(120))                            # above the recipe cards
        self.assertEqual(chip.height, L(62))                            # 82 px pill at 2560x1706
        self.assertFalse(chip.colliderect(f["badge"]))                  # clear of the upper-left status

    def test_chip_dismiss_by_voice(self):
        h = Harness()
        got = []
        frames = h.run(h.put("jules", "soup", RECIPE), h.put("mira", "game", SPORTS),
                       h.call(lambda: got.append(gs.dismiss_chip(h.clock.t))),
                       h.call(lambda: got.append(gs.dismiss_chip(h.clock.t))), h.advance(0.1))
        self.assertEqual(got[0]["id"], "game")
        self.assertIsNone(got[1])  # nothing left: the station says no and "dismiss" goes to the bot
        self.assertEqual(frames[-2]["chips"], ["sports"])
        self.assertEqual(frames[-1]["chips"], [])  # repainted without it
        self.assertIsNone(h.deck.get("mira", "game"))
        self.assertFalse(h.chips)

    def test_chip_expires_on_the_tile_ttl(self):
        h = Harness()
        frames = h.run(h.put("jules", "soup", RECIPE), h.put("mira", "game", SPORTS, ttl=30),
                       h.advance(20), h.advance(11))
        self.assertEqual(frames[1]["chips"], ["sports"])
        self.assertEqual(frames[-1]["painter"], "draw_feed")
        self.assertEqual(frames[-1]["chips"], [])

    def test_chip_moves_to_the_zone_when_the_recipe_ends(self):
        h = Harness()  # in a session
        frames = h.run(h.state("listening"), h.put("jules", "soup", RECIPE), h.put("mira", "game", SPORTS),
                       h.recipe_cmd("close"))
        f = frames[-1]
        self.assertEqual(f["painter"], "draw_zone")
        self.assertEqual(f["zone"]["tile"]["type"], "sports")
        self.assertEqual(f["zone"]["badge"], "listening")
        self.assertFalse(h.chips)
        h2 = Harness()  # idle
        frames = h2.run(h2.put("jules", "soup", RECIPE), h2.put("mira", "game", SPORTS), h2.recipe_cmd("close"))
        self.assertEqual(frames[-1]["painter"], "draw_zone")
        self.assertEqual(frames[-1]["zone"]["tile"]["type"], "sports")

    def test_an_expired_chip_does_not_come_back_after_the_recipe(self):
        h = Harness()
        frames = h.run(h.state("listening"), h.put("jules", "soup", RECIPE), h.put("mira", "game", SPORTS, ttl=5),
                       h.advance(6), h.recipe_cmd("close"))
        self.assertEqual(frames[-1]["painter"], "draw_zone")
        self.assertIsNone(frames[-1]["zone"]["tile"])

    def test_third_chip_replaces_the_oldest(self):
        h = Harness()
        frames = h.run(h.put("jules", "soup", RECIPE), h.put("mira", "game", SPORTS),
                       h.put("hank", "day", CALENDAR), h.put("kai", "game2", SPORTS))
        self.assertEqual(frames[-1]["chips"], ["calendar", "sports"])
        self.assertEqual([c["id"] for c in h.chips.visible(h.clock.t)], ["day", "game2"])
        r = frames[-1]["chip_rects"]
        self.assertLess(r[0].right, r[1].left)  # newest nearest the clock

    def test_teleprompter_follows_speech_progress(self):
        h = Harness()
        steps = [h.state("speaking", text=LONG)]
        for i in range(1, 11):
            steps += [h.state(None, speech={"p": i / 10}), h.advance(0.2)]
        steps += [h.advance(2.0), h.state("speaking", text=LONG, done=True)]
        frames = h.run(*steps)
        tele = frames[0]["tele"]
        self.assertIsNotNone(tele)
        self.assertTrue(tele.scrolls)
        self.assertEqual(tele.win_h, 4 * tele.line_h)
        self.assertEqual(tele.line_h, 100)  # 85 px at line height 1.18
        self.assertTrue(tele.settled)
        self.assertEqual(tele.offset, tele.max_offset)  # last 4 lines once speech ends
        self.assertEqual(tele.fades(), (False, False))
        self.assertEqual(frames[0]["tele_rect"].height, 400)
        self.assertTrue(frames[0]["zone_rect"].contains(frames[0]["tele_rect"]))

    def test_short_reply_does_not_scroll(self):
        h = Harness()
        frames = h.run(h.state("speaking", text="Practice at 6:30."), h.state(None, speech={"p": 0.9}), h.advance(1))
        tele = frames[0]["tele"]
        self.assertFalse(tele.scrolls)
        self.assertEqual(tele.step(T0 + 5), 0.0)
        self.assertEqual(tele.fades(), (False, False))

    def test_barge_in_stops_the_window_in_place(self):
        h = Harness()
        seen = {}
        frames = h.run(h.state("speaking", text=LONG), h.state(None, speech={"p": 0.5}), h.advance(1.0),
                       h.call(lambda: seen.update(off=frames_tele(h).offset)), h.state("listening"), h.advance(2.0))
        tele = frames[0]["tele"]
        self.assertTrue(tele.stopped)
        self.assertGreater(seen["off"], 0)
        self.assertAlmostEqual(tele.offset, seen["off"], delta=1)  # frozen where it was
        self.assertEqual(frames[-1]["zone"]["state"], "listening")

    def test_a_new_tile_stops_the_window(self):
        h = Harness()
        frames = h.run(h.state("speaking", text=LONG), h.state(None, speech={"p": 0.5}), h.advance(1.0),
                       h.put("jules", "game", SPORTS), h.state(None, speech={"p": 0.9}), h.advance(1.0))
        tele = frames[0]["tele"]
        self.assertTrue(tele.stopped)
        self.assertLess(tele.progress, 0.9)

    def test_fallback_pace_without_progress(self):
        h = Harness()
        frames = h.run(h.state("speaking", text=LONG), h.advance(1.4), h.advance(10))
        tele = frames[0]["tele"]
        self.assertEqual(tele.target(T0 + 1.4), 0.0)  # held for 1.5 s
        self.assertAlmostEqual(tele.target(T0 + 3.5) - tele.target(T0 + 2.5), 60, delta=1)  # 60 px/s at 2560
        self.assertGreater(tele.offset, 0)


def frames_tele(h):
    return h.frames[1]["tele"]  # frames[0] is the startup paint while the script runs


class TeleprompterUnit(unittest.TestCase):
    def make(self, lines=12, reduced=False):
        t = gs.Teleprompter(0.0, reduced)
        t.set_geometry(lines, 100, 4 / 3)
        return t

    def test_spoken_line_sits_near_line_two(self):
        t = self.make()
        t.feed(0.5, 0)
        self.assertEqual(t.target(0), 0.5 * 1200 - 150)
        for i in range(60):
            t.step(i / 30)
        self.assertAlmostEqual(t.offset, 450, delta=2)  # eased onto the target
        self.assertEqual(t.fades(), (True, True))

    def test_ease_never_jumps(self):
        t = self.make()
        t.step(0)
        t.feed(1.0, 0)
        first = t.step(1 / 30)
        self.assertLess(first, 0.2 * t.max_offset)

    def test_reduced_motion_pages_with_a_crossfade(self):
        t = self.make(reduced=True)
        t.feed(0.2, 0)
        self.assertEqual(t.step(0), 0)
        t.feed(0.4, 1)
        self.assertEqual(t.step(1), 400)  # a whole 4-line page
        fade = t.crossfade(1.1)
        self.assertEqual(fade[0], 0)
        self.assertAlmostEqual(fade[1], 0.5, places=3)
        self.assertIsNone(t.crossfade(1.25))
        self.assertTrue(gs.reduce_motion({"GROK_STATION_REDUCE_MOTION": "1"}))
        self.assertFalse(gs.reduce_motion({}))

    def test_tele_next(self):
        t = gs.tele_next(None, "thinking", "speaking", False, 0)
        self.assertIsNotNone(t)
        self.assertIs(gs.tele_next(t, "speaking", "speaking", False, 1), t)  # reply text update keeps the clock
        self.assertIs(gs.tele_next(t, "speaking", "speaking", True, 2), t)
        self.assertTrue(t.settled)
        self.assertIsNone(gs.tele_next(t, "speaking", "listening", False, 3))


class ChipsUnit(unittest.TestCase):
    def test_tray(self):
        tray = gs.ChipTray()
        tray.add({"bot": "a", "id": "1", "card": SPORTS, "expires": 50}, 0)
        tray.add({"bot": "b", "id": "2", "card": CALENDAR, "expires": 100}, 1)
        dropped = tray.add({"bot": "c", "id": "3", "card": SPORTS, "expires": 100}, 2)
        self.assertEqual(dropped["id"], "1")
        self.assertEqual([c["id"] for c in tray.visible(3)], ["2", "3"])
        self.assertEqual(tray.dismiss(3)["id"], "3")
        self.assertEqual(tray.handoff(4)["id"], "2")
        self.assertFalse(tray)

    def test_chip_text(self):
        self.assertEqual(gs.chip_text(SPORTS), ("LIVE", True, "City league", "HC 3 – 2 RV"))
        self.assertEqual(gs.chip_text(CALENDAR)[3], "Next 6:30 · Practice")


class Contrast(unittest.TestCase):
    def test_league_label_is_muted(self):
        calls = []
        real = gs._line

        def line(surf, kind, px, text, color, width):
            calls.append((text, tuple(color)))
            return real(surf, kind, px, text, color, width)

        gs._line = line
        try:
            gs.draw_sports(pygame.Surface((2560, 1706)), gs.Layout(2560, 1706), SPORTS, pygame.Rect(128, 800, 2304, 780))
        finally:
            gs._line = real
        self.assertIn(("City league", tuple(gs.MUTED)), calls)


class OtherSizes(unittest.TestCase):
    def test_zone_and_window_fit(self):
        for size in ((1920, 1080), (1280, 800), (800, 480)):
            h = Harness(size=size)
            frames = h.run(h.state("speaking", heard="what's on tomorrow", text=LONG), h.put("jules", "game", SPORTS))
            f = frames[0]
            self.assertTrue(f["zone_rect"].contains(f["tele_rect"]), size)
            self.assertEqual(f["tele_rect"].height, 4 * f["tele"].line_h)
            for name in ("clock", "date", "weather"):
                if f[name] is not None:
                    self.assertFalse(f["zone_rect"].colliderect(f[name]), (size, name))
            self.assertLess(frames[1]["badge"].bottom, clock_ink_top(size), size)


class Wiring(unittest.TestCase):
    def test_speech_progress_post(self):
        ev = gs.state_event({"speech": {"p": 0.42}})
        self.assertEqual(ev["speech"], 0.42)
        self.assertIsNone(ev["state"])
        self.assertEqual(gs.state_event({"speech": {"p": 3}})["speech"], 1.0)
        with self.assertRaises(ValueError):
            gs.state_event({"speech": {"p": "x"}})

    def test_dismiss_phrases(self):
        self.assertEqual(cards.recipe_utterance("Dismiss."), "dismiss")
        self.assertEqual(cards.recipe_utterance("hey grok, clear that please"), "dismiss")
        import relay
        self.assertEqual(relay.recipe_match("clear that"), "dismiss")
        self.assertEqual(relay.post_recipe_cmd("dismiss", post=lambda c: {"ok": c == "dismiss"}), {"ok": True})
        self.assertEqual(cards.apply_recipe_command(cards.Deck(), "dismiss", 1.0), {"ok": False})

    def test_deck_keeps_recipe_current_and_reports_expiry(self):
        d = cards.Deck(rotate_s=1, ttl_max=4000)
        d.put("jules", "soup", 1, 3600, RECIPE, 0)
        d.put("mira", "game", 99, 60, SPORTS, 1)
        for t in (2, 3, 4, 5):
            self.assertEqual(d.current(t)["id"], "soup")
        self.assertEqual(d.expiry("mira", "game"), 61)
        self.assertIsNone(d.expiry("mira", "nope"))


class WakeSpeechClock(unittest.TestCase):
    def test_progress_and_ticker(self):
        import threading
        sys.path.insert(0, os.path.join(ROOT, "wake"))
        try:
            import grok_wake as gw
        except Exception as e:  # the wake listener needs numpy/onnx; skip where they're missing
            self.skipTest(f"wake import: {type(e).__name__}")
        c = gw.SpeechClock(100.0)
        self.assertEqual(c.progress(101), 0.0)
        c.add(48000 * 4)  # 4 s of 24 kHz PCM16 received
        self.assertEqual(c.progress(101), 0.25)
        self.assertEqual(c.progress(200), 1.0)
        got, stop = [], threading.Event()

        def post(p):
            got.append(p)
            if len(got) >= 3:
                stop.set()

        gw.speech_ticker(c, stop, post=post, hz=200)
        self.assertEqual(len(got), 3)
        self.assertGreaterEqual(gw.SPEECH_HZ, 5)


if __name__ == "__main__":
    unittest.main()
