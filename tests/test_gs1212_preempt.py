#!/usr/bin/env python3
"""0.12.12 (Nix L2, L3): a voice question the caps or gap refuse does not cancel a waiting admin Test,
and a real preemption is logged as 'handoff {rid} preempted test'. Names are made up."""
import asyncio, os, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
relay = handoff = None  # imported at test time: test_handoff must be the first to import the relay with its env


def _mods():
    global relay, handoff
    if relay is None:
        os.environ.setdefault("RELAY_TOKEN", "test-token"); os.environ.setdefault("XAI_API_KEY", "test-key")
        import handoff as h, relay as r
        relay, handoff = r, h

TMP = tempfile.mkdtemp()


class _Lim(unittest.TestCase):
    def setUp(self):
        _mods()
        self._saved = relay._limits
        relay._limits = handoff.Limits(os.path.join(tempfile.mkdtemp(dir=TMP), "limits.json"), relay.today)

    def tearDown(self):
        relay._limits = self._saved


class RefusedQuestionKeepsTheTest(_Lim):
    def _test_held(self):
        lim = relay._limits
        t_rid = handoff.new_request_id()
        self.assertIsNone(lim.take("jules", t_rid, test=True))
        return t_rid

    def test_daily_cap_refusal_does_not_preempt(self):
        lim = relay._limits
        lim.per_bot_day = 1
        t_rid = self._test_held()
        cancelled = []
        self.assertEqual(lim.take("jules", handoff.new_request_id(), preempt=cancelled.append), "bot daily cap")
        self.assertEqual(cancelled, [])
        self.assertEqual(lim._load()["out"].get("jules"), t_rid)  # the Test still holds its slot

    def test_global_cap_refusal_does_not_preempt(self):
        lim = relay._limits
        lim.global_day = 1
        t_rid = self._test_held()
        cancelled = []
        self.assertEqual(lim.take("jules", handoff.new_request_id(), preempt=cancelled.append), "global daily cap")
        self.assertEqual(cancelled, [])
        self.assertEqual(lim._load()["out"].get("jules"), t_rid)

    def test_min_gap_refusal_does_not_preempt(self):
        lim = relay._limits
        lim.gap_s = 60.0
        t_rid = self._test_held()
        cancelled = []
        self.assertEqual(lim.take("jules", handoff.new_request_id(), preempt=cancelled.append), "too soon")
        self.assertEqual(cancelled, [])
        self.assertEqual(lim._load()["out"].get("jules"), t_rid)

    def test_allowed_question_still_preempts(self):
        lim = relay._limits
        self._test_held()
        cancelled = []
        self.assertIsNone(lim.take("jules", handoff.new_request_id(), preempt=cancelled.append))
        self.assertEqual(cancelled, ["jules"])


class PreemptionIsLogged(_Lim):
    def test_voice_question_logs_the_preemption(self):
        lines, saved, saved_wait = [], relay.log, relay.REPLY_WAIT
        relay.log = lambda *a: lines.append(" ".join(map(str, a)))
        relay.REPLY_WAIT = 0.1
        saved_dirs = (relay.PENDING_DIR, relay.REPLIES_DIR, relay.WEBHOOKS_FILE)
        d = tempfile.mkdtemp(dir=TMP)
        relay.PENDING_DIR, relay.REPLIES_DIR = os.path.join(d, "pending"), os.path.join(d, "replies")
        relay.WEBHOOKS_FILE = os.path.join(d, "bots", "webhooks.json")
        ev = asyncio.Event()
        relay._runs["t_fake1212"] = {"slug": "jules", "state": "waiting", "cancel": ev}
        try:
            self.assertIsNone(relay._limits.take("jules", handoff.new_request_id(), test=True))
            detail = {}
            asyncio.run(relay.ask_teammate(None, "jules", "is the game on?", detail=detail))
        finally:
            relay.log, relay.REPLY_WAIT = saved, saved_wait
            relay.PENDING_DIR, relay.REPLIES_DIR, relay.WEBHOOKS_FILE = saved_dirs
            relay._runs.pop("t_fake1212", None)
        self.assertTrue(ev.is_set())
        self.assertIn("handoff %s preempted test" % detail["rid"], lines)


if __name__ == "__main__":
    unittest.main()
