"""0.13.41: a tool fetch belongs to one turn of one session.

A later turn, and a different session, with the same arguments fetch again and
get a card. Two sessions do not share or replace each other's flight.
"""
import asyncio
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import relay


class Pi:
    def __init__(self):
        self.msgs = []

    async def send_json(self, msg):
        self.msgs.append(msg)


class FreshFlight(unittest.TestCase):
    def setUp(self):
        self.real_run = relay.station_tools.run
        self.real_log = relay.log
        self.calls = []

        async def fake(name, args, http, timers, now):
            self.calls.append((name, dict(args or {})))
            n = len(self.calls)
            return {"ok": True, "n": n}, {"type": "card", "kind": "time", "n": n}

        relay.station_tools.run = fake

    def tearDown(self):
        relay.station_tools.run = self.real_run
        relay.log = self.real_log

    def test_later_turn_and_other_session_get_a_fresh_card(self):
        asyncio.run(self._later())

    async def _later(self):
        st = {"turn": 1}
        pi = Pi()
        await relay.dispatch_content_tile("what time is it", pi, None, {}, time.time(), st)
        for _ in range(8):
            await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(pi.msgs[-1]["type"], "card")
        self.assertEqual(pi.msgs[-1]["n"], 1)
        out, card = await relay.take_tool_card("get_time", {}, None, {}, time.time(), st)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(card)
        self.assertEqual(out["n"], 1)
        relay.clear_tool_flight(st)
        st["turn"] = 2
        out2, card2 = await relay.take_tool_card("get_time", {}, None, {}, time.time(), st)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(out2["n"], 2)
        self.assertEqual(card2["type"], "card")
        self.assertEqual(card2["n"], 2)
        other = {"turn": 1}
        out3, card3 = await relay.take_tool_card("get_time", {}, None, {}, time.time(), other)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(out3["n"], 3)
        self.assertEqual(card3["n"], 3)
        self.assertNotEqual(out3["n"], out["n"])

    def test_two_sessions_do_not_replace_each_others_flight(self):
        asyncio.run(self._two())

    async def _two(self):
        gate = asyncio.Event()

        async def slow(name, args, http, timers, now):
            n = len(self.calls) + 1
            self.calls.append(n)
            await gate.wait()
            return {"ok": True, "n": n}, {"type": "card", "kind": "time", "n": n}

        relay.station_tools.run = slow
        a, b = {"turn": 1}, {"turn": 1}
        pa, pb = Pi(), Pi()
        await relay.dispatch_content_tile("what time is it", pa, None, {}, time.time(), a)
        await relay.dispatch_content_tile("what time is it", pb, None, {}, time.time(), b)
        for _ in range(8):
            if len(self.calls) >= 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.calls, [1, 2])
        self.assertIsNot(a.get("tool_flight"), b.get("tool_flight"))
        gate.set()
        for _ in range(8):
            if pa.msgs and pb.msgs:
                break
            await asyncio.sleep(0)
        self.assertEqual([m["n"] for m in pa.msgs], [1])
        self.assertEqual([m["n"] for m in pb.msgs], [2])
        out_a, card_a = await relay.take_tool_card("get_time", {}, None, {}, time.time(), a)
        out_b, card_b = await relay.take_tool_card("get_time", {}, None, {}, time.time(), b)
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(card_a)
        self.assertIsNone(card_b)
        self.assertEqual(out_a["n"], 1)
        self.assertEqual(out_b["n"], 2)

    def test_a_failed_send_is_kept_and_logged(self):
        asyncio.run(self._fail())

    async def _fail(self):
        logged = []

        async def boom(name, args, http, timers, now):
            raise RuntimeError("fetch broke")

        relay.station_tools.run = boom
        relay.log = lambda *parts: logged.append(" ".join(str(p) for p in parts))
        st = {"turn": 1}
        await relay.dispatch_content_tile("what time is it", Pi(), None, {}, time.time(), st)
        task = st["tool_flight"]["send"]
        self.assertIs(task, st["tool_flight"]["send"])
        self.assertIn(task, relay._TOOL_TASKS)
        for _ in range(8):
            if task.done() and task not in relay._TOOL_TASKS:
                break
            await asyncio.sleep(0)
        self.assertIsNotNone(task.exception())
        self.assertIsInstance(task.exception(), RuntimeError)
        self.assertTrue(any("tool card send failed" in line and "RuntimeError" in line for line in logged))
        self.assertNotIn(task, relay._TOOL_TASKS)


if __name__ == "__main__":
    unittest.main()
