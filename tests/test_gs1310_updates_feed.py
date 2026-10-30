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


class NixLowFindings(RelayGate):
    """Nix design GO, Low findings L1-L6 and I-1."""
    def _raw(self, doc):
        return tc._seal(self.station, self.signer, json.dumps(doc, separators=(",", ":")))

    def _accept(self, raw, seen=None):
        posts = []
        out = self.relay.accept_show(raw, self.station, self.keys, self.known, botreply.Limiter(50, 60),
                                     lambda s: posts.append(s) or 200, ttl_max=3600, seen=seen)
        return out, posts

    def test_corrupt_or_non_dict_file_is_off(self):
        for blob in (b"{not json", b"[]", b'"on"', b'{"bots": []}', b'{"bots": {"jules": "yes"}}'):
            with open(self.relay.updates_path(), "wb") as f:
                f.write(blob)
            self.assertFalse(self.relay.updates_on("jules"), blob)

    def test_internal_ops_and_a_bot_field_in_the_signed_doc_are_400(self):
        self.relay.write_updates("jules", True)
        for doc in (dict(tc._meta(), v=1, op="updates_off"), dict(upd(), bot="jules"),
                    dict(tc._meta(), v=1, op="updates_only", bots=[]), dict(tc._meta(), v=1, op="updates_pause", on=True)):
            (status, _, _), posts = self._accept(self._raw(doc))
            self.assertEqual(status, 400, doc.get("op"))
            self.assertEqual(posts, [])

    def test_an_op_swap_after_signing_fails(self):
        self.relay.write_updates("jules", True)
        from nacl.public import SealedBox
        msg = json.dumps(upd(), separators=(",", ":")).encode()
        signed = bytes(self.signer.sign(msg))
        swapped = signed[:64] + msg.replace(b'"op":"update"', b'"op":"update_clear"')
        sealed = SealedBox(self.station.public_key).encrypt(swapped)
        raw = json.dumps({"v": 1, "bot": "jules", "sealed": tc._b64(sealed)}).encode()
        (status, _, _), posts = self._accept(raw)
        self.assertIn(status, (400, 401, 403))
        self.assertEqual(posts, [])

    def test_update_nonce_replay_is_caught_and_refusals_spend_the_nonce(self):
        seen = set()
        raw = self._raw(upd())
        (status, body, _), _ = self._accept(raw, seen)  # switch off: 403, nonce spent (L5)
        self.assertEqual((status, body), (403, {"error": "updates_off"}))  # I-1
        self.relay.write_updates("jules", True)
        (status, _, _), posts = self._accept(raw, seen)
        self.assertEqual(status, 409)
        self.assertEqual(posts, [])
        raw = self._raw(upd(line="key sk-" + "A" * 40))
        self.assertEqual(self._accept(raw, seen)[0][0], 422)
        self.assertEqual(self._accept(raw, seen)[0][0], 409)
        raw = self._raw(upd("u2"))
        self.assertEqual(self._accept(raw, seen)[0][0], 200)
        self.assertEqual(self._accept(raw, seen)[0][0], 409)

    def test_raw_line_is_linted_before_clipping_and_over_320_is_400(self):
        self.relay.write_updates("jules", True)
        secret_at_the_cut = ("word " * 15) + "sk-" + "A" * 40  # clip_line would drop the key
        self.assertNotIn("sk-", cards.clip_line(secret_at_the_cut))
        self.assertEqual(self._accept(self._raw(upd(line=secret_at_the_cut)))[0][0], 422)
        self.assertEqual(self._accept(self._raw(upd(line="a" * 321)))[0][0], 400)
        self.assertEqual(self._accept(self._raw(upd(line="a " * 160)))[0][0], 200)

    def test_drop_retries_then_reports_pending_and_startup_sync_keeps_only_on_bots(self):
        sent = []
        real = self.relay._station_env_values
        self.relay._station_env_values = lambda keys: {"GROK_STATION_TOKEN": "t", "GROK_STATION_PORT": "8787"}
        try:
            codes = iter([503, 200])
            self.assertTrue(self.relay.post_station_updates_drop("jules", post=lambda b, t, p: next(codes)))
            self.assertFalse(self.relay.post_station_updates_drop("jules", post=lambda b, t, p: 503))
            self.relay.write_updates("jules", True)
            self.assertTrue(self.relay.post_station_updates_only(post=lambda b, t, p: sent.append(json.loads(b)) or 200))
            self.assertTrue(self.relay.post_station_updates_drop("jules", post=lambda b, t, p: 200))
        finally:
            self.relay._station_env_values = real
        self.assertEqual(sent, [{"v": 1, "op": "updates_only", "bots": ["jules"]}])
        f = cards.UpdateFeed()
        gs.apply_update(dict(upd(), bot="jules"), f)
        gs.apply_update(dict(upd(), bot="otto"), f)
        gs.apply_update({"v": 1, "op": "updates_only", "bots": ["jules"]}, f)
        self.assertEqual({r["bot"] for r in f.items.values()}, {"jules"})

    def test_one_lock_for_switch_and_rule_writes(self):
        import threading
        errs = []

        def flip(i):
            try:
                self.relay.write_updates("jules", i % 2 == 0)
                self.relay.note_feed_rule("jules", "Rule %d" % i)
            except Exception as e:  # pragma: no cover
                errs.append(e)
        ts = [threading.Thread(target=flip, args=(i,)) for i in range(40)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        with open(self.relay.updates_path()) as fh:
            doc = json.load(fh)
        self.assertEqual(set(doc), {"bots", "rules"})
        self.assertTrue(doc["rules"]["jules"].startswith("Rule "))
        self.assertIsInstance(self.relay._UPDATES_LOCK, type(threading.Lock()))

    def test_admin_put_validation_and_old_new_log(self):
        import asyncio
        r = self.relay
        logs = []
        saved = (r.admin_gate, r.admin_json, r._known, r.log, r.post_station_updates_drop)

        async def gate(req):
            return None

        def run(body, slug="jules", gate_fn=gate):
            async def aj(req):
                return body
            r.admin_gate, r.admin_json = gate_fn, aj
            req = type("Q", (), {"match_info": {"slug": slug}})()
            resp = asyncio.run(r.admin_updates_put(req))
            return resp.status, json.loads(resp.body)
        try:
            r._known = lambda s: s == "jules"
            r.log = lambda *a: logs.append(" ".join(map(str, a)))
            r.post_station_updates_drop = lambda slug: False
            for bad in ({"on": "yes"}, {"on": True, "x": 1}, {}):
                self.assertEqual(run(bad)[0], 400, bad)
            self.assertEqual(run({"on": True}, slug="ghost")[0], 400)

            async def no_session(req):
                from aiohttp import web
                return web.json_response({"error": "unauthorized"}, status=401)
            self.assertEqual(run({"on": True}, gate_fn=no_session)[0], 401)
            self.assertEqual(run({"on": True}), (200, {"ok": True, "on": True, "was": False}))
            self.assertEqual(run({"on": False}), (200, {"ok": True, "on": False, "was": True, "station": "pending"}))
            self.assertIn("updates jules off->on", logs)
            self.assertIn("updates jules on->off", logs)
        finally:
            r.admin_gate, r.admin_json, r._known, r.log, r.post_station_updates_drop = saved


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
