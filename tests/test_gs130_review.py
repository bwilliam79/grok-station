"""0.13.0 review fixes.

1. A refused reply's reason code (and the _BLOCKED early end of a handoff) is only for the bot's own open
   request. A schema reject can carry a well-formed request id before ownership is checked, so without this a
   signed bot could end another bot's waiting handoff or learn reason codes for a request it does not own.
2. Helper compatibility: a home bot still on the 0.11.8 helper fails on --interim. The instructions tell it to
   send only the answer, once, without --interim, and the bot_list command stays right after its lead-in line.
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_avatar  # noqa: E402
import botreply  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RID = "vs_" + "A" * 22
FALLBACK = "If a command with --interim fails, do not retry the acknowledgement; send only the answer, once, without --interim."


class ReasonNeedsOwnRequest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_avatar.Avatar.asyncSetUp
    asyncTearDown = test_avatar.Avatar.asyncTearDown

    def _with_pending(self, persona):
        r = self.relay
        old = r._pending_doc
        r._pending_doc = lambda rid: {"request_id": rid, "persona": persona} if persona else None
        self.addCleanup(setattr, r, "_pending_doc", old)

    def test_other_bots_request_gets_no_reason(self):
        self._with_pending("miles")
        self.assertIsNone(self.relay.reply_reason(botreply.Reject(400, "schema", "jules", RID)))

    def test_no_pending_gets_no_reason(self):
        self._with_pending(None)
        self.assertIsNone(self.relay.reply_reason(botreply.Reject(400, "schema", "jules", RID)))

    def test_own_request_gets_the_code(self):
        self._with_pending("jules")
        self.assertEqual(self.relay.reply_reason(botreply.Reject(400, "schema", "jules", RID)), "schema")
        rej = botreply.Reject(400, "lint", "jules", RID)
        rej.reason = "secret"
        self.assertEqual(self.relay.reply_reason(rej), "secret")


class HelperCompat(unittest.TestCase):
    FILES = ("admin/routine-home.txt", "admin/routine-cloud.txt")

    def test_instructions_fall_back_without_interim(self):
        for f in self.FILES:
            with open(os.path.join(ROOT, f)) as fh:
                self.assertIn(FALLBACK, fh.read(), f)

    def test_home_bot_list_command_follows_its_lead_in(self):
        with open(os.path.join(ROOT, "admin/routine-home.txt")) as fh:
            s = fh.read()
        lead = s.index("the same command with --kind bot_list:")
        self.assertLess(s.index("--kind bot_list\n", lead), s.index("Send exactly one reply per request"))

    def test_old_helper_rejects_interim_so_fallback_is_needed(self):
        import subprocess
        old = subprocess.run(["git", "-C", ROOT, "show", "f03f9ca:scripts/gs-seal-reply"], capture_output=True, text=True)
        if old.returncode != 0:
            self.skipTest("old helper not in history")
        self.assertNotIn("--interim", old.stdout)
