#!/usr/bin/env python3
"""0.13.25: a failed Confirm and send / Try again stays visible, and is not a station outage.

setup_send used to answer HTTP 502 {"error": "Couldn't reach {main}", "setup": ...} for every
relay outcome that was not 200 or 202. The page treats 502 as the station being offline, and
setupSendResult dropped the toast when that error started with "Couldn't reach " and setup was
present. Try again then repeated the same silent 502.
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402  sets the admin env, then imports grok_admin
import grok_admin as ga  # noqa: E402

NET_DOWN = {0, 502, 503, 504}
KNOWN = {"otto", "juniper"}


def _fn(name):
    start = JS.index("\nfunction " + name + "(")
    nxt = JS.find("\nfunction ", start + 1)
    return JS[start:nxt]


def _node(expr):
    prog = "const esc = s => String(s == null ? '' : s);\n" + _fn("setupSendResult") + \
        "\nprocess.stdout.write(JSON.stringify(" + expr + "));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _panel(row, opts):
    from test_gs1311_bots_merge import SETUP, _run
    return _run(SETUP, "setupRowPanelHTML(%s, %s)" % (json.dumps(row), json.dumps(opts)))


class SetupSendFailure(unittest.TestCase):
    def setUp(self):
        ga._SETUP_INFLIGHT.clear()

    def tearDown(self):
        ga._SETUP_INFLIGHT.clear()

    def _send(self, doc, poster, now=5000):
        return ga.setup_send(
            doc, "otto", known=KNOWN, main_slug="juniper", main_name="Juniper", main_outdated=False,
            now=now, poster=poster, name="Otto", wake="Hey Otto", computer="Office computer")

    def test_relay_reason_is_shown_and_is_not_a_station_outage(self):
        doc = {"rows": {}}
        cases = (
            ((404, {"error": "not found"}), 404, "not found"),
            ((400, {"error": "not configured"}), 400, "not configured"),
            ((400, {"error": "bad request"}), 400, "bad request"),
            ((502, {"error": "webhook rejected"}), 422, "webhook rejected"),
            ((502, {"error": "timeout"}), 422, "timeout"),
            ((502, {"error": "Couldn't reach the bot."}), 422, "Couldn't reach the bot."),
            ((500, {"error": "unavailable"}), 422, "unavailable"),
            (False, 422, "Couldn't reach Juniper"),
        )
        for i, (posted, want_code, want_err) in enumerate(cases):
            code, body = self._send(doc, lambda target, posted=posted: posted, now=5000 + i)
            self.assertNotIn(code, NET_DOWN, posted)
            self.assertEqual(code, want_code, posted)
            self.assertEqual(body["error"], want_err, posted)
            self.assertIn("setup", body)
            self.assertEqual(body["setup"]["rows"]["otto"]["status"], "failed")
            self.assertNotIn("https://", body["error"])
        # The same failure again still returns the reason. It does not collapse or go silent.
        code, body = self._send(doc, lambda target: (502, {"error": "timeout"}), now=5100)
        self.assertEqual((code, body["error"]), (422, "timeout"))
        self.assertNotIn(code, NET_DOWN)
        self.assertEqual(body["setup"]["rows"]["otto"]["status"], "failed")
        code, body = self._send(doc, lambda target: (400, {"error": "see https://evil.example/hook"}), now=5101)
        self.assertNotIn(code, NET_DOWN)
        self.assertNotIn("http", body["error"].lower())
        self.assertNotIn("evil.example", body["error"])

    def test_page_tells_the_reason_including_a_repeated_try_again(self):
        payload = {"ok": False, "status": 422, "data": {"error": "Couldn't reach Rhea", "setup": {"rows": {}}}}
        first = _node("setupSendResult(%s, 'Nix')" % json.dumps(payload))
        second = _node("setupSendResult(%s, 'Nix')" % json.dumps(payload))
        self.assertEqual(first["toast"], "Couldn't reach Rhea")
        self.assertEqual(second["toast"], "Couldn't reach Rhea")
        specific = _node("setupSendResult(%s, 'Noor')" % json.dumps(
            {"ok": False, "status": 404, "data": {"error": "not found", "setup": {"rows": {}}}}))
        self.assertEqual(specific["toast"], "not found")
        send = JS.split("async function setupSend(", 1)[1].split("\nfunction setupEdit(", 1)[0]
        self.assertIn('busyBtn(btn, true, "Sending\\u2026")', send)
        self.assertLess(send.index("busyBtn"), send.index('"/api/bots/setup-send"'))
        self.assertNotIn("Retrying", send)
        result = _fn("setupSendResult")
        self.assertNotIn('data.setup ? ""', result)
        row = {"slug": "nix", "name": "Nix", "wake": "Hey Nix", "computer": "Office computer",
               "status": "failed", "sent_at": 0, "fails": 2, "owner": "Sam"}
        html = _panel(row, {"mainName": "Rhea", "now": 1000, "failText": "timeout"})
        self.assertIn('role="alert"', html)
        self.assertIn("timeout", html)
        self.assertIn(">Try again<", html)
        self.assertNotIn("didn't answer", html)


class SetupRelayWebhookReason(unittest.TestCase):
    """The relay names a webhook timeout or a non-2xx separately from a connection failure.

    The admin call still sees HTTP 502 from the relay. setup_send turns that into a browser status
    outside the station-offline set and keeps this error text.
    """

    def test_webhook_post_names_timeout_rejection_or_unreachable(self):
        with open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8") as fh:
            src = fh.read()
        fn = src.split("async def admin_setup_relay(", 1)[1].split("async def admin_cards_get(", 1)[0]
        post = fn.split("posted = frozen", 1)[1]
        self.assertIn("except asyncio.TimeoutError:", post)
        self.assertLess(post.index("except asyncio.TimeoutError:"), post.index("except Exception:"))
        self.assertIn('"timeout"', post)
        self.assertIn('"webhook rejected"', post)
        self.assertIn('"Couldn\'t reach the bot."', post)


if __name__ == "__main__":
    unittest.main()
