#!/usr/bin/env python3
"""Bots row compact rev2 (gs138). A message replaces the Replies from line.

No browser. The pure functions are taken from app.js and run under node.
Row height stays 78/79 desktop and 110/111 on a phone because the name line is
24 px, the second line is 20 px, and the padding and phone gap do not grow.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()

FAIL = "Update failed: The bot refused the update."


def _fn(name):
    start = JS.index("\nfunction " + name + "(")
    depth, j = 0, JS.index("{", start)
    for k in range(j, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[k], 0)
        if depth == 0:
            return JS[start:k + 1]
    raise AssertionError("unclosed " + name)


def _run(expr):
    prog = "\n".join(_fn(n) for n in (
        "botMessageLead", "botMessageAnnounce", "botMessageLineHTML", "botRowL2HTML",
        "botMessageDetail", "botMessagePopHTML", "botRowTapOpensConfigure"))
    prog = "const esc = s => String(s == null ? '' : s);\n" + prog + \
        "\nprocess.stdout.write(JSON.stringify(" + expr + "));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class BotsRowCompact(unittest.TestCase):
    def test_lead_is_the_text_before_the_first_colon(self):
        self.assertEqual(_run("botMessageLead(%s)" % json.dumps(FAIL)), "Update failed")
        self.assertEqual(_run("botMessageLead('Hello: there: more')"), "Hello")
        self.assertEqual(_run("botMessageLead('  Needs a look : rest')"), "Needs a look")
        for blank in ("", "no colon here", ": starts with a colon", "   : blank lead"):
            self.assertEqual(_run("botMessageLead(%s)" % json.dumps(blank)), "Needs attention", blank)

    def test_a_message_is_announced_once_per_bot(self):
        first = _run("botMessageAnnounce({}, 'kai', %s)" % json.dumps(FAIL))
        self.assertTrue(first["announce"])
        self.assertEqual(first["said"]["kai"], FAIL)
        again = _run("botMessageAnnounce(%s, 'kai', %s)" % (json.dumps(first["said"]), json.dumps(FAIL)))
        self.assertFalse(again["announce"])
        other = _run("botMessageAnnounce(%s, 'otto', 'Helper out of date: use Update')" % json.dumps(again["said"]))
        self.assertTrue(other["announce"])
        self.assertEqual(other["said"]["kai"], FAIL)
        cleared = _run("botMessageAnnounce(%s, 'kai', '')" % json.dumps(other["said"]))
        self.assertFalse(cleared["announce"])
        self.assertNotIn("kai", cleared["said"])
        back = _run("botMessageAnnounce(%s, 'kai', %s)" % (json.dumps(cleared["said"]), json.dumps(FAIL)))
        self.assertTrue(back["announce"])

    def test_the_message_replaces_the_replies_line(self):
        line = _run("botRowL2HTML('Replies from: home', 'home', %s, true, '<div class=\"offsub\">x</div>', 'kai')"
                    % json.dumps(FAIL))
        self.assertIn(">Replies from: home<", line)
        self.assertIn('class="help"', line)
        self.assertEqual(line.count("<button"), 1)
        self.assertIn('class="bmsg"', line)
        self.assertIn('aria-haspopup="dialog"', line)
        self.assertIn('aria-label="%s, show details"' % FAIL, line)
        self.assertIn("⚠&#xFE0E;", line)
        self.assertIn('role="alert">%s</span>' % FAIL, line)
        self.assertNotIn("offsub", line)
        quiet = _run("botRowL2HTML('Replies from: home', 'home', %s, false, '', 'kai')" % json.dumps(FAIL))
        self.assertNotIn('role="alert"', quiet)
        self.assertIn('class="tx">%s</span>' % FAIL, quiet)
        empty = _run("botRowL2HTML('Replies from: home', 'home', '', false, '<div class=\"offsub\">x</div>', 'kai')")
        self.assertNotIn("bmsg", empty)
        self.assertIn("offsub", empty)
        self.assertIn(">Replies from: home<", empty)

    def test_popover_title_and_phone_sheet(self):
        detail = _run("botMessageDetail('Juniper', 'Replies from: home', %s)" % json.dumps(FAIL))
        self.assertEqual(detail["title"], "Update failed")
        self.assertEqual(detail["body"], FAIL)
        self.assertEqual(detail["context"], "Juniper · Replies from: home")
        desk = _run("botMessagePopHTML(%s, false)" % json.dumps(detail))
        self.assertIn('aria-modal="false"', desk)
        self.assertIn(">Update failed<", desk)
        self.assertIn(">%s</" % FAIL, desk)
        self.assertIn(">Juniper · Replies from: home<", desk)
        self.assertIn(">Close<", desk)
        self.assertNotIn("scrim", desk)
        phone = _run("botMessagePopHTML(%s, true)" % json.dumps(detail))
        self.assertIn('aria-modal="true"', phone)
        plain = _run("botMessageDetail('Otto', 'Replies from: not set', 'the recorder is quiet')")
        self.assertEqual(plain["title"], "Needs attention")

    def test_a_message_tap_does_not_open_configure(self):
        # The message is its own hit target. A phone tap on it must not also open Configure.
        hit = _run("botRowTapOpensConfigure({closest: sel => sel.indexOf('.bmsg') >= 0 ? {} : null}, true)")
        self.assertIs(hit, False)
        button = _run("botRowTapOpensConfigure({closest: sel => sel.indexOf('button') >= 0 ? {} : null}, true)")
        self.assertIs(button, False)
        row = _run("botRowTapOpensConfigure({closest: () => null}, true)")
        self.assertIs(row, True)
        desk = _run("botRowTapOpensConfigure({closest: () => null}, false)")
        self.assertIs(desk, False)
        paint = _fn("paintBots")
        handler = paint.split('querySelectorAll(".bmsg")', 1)[1].split("querySelectorAll(\".botrow\")", 1)[0]
        self.assertIn("stopPropagation()", handler)
        self.assertIn("openBotMessage(btn, el)", handler)
        self.assertIn("botRowTapOpensConfigure(", paint)
        self.assertIn("el._msgSaid", paint)

    def test_row_heights_stay_put_and_the_switch_wins(self):
        # Desktop 16 + name 24 + 2 + line 20 + 16 = 78. A 1 px border makes the next row 79.
        # Phone 16 + 46 + 4 gap + wake word 28 + 16 = 110, plus the border is 111.
        self.assertEqual(16 + 24 + 2 + 20 + 16, 78)
        self.assertEqual(16 + 46 + 4 + 28 + 16, 110)
        flat = CSS.replace(" ", "")
        self.assertIn(".botrow.lbl{height:24px;line-height:24px;", flat)
        self.assertIn(".botrow.l2{position:relative;height:20px;margin-top:2px;", flat)
        self.assertIn(".botrow{height:auto;min-height:76px;padding:16px0;align-items:center}", flat)
        self.assertIn(".botrow{height:auto;grid-template-columns:44pxminmax(0,1fr)auto;min-height:0;padding:16px0;row-gap:4px;align-items:start}", flat)
        self.assertIn(".botrow.ww{min-height:28px}", flat)
        self.assertIn(".botrow.ww{position:relative;z-index:2}", flat)
        self.assertIn(".bmsg::before{content:\"\";position:absolute;inset:-2px-4px}", flat)
        self.assertIn(".bmsg::before{inset:-12px-4px}", flat)
        self.assertLess(flat.index(".botrow.ww{position:relative;z-index:2}"), flat.index(".bmsg::before{inset:-12px-4px}"))
        self.assertNotIn("height:104px", flat)
        self.assertNotIn("height:112px", flat)


if __name__ == "__main__":
    unittest.main()
