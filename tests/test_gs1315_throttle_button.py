"""0.13.15 Noor QA 3: after a 409 the resend button and the 409 line agree (same wait), and both clear when it ends."""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gs1311_bots_merge import SETUP, _run, _row  # noqa: E402

NAMES = SETUP + ("setupThrottleLine", "setupRowStatusHTML")
ROW = _row(status="sent", sent_at=600)  # past the normal wait: enabled unless a 409 says otherwise


def go(fn, now, throttle):
    opts = {"mainName": "Juniper", "now": now, "throttle": throttle}
    return _run(NAMES, "%s(%s, %s)" % (fn, json.dumps(ROW), json.dumps(opts)))


class ThrottleButton(unittest.TestCase):
    def test_409_disables_button_with_matching_wait(self):
        a = go("setupRowActionHTML", 1000, {"otto": 1240})
        self.assertIn(">Send again in 4 min<", a)
        self.assertIn('aria-label="Send the setup request to Juniper again, available in 4 minutes"', a)  # 0.13.16 design §6: request goes to main
        self.assertIn('aria-disabled="true"', a)
        s = go("setupRowStatusHTML", 1000, {"otto": 1240})
        self.assertIn("Still waiting for Otto. Try again in 4 min.", s)
        self.assertEqual(s.count('class="shelp'), 1)

    def test_reenables_when_wait_ends(self):
        a = go("setupRowActionHTML", 1300, {"otto": 1240})
        self.assertIn(">Send request again<", a)
        self.assertNotIn("aria-disabled", a)
        s = go("setupRowStatusHTML", 1300, {"otto": 1240})
        self.assertNotIn("Still waiting", s)
        self.assertIn("checks this station\u2019s details and passes them to Otto.", s)  # sent helper (not yet relayed)


if __name__ == "__main__":
    unittest.main()
