#!/usr/bin/env python3
"""0.12.11: a refused voice question says why; admin tests never crowd out voice questions; the Bots page
sends no automatic questions. Unit and source checks only. Names are made up."""
import asyncio, os, re, sys, tempfile, unittest

TMP = tempfile.mkdtemp()
for _k, _v in dict(RELAY_TOKEN="test-token", XAI_API_KEY="test-key", ALERT_DIR=os.path.join(TMP, "none"),
                  PERSONAS_FILE=os.path.join(TMP, "none.json"), HANDOFF_PENDING=os.path.join(TMP, "pending"),
                  HANDOFF_REPLIES=os.path.join(TMP, "replies"), HANDOFF_STATE=os.path.join(TMP, "handoff.json"),
                  REPLY_WAIT="3").items():
    os.environ.setdefault(_k, _v)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import handoff  # noqa: E402
import relay  # noqa: E402

JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def _fn(name):
    i = JS.index("function " + name + "(")
    return JS[i:JS.index("\n}\n", i) + 3]


class _Lim(unittest.TestCase):
    def setUp(self):
        self._saved = relay._limits
        self.path = tempfile.mktemp(dir=TMP, suffix=".json")
        relay._limits = handoff.Limits(self.path, relay.today)

    def tearDown(self):
        relay._limits = self._saved

    def ask(self, bot="jules"):
        return asyncio.run(relay.ask_teammate(None, bot, "is the game on?"))


class SpokenRefusal(_Lim):
    def test_a_outstanding_is_not_the_daily_limit_line(self):
        relay._limits.take("jules", handoff.new_request_id())  # a real voice question is in flight
        text, verdict = self.ask()
        say = relay.handoff_line("Jules", verdict)
        self.assertIsNone(text)
        self.assertNotIn("lot of questions", say)
        self.assertIn("still working on another question", say)

    def test_too_soon_has_its_own_retry_line(self):
        relay._limits.gap_s = 30.0
        rid = handoff.new_request_id()
        relay._limits.take("jules", rid)
        relay._limits.release("jules", rid)
        say = relay.handoff_line("Jules", self.ask()[1])
        self.assertNotIn("lot of questions", say)
        self.assertIn("Try again", say)

    def test_d_real_daily_cap_keeps_the_daily_line(self):
        relay._limits.per_bot_day = 1
        rid = handoff.new_request_id()
        relay._limits.take("jules", rid)
        relay._limits.release("jules", rid)
        verdict = self.ask()[1]
        self.assertEqual(verdict, "limit")
        self.assertIn("lot of questions from the station today", relay.handoff_line("Jules", verdict))


class VoiceBeatsAdminTest(_Lim):
    def test_b_voice_question_takes_slot_from_admin_test(self):
        lim = relay._limits
        t_rid, v_rid = handoff.new_request_id(), handoff.new_request_id()
        self.assertIsNone(lim.take("jules", t_rid, test=True))
        cancelled = []
        self.assertIsNone(lim.take("jules", v_rid, preempt=cancelled.append))
        self.assertEqual(cancelled, ["jules"])
        lim.release("jules", t_rid)  # the cancelled test's cleanup must not free the voice question
        self.assertEqual(lim.block_reason("jules"), "outstanding")

    def test_voice_held_slot_still_refuses(self):
        lim = relay._limits
        lim.take("jules", handoff.new_request_id())
        self.assertEqual(lim.take("jules", handoff.new_request_id(), preempt=lambda b: None), "outstanding")
        self.assertEqual(lim.take("jules", handoff.new_request_id(), test=True), "outstanding")

    def test_preempt_cancels_the_waiting_admin_test(self):
        ev = asyncio.Event()
        relay._runs["t_fake0000"] = {"slug": "jules", "state": "waiting", "cancel": ev}
        try:
            relay.cancel_admin_tests("jules")
            self.assertTrue(ev.is_set())
        finally:
            relay._runs.pop("t_fake0000", None)

    def test_admin_test_has_a_cooldown(self):
        src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
        self.assertIn("_test_last[slug] = t_now", src)
        self.assertIn("TEST_COOLDOWN_S = 15", src)


class NoAutomaticBotQuestions(unittest.TestCase):
    def test_c_bots_page_schedules_no_automatic_test(self):
        page = _fn("pageBots")
        for line in page.splitlines():
            if "every(" in line:
                self.assertNotIn("Check", line)
                self.assertNotIn("/test", line)
        self.assertNotIn("recheckBots", JS)
        self.assertNotIn("checkNewBots", JS)
        self.assertNotIn("startBotCheck", _fn("refreshBots"))
        self.assertNotIn('{state: "checking"}', _fn("paintBots"))

    def test_status_comes_from_server_state(self):
        self.assertIn("serverCheck(b)", _fn("botRing"))
        self.assertIn("last_answered_at", _fn("serverCheck"))
        # the only automatic-looking callers left: Configure's Test and the add-bot flow
        callers = [m.start() for m in re.finditer(r"startBotCheck\(", JS)]
        self.assertEqual(len(callers), 2)  # the definition and the add-bot call


if __name__ == "__main__":
    unittest.main()
