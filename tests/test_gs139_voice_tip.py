#!/usr/bin/env python3
"""gs-configure-all rev3.6 #2: the team tip moves into a Voice tip button in the Bots header.

Renders the real botsHeaderShell / bindVoiceTip / paintTeam / trapTab from app.js with admin.css in headless
Chrome at 1280 (desktop) and 390 (phone, 767 px breakpoint) and checks size, label, popover placement,
z-order, Esc / outside tap / button close with focus return. Fails on 0.13.8 (no #vtip; inline #teamcard).
"""
import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs139_setup_bots as base  # noqa: E402

JS = base.JS
CSS = base.CSS
CHROME = shutil.which("google-chrome") or shutil.which("chromium")
FNS = ("trapTab", "botsHeaderShell", "teamState", "teamPhraseHtml", "paintTeam", "closeVoiceTip", "bindVoiceTip")


def _src():
    out = []
    for n in FNS:
        out.append(base._fn(n))
    return "\n".join(out)


def _const(name):
    i = JS.index("const " + name + " = ")
    return JS[i:JS.index(";\n", i) + 1]


SCENARIO = r"""
  const btn = document.getElementById('vtip'), pop = document.getElementById('vtippop');
  res.btn = R(btn); res.label = getComputedStyle(btn.querySelector('.vtl')).display;
  res.aria = btn.getAttribute('aria-label'); res.first = document.querySelector('#bothdr button').id;
  res.closedHidden = pop.hidden;
  btn.click();
  res.open = {hidden: pop.hidden, exp: btn.getAttribute('aria-expanded'), pop: R(pop), z: getComputedStyle(pop).zIndex,
    icon: getComputedStyle(btn.querySelector('.ico')).color, text: pop.textContent.trim(),
    wake: getComputedStyle(pop.querySelector('.wake')).color, hdr: R(document.getElementById('hdr'))};
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  res.esc = {hidden: pop.hidden, exp: btn.getAttribute('aria-expanded'), focus: document.activeElement === btn};
  btn.click(); document.getElementById('far').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
  res.outside = pop.hidden;
  btn.click(); btn.click();
  res.toggle = {hidden: pop.hidden, focus: document.activeElement === btn};
"""


def chrome(width):
    page = """<!doctype html><html><head><meta name=viewport content="width=device-width"><style>%s</style></head>
<body><header class="top" id="hdr"><h1>Bots</h1><div class="r"><div id="topr"></div></div></header>
<button id="far" type="button">far</button><pre id=out></pre>
<script>window.onerror=(m,s,l)=>{document.getElementById('out').textContent=JSON.stringify({err:m,l:l});};</script>
<script>
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
%s
%s
%s
%s
document.getElementById('topr').innerHTML = botsHeaderShell();
bindVoiceTip();
paintTeam({_bots: [{name: 'Juniper', main: true}, {name: 'Otto', slug: 'otto'}], _imported: {rows: [], updated_at: 1}});
const R = n => { const q = n.getBoundingClientRect(); return {x:Math.round(q.left), y:Math.round(q.top), w:Math.round(q.width), h:Math.round(q.height)}; };
const res = {width: innerWidth};
try { %s } catch (e) { res.err = String(e); }
document.getElementById('out').textContent = JSON.stringify(res);
</script></body></html>""" % (CSS, base._icons(), "\n".join(_const(c) for c in ("ICO_WAVE", "TEAM_PHRASE")),
                              "", _src(), SCENARIO)
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "inner.html"), "w") as f:
        f.write(page)
    outer = os.path.join(d, "outer.html")
    with open(outer, "w") as f:
        f.write('<!doctype html><body style="margin:0"><iframe id=f src="inner.html" style="border:0;width:%dpx;'
                'height:700px"></iframe><pre id=res></pre><script>document.getElementById("f").onload=()=>{setTimeout('
                '()=>{document.getElementById("res").textContent=document.getElementById("f").contentDocument.'
                'getElementById("out").textContent;},500)};</script></body>' % width)
    out = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--allow-file-access-from-files",
                          "--window-size=1400,900", "--virtual-time-budget=4000", "--dump-dom", "file://" + outer],
                         capture_output=True, text=True, timeout=120).stdout
    shutil.rmtree(d, ignore_errors=True)
    m = re.search(r'<pre id="res">(.*?)</pre>', out, re.S)
    assert m and m.group(1).strip(), "no result from Chrome"
    data = json.loads(_html.unescape(m.group(1)))
    assert "err" not in data, data
    return data


TEAL = "rgb(63, 211, 182)"


class VoiceTipSource(unittest.TestCase):
    def test_inline_tip_card_is_gone(self):
        self.assertNotIn('id="teamcard"', JS)
        self.assertNotIn("#teamcard", CSS)

    def test_states_and_focus_rules(self):
        self.assertIn(".vtipbtn:hover{background:rgba(255,255,255,.04)}", CSS)
        self.assertIn(".vtipbtn:focus-visible{outline:2px solid var(--gs-teal)", CSS)
        self.assertIn(".vtipbtn[aria-expanded=true] .ico{color:var(--gs-teal)}", CSS)


@unittest.skipUnless(CHROME, "Chrome not installed")
class VoiceTipChrome(unittest.TestCase):
    def _common(self, d):
        self.assertEqual(d["first"], "vtip", d)
        self.assertEqual(d["aria"], "Voice tip: update team list")
        self.assertTrue(d["closedHidden"])
        o = d["open"]
        self.assertFalse(o["hidden"]); self.assertEqual(o["exp"], "true")
        self.assertEqual(o["z"], "12")
        self.assertEqual(o["icon"], TEAL); self.assertEqual(o["wake"], TEAL)
        self.assertEqual(o["text"], "Added a bot? Say \u201cHey Grok, update my team list.\u201d")
        self.assertEqual(d["esc"], {"hidden": True, "exp": "false", "focus": True})
        self.assertTrue(d["outside"])
        self.assertEqual(d["toggle"], {"hidden": True, "focus": True})

    def test_desktop(self):
        d = chrome(1280)
        self._common(d)
        self.assertEqual(d["btn"]["h"], 32, d)
        self.assertNotEqual(d["label"], "none")
        self.assertGreater(d["open"]["pop"]["y"], d["btn"]["y"] + d["btn"]["h"] - 1, d)

    def test_phone(self):
        for w in (390, 320):
            d = chrome(w)
            self._common(d)
            self.assertEqual((d["btn"]["w"], d["btn"]["h"]), (44, 44), d)
            self.assertEqual(d["label"], "none")
            p = d["open"]["pop"]
            self.assertEqual((p["x"], p["w"]), (0, w), d)
            self.assertGreaterEqual(p["y"], d["open"]["hdr"]["y"] + d["open"]["hdr"]["h"], d)


if __name__ == "__main__":
    unittest.main()
