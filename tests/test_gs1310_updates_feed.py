"""0.13.10 bot updates feed (Noor SPEC rev4): schema, ordering, rotation, night, pause, gating."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_cards as tc  # noqa: E402  (sealing helpers, same keys and env)
from test_cards import cards, botreply, gs  # noqa: E402

T0 = 1_800_000_000.0


def iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()


def upd(uid="u1", type_="done", line="Booked the table", t=T0, **extra):
    body = {"type": type_, "line": line, "time": iso(t)}
    body.update(extra)
    doc = {"v": 1, "op": "update", "id": uid, "update": body}
    doc.update(tc._meta())
    return doc


class Schema(unittest.TestCase):
    def test_four_types_and_unknown_fields(self):
        for t in cards.UPDATE_TYPES:
            self.assertEqual(cards.validate_update(upd(type_=t))["update"]["type"], t)
        for bad in (upd(type_="urgent"), dict(upd(), extra=1), upd(line="   ")):
            with self.assertRaises(cards.CardError):
                cards.validate_update(bad)
        doc = upd()
        doc["update"]["speak"] = True  # no audio field exists
        with self.assertRaises(cards.CardError):
            cards.validate_update(doc)

    def test_long_line_is_cut_at_a_word_with_an_ellipsis(self):
        line = cards.validate_update(upd(line="word " * 30))["update"]["line"]
        self.assertLessEqual(len(line), 80)
        self.assertTrue(line.endswith("\u2026"))

    def test_needs_you_ignores_expires_at(self):
        out = cards.validate_update(upd(type_="needs_you", expires_at=iso(T0 + 60)))
        self.assertNotIn("expires_at", out["update"])


class FeedRules(unittest.TestCase):
    def setUp(self):
        self.f = cards.UpdateFeed()

    def put(self, bot, uid, type_, t, **kw):
        self.f.put(bot, uid, cards.validate_update(upd(uid, type_, "x", t, **kw))["update"])

    def test_order_ageing_and_one_working_on_per_bot(self):
        self.put("otto", "a", "working_on", T0)
        self.put("otto", "b", "working_on", T0 + 1)
        self.put("juniper", "c", "done", T0 + 2)
        self.put("juniper", "d", "needs_you", T0 + 3)
        needs, rest = self.f.ordered(T0 + 10)
        self.assertEqual([r["id"] for r in needs], ["d"])
        self.assertEqual([r["id"] for r in rest], ["c", "b"])
        _, rest = self.f.ordered(T0 + 2 + 30 * 60)
        self.assertTrue(next(r for r in rest if r["id"] == "c")["aged"])
        needs, rest = self.f.ordered(T0 + 3 * 3600)
        self.assertEqual(rest, [])
        self.assertEqual([r["id"] for r in needs], ["d"])  # Needs you stays until handled

    def test_two_needs_you_take_slots_1_and_2_and_slot_3_rotates(self):
        needs = [{"id": "n2"}, {"id": "n1"}]
        others = [{"id": "o%d" % i} for i in range(4)]
        for step in range(4):
            tiles = cards.update_slots(needs, others, step)
            self.assertEqual([t["id"] for t in tiles[:2]], ["n2", "n1"])
            self.assertEqual(tiles[2]["id"], "o%d" % step)

    def test_one_needs_you_pins_slot_1_and_none_rotates_all(self):
        others = [{"id": "o%d" % i} for i in range(6)]
        self.assertEqual([t["id"] for t in cards.update_slots([{"id": "n"}], others, 1)], ["n", "o2", "o3"])
        self.assertEqual([t["id"] for t in cards.update_slots([], others, 1)], ["o3", "o4", "o5"])

    def test_night_hides_everything_including_needs_you(self):
        self.put("otto", "n", "needs_you", T0)
        state = cards.update_band_state(self.f, T0 + 5, 0, night=True)
        self.assertFalse(state["visible"])
        self.assertEqual(state["tiles"], [])

    def test_bot_talking_keeps_the_row_visible_but_paused(self):
        self.put("otto", "n", "done", T0)
        state = cards.update_band_state(self.f, T0 + 5, 0, bot_talking=True)
        self.assertTrue(state["visible"])
        self.assertTrue(state["paused"])
        self.assertEqual(cards.update_next_step(3, 25, paused=True), 3)
        self.assertEqual(cards.update_next_step(3, 20, paused=False), 4)

    def test_switch_off_drops_a_bots_rows(self):
        self.put("otto", "a", "done", T0)
        self.f.drop_bot("otto")
        self.assertEqual(self.f.ordered(T0), ([], []))

    def test_rule_line_under_the_switch(self):
        self.assertEqual(cards.update_rule_line(False, "x"), "Off: nothing shows on the station")
        self.assertEqual(cards.update_rule_line(True, ""), "All updates")
        self.assertEqual(cards.update_rule_line(True, "Only Heads-up"), "Only Heads-up")


class RelayGate(unittest.TestCase):
    def setUp(self):
        tc.SignedShow.setUp(self)  # same station key, signer and known bot
        import relay
        self.relay = relay
        self.tmp = tempfile.mkdtemp()
        self.real_path = relay.updates_path
        relay.updates_path = lambda: os.path.join(self.tmp, "updates.json")

    def tearDown(self):
        self.relay.updates_path = self.real_path

    def _send(self, doc):
        posts = []
        raw = tc._seal(self.station, self.signer, json.dumps(doc, separators=(",", ":")))
        status, _, _ = self.relay.accept_show(raw, self.station, self.keys, self.known, botreply.Limiter(12, 60),
                                              lambda s: posts.append(s) or 200, ttl_max=3600)
        return status, posts

    def test_new_bots_are_off_by_default(self):
        status, posts = self._send(upd())
        self.assertEqual(status, 403)
        self.assertEqual(posts, [])

    def test_on_forwards_and_remembers_the_rule(self):
        self.relay.write_updates("jules", True)
        status, posts = self._send(upd(feed_rule="Only Done and Needs you"))
        self.assertEqual(status, 200)
        self.assertEqual(posts[0]["op"], "update")
        self.assertEqual(posts[0]["bot"], "jules")
        self.assertEqual(self.relay.read_feed_rules()["jules"], "Only Done and Needs you")

    def test_a_secret_in_the_line_is_refused(self):
        self.relay.write_updates("jules", True)
        status, posts = self._send(upd(line="key sk-" + "A" * 40))
        self.assertEqual(status, 422)
        self.assertEqual(posts, [])


class StationApply(unittest.TestCase):
    def test_apply_put_clear_and_off(self):
        f = cards.UpdateFeed()
        doc = dict(upd(), bot="otto")
        self.assertEqual(gs.apply_update(doc, f)[0], "put")
        self.assertEqual(len(f.items), 1)
        clr = {"v": 1, "op": "update_clear", "id": "u1", "bot": "otto"}
        clr.update(tc._meta())
        self.assertEqual(gs.apply_update(clr, f)[0], "clear")
        gs.apply_update(dict(upd(), bot="otto"), f)
        gs.apply_update({"v": 1, "op": "updates_off", "bot": "otto"}, f)
        self.assertEqual(f.items, {})
        with self.assertRaises(cards.CardError):
            gs.apply_update({"v": 1, "op": "updates_off", "bot": "otto", "x": 1}, f)


class StationBand(unittest.TestCase):
    def setUp(self):
        import pygame
        pygame.display.init()
        pygame.font.init()
        self.old = gs.UPDATES
        gs.UPDATES = cards.UpdateFeed()
        gs.UPD_ROT.update(step=0, at=None)

    def tearDown(self):
        gs.UPDATES = self.old

    def test_idle_band_paints_updates_and_rotates_every_20s(self):
        import pygame, time
        now = time.time()
        for i in range(5):
            gs.apply_update(dict(upd("u%d" % i, "done", "Line %d" % i, now - i), bot="otto"), gs.UPDATES)
        gs.apply_update(dict(upd("n", "needs_you", "Approve the refund?", now), bot="juniper"), gs.UPDATES)
        rows = gs.idle_band_rows(now)
        self.assertEqual(rows[0]["id"], "n")
        ident = gs.feed_identity(now)
        surf = pygame.Surface((2560, 1706))
        gs.draw_next_up(surf, gs.Layout(2560, 1706), None)
        self.assertTrue(gs.LAST_UPDATE_BAND["visible"])
        first = [r["id"] for r in gs.LAST_UPDATE_BAND["tiles"]]
        self.assertNotEqual(gs.feed_identity(now + 21), ident)  # a repaint is due at 20 s
        rows = gs.idle_band_rows(now + 21)
        self.assertEqual(rows[0]["id"], "n")
        self.assertNotEqual([r["id"] for r in rows], first)

    def test_night_and_pause(self):
        import time
        now = time.time()
        gs.apply_update(dict(upd("n", "needs_you", "x", now), bot="otto"), gs.UPDATES)
        self.assertEqual(gs.idle_band_rows(now, night=True), [])
        gs.idle_band_rows(now, paused=True)
        self.assertEqual(gs.update_rotation_step(now + 100, paused=True), 0)


if __name__ == "__main__":
    unittest.main()
