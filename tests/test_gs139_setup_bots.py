#!/usr/bin/env python3
"""Set up bots (gs139). Copy, status model, disclosure, callout priority, and idempotent send.

No browser. The page functions are taken from app.js and run under node. The send path is
grok_admin.setup_send: on the base revision that name does not exist (AttributeError).
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
ADMIN = open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read()
RELAY = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402  sets the admin env, then imports grok_admin
import grok_admin as ga  # noqa: E402
import handoff  # noqa: E402


def _fn(name):
    start = JS.index("\nfunction " + name + "(")
    nxt = JS.find("\nfunction ", start + 1)
    return JS[start:nxt]


def _icons():
    lines = []
    for name in ("ICO_OK", "ICO_ERR", "ICO_KEY", "ICO_DOTS", "ICO_DASH", "ICO_CLOCK", "ICO_PULSE", "ICO_SLASH"):
        i = JS.index("const " + name + " = ")
        lines.append(JS[i:JS.index(";\n", i) + 1])
    return "\n".join(lines)


def _run(expr):
    names = ("clockTime", "joinBotNames", "setupCopy", "setupStuck", "setupDomId", "setupMark",
             "setupOwnerLabel", "setupSteps",
             "setupIcon", "setupRowModel",
             "setupComputerSuggestions", "setupComputerFieldHTML",
             "setupSendResult",
             "botsCalloutKind", "botsCalloutHTML")
    src = "\n".join(_fn(n) for n in names)
    prog = "const esc = s => String(s == null ? '' : s);\n" + _icons() + "\n" + src + \
        "\nprocess.stdout.write(JSON.stringify(" + expr + "));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


OTTO = {"slug": "otto", "name": "Otto", "wake": "Hey Otto", "computer": "Office computer",
        "status": "not_set_up", "sent_at": 0, "fails": 0}
KNOWN = {"otto", "juniper"}


def _clock(ts):
    """Same local h:mm AM/PM as clockTime in app.js."""
    d = datetime.datetime.fromtimestamp(ts)
    h = d.hour
    ap = "PM" if h >= 12 else "AM"
    h = h % 12 or 12
    return "%d:%02d %s" % (h, d.minute, ap)


def _row(status, **extra):
    row = dict(OTTO)
    row["status"] = status
    row.update(extra)
    return row


def _html(row, **opts):
    base = {"mainName": "Juniper", "expanded": False, "sendBlocked": False, "now": 1000}
    base.update(opts)
    return _run("setupRowHTML(%s, %s)" % (json.dumps(row), json.dumps(base)))


def _visible(html):
    """Tags removed. A highlighted word and its Edit button stay two words."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


# R4, filled in: owner Sam or "the station owner", bot Otto, computer Office computer, wake Hey Otto.
SAM_WEBHOOK = (
    'Step 1: Show Sam a confirm card in this chat, and only go ahead if Sam says yes. '
    'Step 2: If you already have a routine whose instructions start with "Grok Station instructions", '
    "update that one and don't create a second; otherwise create a webhook-triggered routine called "
    '"Grok Station" on Sam\'s home computer (Office computer). '
    "Step 3: Reply on the station as Otto; your wake word is Hey Otto. "
    "Step 4: Keep the webhook URL and key in your routine panel, don't send them in any chat, and "
    "Sam copies them into the station."
)
FALLBACK_WEBHOOK = (
    "Step 1: Show the station owner a confirm card in this chat, and only go ahead if the station owner says yes. "
    'Step 2: If you already have a routine whose instructions start with "Grok Station instructions", '
    "update that one and don't create a second; otherwise create a webhook-triggered routine called "
    '"Grok Station" on the station owner\'s home computer (Office computer). '
    "Step 3: Reply on the station as Otto; your wake word is Hey Otto. "
    "Step 4: Keep the webhook URL and key in your routine panel, don't send them in any chat, and "
    "the station owner copies them into the station."
)
SAM_STEPS = [
    "Show Sam a confirm card in this chat. Only go ahead if Sam says yes.",
    'If you already have a routine whose instructions start with "Grok Station instructions", update that one. '
    "Don't create a second. Otherwise create a webhook-triggered routine called \"Grok Station\" on Sam's home "
    "computer (Office computer Edit).",
    "Reply on the station as Otto Edit. Your wake word is Hey Otto Edit.",
    "Keep the webhook URL and key in your routine panel. Don't send them in any chat. "
    "Sam copies them into the station.",
]
FALLBACK_STEPS = [
    "Show the station owner a confirm card in this chat. Only go ahead if the station owner says yes.",
    'If you already have a routine whose instructions start with "Grok Station instructions", update that one. '
    "Don't create a second. Otherwise create a webhook-triggered routine called \"Grok Station\" on the station "
    "owner's home computer (Office computer Edit).",
    "Reply on the station as Otto Edit. Your wake word is Hey Otto Edit.",
    "Keep the webhook URL and key in your routine panel. Don't send them in any chat. "
    "the station owner copies them into the station.",
]




class SetupSend(unittest.TestCase):
    def setUp(self):
        ga._SETUP_INFLIGHT.clear()
        self.records = [{"slug": "otto",
                         "webhook": {"url": "https://example.test/hook/otto", "key": "secretkey1"},
                         "public_key": "abc"}]
        self.added = []
        self.messages = []

    def tearDown(self):
        ga._SETUP_INFLIGHT.clear()

    def _add(self, *args, **kwargs):
        self.added.append((args, kwargs))
        self.records.append({"slug": "otto-copy"})

    def _send(self, doc, poster, **kw):
        opt = dict(known=KNOWN, main_slug="juniper", main_name="Juniper", main_outdated=False,
                   now=1000, name="Otto", wake="Hey Otto", computer="Office computer",
                   add_bot=self._add, save=None)
        opt.update(kw)
        slug = opt.pop("slug", "otto")
        return ga.setup_send(doc, slug, poster=poster, **opt)

    def test_idempotent_resend_keeps_one_record_and_the_same_message(self):
        # Base code has no setup_send (AttributeError). A send that called add_bot, rebuilt the
        # text, or posted a double-click twice would fail the assertions below.
        doc = {"rows": {}}
        code, body = self._send(doc, lambda msg: self.messages.append(msg) or True)
        self.assertEqual(code, 200)
        code, body = self._send(doc, lambda msg: self.messages.append(msg) or True, now=1005)
        self.assertEqual(code, 200)
        self.assertEqual(self.messages[0], self.messages[1])
        self.assertGreater(len(self.messages[0]), 500)
        self.assertIn('start with "Grok Station instructions"', self.messages[0])
        self.assertIn("don't create a second", self.messages[0])
        self.assertIn("the station owner copies them into the station.", self.messages[0])
        self.assertNotIn("\n", self.messages[0])
        self.assertIn("yes. Step 2: If you already", self.messages[0])
        # The webhook cleaner drops newline controls. The relay still sends spaces before the payload.
        wire = handoff.payload("ab" * 8, "juniper", self.messages[0].replace("\n", " "),
                               "2026-10-29T00:00:00Z", cap=4000)["question"]
        bare = handoff.payload("ab" * 8, "juniper", self.messages[0],
                               "2026-10-29T00:00:00Z", cap=4000)["question"]
        self.assertIn("yes. Step 2: If you already", wire)
        self.assertIn("the station owner copies them into the station.", wire)
        self.assertGreater(len(wire), 500)
        self.assertEqual(bare, wire)
        relay_fn = RELAY[RELAY.index("async def admin_setup_relay"):RELAY.index("async def admin_cards_get")]
        self.assertLess(relay_fn.index('replace("\\n", " ")'), relay_fn.index("handoff.payload("))
        self.assertEqual(self.added, [])
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0]["webhook"]["key"], "secretkey1")
        self.assertEqual(self.records[0]["public_key"], "abc")
        self.assertEqual(len(doc["rows"]), 1)
        self.assertNotIn("message", body["setup"]["rows"]["otto"])
        self.assertNotIn("secretkey1", json.dumps(body))
        # A later edit does not rewrite the frozen bytes. Retry sends the original message.
        self.assertEqual(ga.setup_draft(doc, "otto", known=KNOWN, now=1006, computer="Shed computer")[0], 409)
        doc["rows"]["otto"]["status"] = "failed"  # Edit is allowed again; the frozen message stays
        code, _ = ga.setup_draft(doc, "otto", known=KNOWN, now=1006, computer="Shed computer")
        self.assertEqual(code, 200)
        self.assertEqual(doc["rows"]["otto"]["computer"], "Shed computer")
        self.assertIn("Office computer", doc["rows"]["otto"]["message"])
        self.assertNotIn("Shed", doc["rows"]["otto"]["message"])
        self._send(doc, lambda msg: self.messages.append(msg) or True, now=1010)
        self.assertEqual(self.messages[-1], self.messages[0])
        self.assertNotIn("Shed", self.messages[-1])

    def test_concurrent_sends_post_once(self):
        doc = {"rows": {}}
        messages = []
        started = threading.Event()
        release = threading.Event()

        def poster(msg):
            messages.append(msg)
            started.set()
            self.assertTrue(release.wait(2))
            return True

        errors = []

        def call():
            try:
                self._send(doc, poster, now=3000)
            except Exception as exc:  # pragma: no cover - the assertion below reports it
                errors.append(exc)

        first = threading.Thread(target=call)
        second = threading.Thread(target=call)
        first.start()
        self.assertTrue(started.wait(2))
        second.start()
        second.join(2)
        self.assertFalse(second.is_alive())
        release.set()
        first.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(doc["rows"]), 1)
        self.assertEqual(self.added, [])
        self.assertEqual(len(self.records), 1)
        self.assertEqual(doc["rows"]["otto"]["status"], "sent")

    def test_fail_then_fail_again_then_success_resets_the_timer(self):
        doc = {"rows": {}}
        n = {"i": 0}

        def poster(msg):
            self.messages.append(msg)
            n["i"] += 1
            return n["i"] >= 3

        code, body = self._send(doc, poster, now=5000)
        self.assertEqual((code, doc["rows"]["otto"]["status"], doc["rows"]["otto"]["fails"]), (502, "failed", 1))
        self.assertIn("Couldn't reach Juniper", body["error"])
        code, _ = self._send(doc, poster, now=5001)
        self.assertEqual((code, doc["rows"]["otto"]["fails"]), (502, 2))
        code, _ = self._send(doc, poster, now=5002)
        self.assertEqual(code, 200)
        self.assertEqual(doc["rows"]["otto"]["status"], "sent")
        self.assertEqual(doc["rows"]["otto"]["fails"], 0)
        self.assertEqual(doc["rows"]["otto"]["sent_at"], 5002)
        self.assertEqual(len(set(self.messages)), 1)
        self.assertFalse(ga.setup_public(doc, 5002 + 299)["rows"]["otto"]["stuck"])
        self.assertTrue(ga.setup_public(doc, 5002 + 300)["rows"]["otto"]["stuck"])
        self._send(doc, lambda msg: self.messages.append(msg) or True, now=8000)
        self.assertEqual(doc["rows"]["otto"]["sent_at"], 8000)
        self.assertEqual(doc["rows"]["otto"]["fails"], 0)
        self.assertFalse(ga.setup_public(doc, 8000)["rows"]["otto"]["stuck"])
        self.assertTrue(ga.setup_public(doc, 8000 + 300)["rows"]["otto"]["stuck"])
        self.assertEqual(self.messages[-1], self.messages[0])

    def test_outdated_main_unknown_slug_and_unknown_fields(self):
        doc = {"rows": {}}
        called = []
        code, body = self._send(doc, lambda msg: called.append(msg) or True, main_outdated=True)
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "Update the main bot first.")
        self.assertEqual(called, [])
        self.assertEqual(doc.get("rows") or {}, {})
        self.assertEqual(self.added, [])
        code, body = self._send(doc, lambda msg: called.append(msg) or True, slug="maple")
        self.assertEqual(code, 404)
        self.assertEqual(called, [])
        self.assertEqual(ga.setup_check_body({"slug": "otto", "message": "x"}, ("slug",)), "bad request")
        self.assertIsNone(ga.setup_check_body({"slug": "otto"}, ("slug",)))
        self.assertNotIn("/api/bots/setup-transition", ADMIN)
        self.assertNotIn("def api_post_setup_status", ADMIN)

    def test_waiting_and_cancelled_are_not_stored(self):
        self.assertFalse(hasattr(ga, "setup_transition"))
        self.assertNotIn("/api/bots/setup-transition", ADMIN)
        doc = {"rows": {"otto": {"name": "Otto", "wake": "Hey Otto", "computer": "Office computer",
                                 "status": "waiting", "sent_at": 1, "fails": 0, "message": "frozen"}}}
        view = ga.setup_public(doc, 1000)
        self.assertEqual(view["rows"]["otto"]["status"], "not_set_up")
        self.assertNotIn("message", view["rows"]["otto"])
        doc["rows"]["otto"]["status"] = "cancelled"
        self.assertEqual(ga.setup_public(doc, 1000)["rows"]["otto"]["status"], "not_set_up")

    def test_connected_does_not_create_a_second_record_or_touch_pasted_values(self):
        doc = {"rows": {"otto": {"name": "Otto", "wake": "Hey Otto", "computer": "Office computer",
                                 "status": "sent", "sent_at": 1, "fails": 0, "message": "frozen"}}}
        code, _ = ga.setup_connected(doc, "otto", known=KNOWN, has_webhook=False, now=2, name="Otto")
        self.assertEqual(code, 409)
        self.assertEqual(doc["rows"]["otto"]["message"], "frozen")
        code, body = ga.setup_connected(doc, "otto", known=KNOWN, has_webhook=True, now=2, name="Otto")
        self.assertEqual(code, 200)
        self.assertEqual(body["setup"]["rows"]["otto"]["status"], "connected")
        self.assertEqual(body["setup"]["hint"], {"name": "Otto", "dismissed": False})
        ga.setup_connected(doc, "otto", known=KNOWN, has_webhook=True, now=3, name="Otto")
        self.assertEqual(list(doc["rows"]), ["otto"])
        self.assertEqual(doc["rows"]["otto"]["message"], "frozen")
        self.assertEqual(self.records[0]["webhook"]["key"], "secretkey1")
        self.assertEqual(self.added, [])
        code, body = ga.setup_dismiss_hint(doc, 4)
        self.assertEqual(code, 200)
        self.assertTrue(body["setup"]["hint"]["dismissed"])

    def test_payload_cap_is_off_the_wire_and_defaults_to_500(self):
        q = "y" * 800
        wide = handoff.payload("ab" * 8, "juniper", q, "2026-10-29T00:00:00Z", cap=4000)
        self.assertEqual(len(wide["question"]), 800)
        self.assertNotIn("cap", json.loads(json.dumps(wide)))
        short = handoff.payload("ab" * 8, "juniper", q, "2026-10-29T00:00:00Z")
        self.assertEqual(len(short["question"]), handoff.Q_MAX)
        self.assertEqual(handoff.Q_MAX, 500)




class Rev33Owner(unittest.TestCase):
    """The setup text uses the name stored at station setup, or 'the station owner'."""

    def _auth(self, doc):
        ga.write_json(ga.path("auth.json"), doc)

    def test_owner_name_sam_and_absent(self):
        sam = dict(OTTO, owner="Sam")
        steps = _run("setupSteps(%s)" % json.dumps(sam))
        blob = " ".join(steps)
        self.assertIn("Show Sam a confirm card", steps[0])
        self.assertIn("if Sam says yes", steps[0])
        self.assertIn("Sam's home computer", steps[1])
        self.assertIn("Sam copies them into the station.", steps[3])
        self.assertNotRegex(blob, r"\b(he|she|himself|herself)\b")
        self.assertNotIn("Show you", steps[0])
        self.assertNotIn("the station owner", blob)
        missing = _run("setupSteps(%s)" % json.dumps(OTTO))
        self.assertIn("Show the station owner a confirm card", missing[0])
        self.assertIn("if the station owner says yes", missing[0])
        self.assertIn("the station owner's home computer", missing[1])
        self.assertIn("the station owner copies them into the station.", missing[3])
        self.assertNotRegex(" ".join(missing), r"\b(he|she|himself|herself)\b")
        self.assertNotIn("Show you", missing[0])
        text = ga.setup_message("Otto", "Hey Otto", "Office computer", owner="Sam")
        self.assertEqual(text, ga.setup_message("Otto", "Hey Otto", "Office computer", owner="Sam"))
        self.assertEqual(ga.setup_message("Otto", "Hey Otto", "Office computer", owner=""),
                         ga.setup_message("Otto", "Hey Otto", "Office computer", owner=""))
        auth = ga.path("auth.json")
        existed = os.path.isfile(auth)
        raw = open(auth, encoding="utf-8").read() if existed else None
        ga._SETUP_INFLIGHT.clear()
        try:
            self._auth({"name": "Sam"})
            self.assertEqual(ga.setup_owner_name(), "Sam")
            self.assertEqual(ga.setup_public({"rows": {}}, 1)["owner"], "Sam")
            self._auth({"username": "admin", "password": "kept"})
            self.assertEqual(ga.setup_owner_name(), "")
            self._auth({"password": "kept"})
            self.assertEqual(ga.setup_owner_name(), "")
            none = ga.setup_message("Otto", "Hey Otto", "Office computer")
            self.assertIn("the station owner", none)
            self.assertNotRegex(none, r"\b(he|she|himself|herself)\b")
            self.assertNotIn("Show you", none)
            self._auth({"name": "Sam", "password": "kept"})
            doc = {"rows": {}}
            code, body = ga.setup_send(
                doc, "otto", known=KNOWN, main_slug="juniper", main_name="Juniper", main_outdated=False,
                now=1000, poster=lambda msg: True, name="Otto", wake="Hey Otto", computer="Office computer")
            self.assertEqual(code, 200)
            self.assertIn("Sam's home computer", doc["rows"]["otto"]["message"])
            self.assertNotIn("message", body["setup"]["rows"]["otto"])
            self.assertEqual(body["setup"]["owner"], "Sam")
            self._auth({"password": "kept"})
            ga.setup_send(
                doc, "otto", known=KNOWN, main_slug="juniper", main_name="Juniper", main_outdated=False,
                now=1005, poster=lambda msg: True, name="Otto", wake="Hey Otto", computer="Office computer")
            self.assertIn("Sam", doc["rows"]["otto"]["message"])
            self.assertNotIn("the station owner", doc["rows"]["otto"]["message"])
        finally:
            ga._SETUP_INFLIGHT.clear()
            if existed:
                with open(auth, "w", encoding="utf-8") as f:
                    f.write(raw)
            elif os.path.isfile(auth):
                os.remove(auth)






class Rev33Computer(unittest.TestCase):
    """The computer name is a suggestion: this bot's computer, then the main bot's. It stays editable."""

    def test_suggestion_order_is_own_then_main_and_not_locked(self):
        send = ADMIN[ADMIN.index("def bots_setup_send"):ADMIN.index("def bots_setup_draft")]
        self.assertNotIn('ctx["hosts"].get(ctx["main"]) or ctx["hosts"].get(slug)', send)
        self.assertIn('setup_computer_suggestions(ctx["hosts"].get(slug), ctx["hosts"].get(ctx["main"]))', send)
        self.assertEqual(ga.setup_computer_suggestions("Den laptop", "Office computer"),
                         ["Den laptop", "Office computer"])
        self.assertEqual(ga.setup_computer_suggestions("Office computer", "Office computer"), ["Office computer"])
        self.assertEqual(ga.setup_computer_suggestions("", "Office computer"), ["Office computer"])
        self.assertEqual(ga.setup_computer_suggestions("  ", None), [])
        html = _run("setupComputerFieldHTML(%s, %s, %s)" % (
            json.dumps("Den laptop"), json.dumps("Office computer"), json.dumps("Den laptop")))
        self.assertIn("<datalist", html)
        self.assertIn('list="psthosts"', html)
        self.assertLess(html.index(">Den laptop<") if ">Den laptop<" in html else html.index("Den laptop"),
                        html.index("Office computer"))
        self.assertLess(html.index("Den laptop"), html.index("Office computer"))
        self.assertNotIn("readonly", html.lower())
        self.assertNotIn("disabled", html.lower())
        paste = JS[JS.index("function openPasteValues("):JS.index("\nfunction ", JS.index("function openPasteValues("))]
        self.assertIn("setupComputerFieldHTML", paste)
        self.assertNotIn("readonly", paste.lower())
        listed = _run("setupComputerSuggestions('Den laptop', 'Office computer')")
        self.assertEqual(listed, ["Den laptop", "Office computer"])




class Rev33RelayTarget(unittest.IsolatedAsyncioTestCase):
    """setup-relay may post only to the configured main bot. Any other slug is refused with no outbound call."""

    async def test_other_slug_is_refused_and_never_posts(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        import relay

        r = relay
        tmp = tempfile.mkdtemp()
        snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "PERSONAS_FILE": r.PERSONAS_FILE, "ADMIN_TOKEN": r.ADMIN_TOKEN,
            "voices": set(r.VOICES),
            "personas": {k: (dict(v) if isinstance(v, dict) else v) for k, v in r._personas.items()},
            "ClientSession": r.ClientSession,
        }
        calls = []

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            def __init__(self, *a, **k):
                calls.append("open")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, *a, **k):
                calls.append(url)
                return _Resp()

        main_url = "https://api2.cursor.sh/automations/webhook/one"
        other_url = "https://api2.cursor.sh/automations/webhook/two"
        client = None
        try:
            r.WEBHOOKS_FILE = os.path.join(tmp, "webhooks.json")
            r.PERSONAS_FILE = os.path.join(tmp, "personas.json")
            r.ADMIN_TOKEN = b"admin-secret"
            r.VOICES.clear()
            r.VOICES.update({"ara"})
            r._personas["mtime"] = None
            with open(r.PERSONAS_FILE, "w", encoding="utf-8") as f:
                json.dump({"personas": [
                    {"slug": "juniper", "name": "Juniper", "voice": "ara", "prompt": "The notes helper.",
                     "aliases": [], "facts": []},
                    {"slug": "otto", "name": "Otto", "voice": "ara", "prompt": "The notes helper.",
                     "aliases": [], "facts": []},
                ]}, f)
            os.makedirs(os.path.dirname(r.overlay_path()), exist_ok=True)
            with open(r.overlay_path(), "w", encoding="utf-8") as f:
                json.dump({"list_source": "juniper"}, f)
            self.assertTrue(r.write_webhooks({
                "juniper": {"url": main_url, "token": "main-token-aaaa", "saved_at": 10},
                "otto": {"url": other_url, "token": "otto-token-bbbb", "saved_at": 10},
            }))
            r.ClientSession = _Session
            app = web.Application()
            app.add_routes(r.ADMIN_ROUTES)
            client = TestClient(TestServer(app))
            await client.start_server()
            headers = {"Authorization": "Bearer admin-secret", "X-Client-Cert-Fp": "c" * 64}
            body = {"actor": "admin", "message": "Step 1: Show the station owner a confirm card."}
            ok = await client.post("/admin/bots/setup-relay", headers=headers,
                                   json=dict(body, slug="juniper"))
            self.assertEqual(ok.status, 202, await ok.text())
            self.assertEqual(calls, ["open", main_url])
            calls.clear()
            refused = await client.post("/admin/bots/setup-relay", headers=headers,
                                        json=dict(body, slug="otto"))
            self.assertGreaterEqual(refused.status, 400)
            self.assertLess(refused.status, 500)
            self.assertEqual(calls, [])
            # F3: fixed error text, nothing from the request echoed back.
            self.assertEqual(await refused.json(), {"error": "not found"})
            odd = await client.post("/admin/bots/setup-relay", headers=headers,
                                    json=dict(body, slug="pip<x>"))
            self.assertEqual((odd.status, await odd.json()), (404, {"error": "not found"}))
            self.assertEqual(calls, [])
        finally:
            if client is not None:
                await client.close()
            r.ClientSession = snap["ClientSession"]
            r.WEBHOOKS_FILE = snap["WEBHOOKS_FILE"]
            r.PERSONAS_FILE = snap["PERSONAS_FILE"]
            r.ADMIN_TOKEN = snap["ADMIN_TOKEN"]
            r.VOICES.clear()
            r.VOICES.update(snap["voices"])
            r._personas.clear()
            r._personas.update(snap["personas"])
            shutil.rmtree(tmp, ignore_errors=True)


class ReviewOwnerEscaped(unittest.TestCase):
    """The owner name is escaped in the on-screen steps, like the bot name and computer."""

    def test_owner_name_is_html_escaped(self):
        row = dict(OTTO, owner='<img src=x onerror="a()">')
        real_esc = JS[JS.index("const esc = s =>"):JS.index(";\n", JS.index("const esc = s =>")) + 1]
        prog = real_esc + "\n" + _fn("setupOwnerLabel") + "\n" + _fn("setupMark") + "\n" + _fn("setupSteps") + \
            "\nprocess.stdout.write(JSON.stringify(setupSteps(" + json.dumps(row) + ")));"
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True)
        blob = " ".join(json.loads(out.stdout))
        self.assertNotIn("<img", blob)
        self.assertIn("&lt;img src=x", blob)


if __name__ == "__main__":
    unittest.main()

