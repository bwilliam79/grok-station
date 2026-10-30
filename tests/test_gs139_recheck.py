#!/usr/bin/env python3
"""gs139 Set up bots: Noor's re-check of eeadb17 (R1-R5) and her two count/name rulings (R6, R7).

R1-R5 render the real app.js functions with admin.css in headless Chrome and measure the result.
R6-R7 run the callout functions under node.
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
NOW = 100000
ROWS = [
    {"slug": "otto", "name": "Otto", "status": "sent", "sent_at": NOW - 60},
    {"slug": "pip", "name": "Pip", "status": "sent", "sent_at": NOW - 600},
    {"slug": "maple", "name": "Maple", "status": "failed", "fails": 2},
]
PAGE_FNS = ("clockTime", "joinBotNames", "setupCopy", "setupStuck", "setupDomId", "setupMark", "setupOwnerLabel",
            "setupSteps", "setupIcon", "setupRowModel", "setupRowHTML", "setupListHTML", "setupMessageDetail",
            "setupMessagePopHTML", "popoverDismiss", "showSetupMessage", "closeBotMessage", "botRepaintDeferred", "flushBotRepaint")


def _src(names):
    out = []
    for n in names:
        if "\nfunction " + n + "(" in JS:
            out.append(base._fn(n))
    return "\n".join(out)


def chrome(width, scenario, extra=()):
    """Render the Set up bots list (plus phone header and tab bar) at `width`, run `scenario`, return its JSON."""
    page = """<!doctype html><html><head><meta name=viewport content="width=device-width"><style>%s</style></head>
<body><div class="top" id="hdr"><h1>Bots</h1></div><div class="body bots" id="host"></div>
<nav class="phonebar" id="pbar"><button type="button">Home</button><button type="button" class="on">Bots</button></nav>
<pre id=out></pre><script>window.onerror=(m,s,l)=>{document.getElementById('out').textContent=JSON.stringify({err:m,l:l});};</script>
<script>
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
let botMessageCloser = null;
function openMainTest() {}
%s
%s
const rows = %s;
document.getElementById('host').innerHTML = setupListHTML(rows, {mainName:'Juniper', now:%d, expanded:false});
const R = n => { const q = n.getBoundingClientRect(); return {x:Math.round(q.left), y:Math.round(q.top), w:Math.round(q.width), h:Math.round(q.height)}; };
const res = {width: innerWidth};
(async () => { try { %s } catch (e) { res.err = String(e); }
  document.getElementById('out').textContent = JSON.stringify(res); })();
</script></body></html>""" % (CSS, base._icons(), _src(PAGE_FNS + tuple(extra)), json.dumps(ROWS), NOW, scenario)
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


OPEN_MAPLE = """
  const btn = document.querySelector('.ssub[data-ssub="maple"]');
  showSetupMessage(btn, document.body, rows[2], {mainName:'Juniper', now:%d});
""" % NOW

# Close must be at least 44x44 and the top element at every point of a 44x44 square centred on it (5x5 grid).
CLOSE_PROBE = OPEN_MAPLE + """
  const x = document.getElementById('smsgx'); const q = x.getBoundingClientRect();
  res.close = R(x); let miss = [];
  for (let i = 0; i < 5; i++) for (let j = 0; j < 5; j++) {
    const cx = q.left + q.width / 2, cy = q.top + q.height / 2;
    const px = cx - 21.5 + 43 * i / 4, py = cy - 21.5 + 43 * j / 4;
    const hit = document.elementFromPoint(px, py);
    if (!hit || !(hit === x || x.contains(hit))) miss.push([Math.round(px), Math.round(py), hit ? (hit.id || hit.className || hit.tagName) : null]);
  }
  res.miss = miss; res.vh = innerHeight; res.bar = R(document.getElementById('pbar'));
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class R1PhoneSheetCloseClearsTabBar(unittest.TestCase):
    def test_close_is_a_full_unobstructed_44_target(self):
        for w in (390, 320):
            d = chrome(w, CLOSE_PROBE)
            self.assertGreaterEqual(d["close"]["h"], 44, (w, d))
            self.assertGreaterEqual(d["close"]["w"], 44, (w, d))
            self.assertEqual(d["miss"], [], (w, d))
            self.assertLessEqual(d["close"]["y"] + d["close"]["h"], d["bar"]["y"], (w, d))

    def test_shared_pop_rule_pads_by_tab_bar_plus_safe_area(self):
        tab_h = re.search(r"\.tabs\{[^}]*height:(\d+)px", CSS).group(1)
        pop = re.search(r"\n  \.pop\{([^}]*)\}", CSS).group(1)
        self.assertIn("calc(%spx + env(safe-area-inset-bottom))" % tab_h, pop)


CHIP_PROBE = """
  res.chips = [...document.querySelectorAll('.ssub .schip')].map(c => ({t: c.textContent.trim(), w: Math.round(c.getBoundingClientRect().width),
     sw: c.scrollWidth, cw: c.clientWidth, ct: (c.querySelector('.ct') || c).scrollWidth > (c.querySelector('.ct') || c).clientWidth}));
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class R2ChipNeverTruncates(unittest.TestCase):
    def test_chip_text_whole_at_320(self):
        for w in (320, 390):
            d = chrome(w, CHIP_PROBE)
            self.assertTrue(d["chips"], d)
            for c in d["chips"]:
                self.assertFalse(c["ct"], (w, c))
                self.assertLessEqual(c["sw"], c["cw"] + 1, (w, c))


SCRIM_PROBE = OPEN_MAPLE + """
  const h = document.getElementById('hdr').getBoundingClientRect();
  const hit = document.elementFromPoint(h.left + h.width / 2, h.top + h.height / 2);
  res.header = hit ? (hit.dataset.smsgScrim ? 'scrim' : (hit.id || hit.className || hit.tagName)) : null;
  const b = document.getElementById('pbar').getBoundingClientRect();
  const hb = document.elementFromPoint(b.left + 10, b.bottom - 4);
  res.bar = hb ? (hb.dataset.smsgScrim ? 'scrim' : (hb.closest('.pop') ? 'sheet' : (hb.id || hb.className || hb.tagName))) : null;
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class R3ScrimCoversHeaderAndTabBar(unittest.TestCase):
    def test_header_hit_lands_on_scrim_while_sheet_open(self):
        for w in (390, 320):
            d = chrome(w, SCRIM_PROBE)
            self.assertEqual(d["header"], "scrim", (w, d))
            self.assertIn(d["bar"], ("scrim", "sheet"), (w, d))


KEY_PROBE = """
  const added = [], removed = [];
  const ae = document.addEventListener.bind(document), re_ = document.removeEventListener.bind(document);
  document.addEventListener = (t, f, o) => { added.push(t); ae(t, f, o); };
  document.removeEventListener = (t, f, o) => { removed.push(t); re_(t, f, o); };
  const outside = document.createElement('button'); outside.id = 'outside'; outside.textContent = 'x';
  document.body.appendChild(outside);
  const btn = document.querySelector('.ssub[data-ssub="maple"]');
  const open = () => showSetupMessage(btn, document.body, rows[2], {mainName:'Juniper', now:%d});
  // 1: focus leaves the popover (Tab out) -> it closes.
  open(); document.getElementById('smsgx').focus(); outside.focus();
  res.closedOnFocusOut = !document.getElementById('smsgpop');
  // 2: if focus sits outside while still open, Esc on the document closes it and returns focus to the row.
  open();
  if (document.getElementById('smsgpop')) {
    document.body.focus();
    document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  }
  res.closedOnDocEsc = !document.getElementById('smsgpop');
  res.focusBack = document.activeElement === btn;
  // 3: listeners added while open are all removed on close.
  const cnt = a => a.filter(t => t === 'keydown' || t === 'focusin').length;
  res.added = cnt(added); res.removed = cnt(removed);
""" % NOW


@unittest.skipUnless(CHROME, "Chrome not installed")
class R4DesktopPopoverEscAndFocusOut(unittest.TestCase):
    def test_esc_on_document_and_focus_leaving_close_it(self):
        d = chrome(1280, KEY_PROBE)
        self.assertTrue(d["closedOnFocusOut"], d)
        self.assertTrue(d["closedOnDocEsc"], d)
        self.assertTrue(d["focusBack"], d)
        self.assertGreater(d["added"], 0, d)
        self.assertEqual(d["added"], d["removed"], d)


LINK_PROBE = OPEN_MAPLE + """
  const l = document.querySelector('#smsgpop [data-act="maintest"]'); res.link = R(l);
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class R5DesktopTestLinkHeight(unittest.TestCase):
    def test_test_link_is_24_tall(self):
        for w in (1280, 768):
            d = chrome(w, LINK_PROBE)
            self.assertGreaterEqual(d["link"]["h"], 24, (w, d))


def _callout(el_js):
    names = ("joinBotNames", "setupCopy", "botsCalloutKind", "botsCalloutHTML", "setupComputerSuggestions",
             "setupMainComputer", "setupListRows", "setupUnconnected", "pendingTeammates", "botIdFromName", "paintBotCallout")
    src = _src(names)
    i = JS.index("const BOT_ID_RE = ")
    prog = ("const esc = s => String(s == null ? '' : s);\n" + base._icons() + "\n" + JS[i:JS.index(";\n", i) + 1] + "\n" +
            "const host = {innerHTML: ''};\nconst $ = s => (s === '#botcall' ? host : null);\n"
            "const document = {getElementById: () => null};\nfunction finishNeedsUpdate() { return false; }\n" +
            src + "\npaintBotCallout(" + el_js + ");\nprocess.stdout.write(JSON.stringify(host.innerHTML));")
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr[-800:])
    return re.sub(r"<[^>]+>", " ", json.loads(out.stdout))


def _fixture(main_name="Juniper", setup=None, imported=("Nova", "Wren")):
    bots = [{"slug": "juniper", "name": main_name, "main": True},
            {"slug": "otto", "name": "Otto"}, {"slug": "pip", "name": "Pip"},
            {"slug": "ivy", "name": "Ivy", "webhook": {"configured": True}}]
    el = {"_bots": bots, "_setup": {"rows": setup or {"pip": {"status": "connected"}}},
          "_imported": {"rows": [{"name": n, "state": "new"} for n in imported]}}
    return json.dumps(el)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class R6CalloutCountsSetupList(unittest.TestCase):
    def test_count_matches_unconnected_setup_rows(self):
        # Otto (not set up), Nova and Wren (roster) are unconnected; Pip connected, Ivy already.
        txt = _callout(_fixture())
        self.assertIn("3 bots not connected", txt)
        self.assertIn("Configure all", txt)

    def test_singular_uses_same_count(self):
        txt = _callout(_fixture(imported=(), setup={"pip": {"status": "connected"}}))
        self.assertIn("1 bot not connected", txt)
        self.assertIn("Set up Otto", txt)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class R7CalloutUsesMainBotName(unittest.TestCase):
    def test_real_name(self):
        self.assertIn("Maeve can send each one its setup message.", _callout(_fixture(main_name="Maeve")))

    def test_fallback_is_capitalised(self):
        self.assertIn("Your main bot can send each one its setup message.", _callout(_fixture(main_name="")))

    def test_no_hard_coded_name(self):
        self.assertNotIn("Juniper", base._fn("botsCalloutHTML"))


BOT_FNS = ("botMessageLead", "botMessageDetail", "botMessagePopHTML", "repliesFrom", "showBotMessage")
BOT_FNS += ("botRepaintDeferred", "flushBotRepaint")
BOT_KEY_PROBE = """
  const added = [], removed = [];
  const ae = document.addEventListener.bind(document), re_ = document.removeEventListener.bind(document);
  document.addEventListener = (t, f, o) => { added.push(t); ae(t, f, o); };
  document.removeEventListener = (t, f, o) => { removed.push(t); re_(t, f, o); };
  const host = document.createElement('div');
  host.innerHTML = '<div class="botrow" data-slug="otto"><span>O</span><div class="info"><button type="button" class="bmsg" ' +
    'data-bmsg="otto" aria-expanded="false"><span class="tx">Couldn\\'t reach Otto. Check its computer.</span></button></div></div>' +
    '<button type="button" id="outside">x</button>';
  document.body.appendChild(host);
  const el = {_bots: [{slug: 'otto', name: 'Otto'}]};
  const btn = host.querySelector('.bmsg'), outside = document.getElementById('outside');
  const open = () => showBotMessage(btn, el);
  // 1: focus moves outside the popover -> it closes, focus stays where it went.
  open(); res.opened = !!document.getElementById('bmsgpop'); outside.focus();
  res.closedOnFocusOut = !document.getElementById('bmsgpop'); res.focusStayed = document.activeElement === outside;
  // 2: focus parked outside the popover while open; Esc on the document closes it and returns focus to the row.
  open();
  if (document.getElementById('bmsgpop')) {
    document.activeElement.blur();
    document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  }
  res.closedOnDocEsc = !document.getElementById('bmsgpop');
  res.focusBack = document.activeElement === btn;
  res.expanded = btn.getAttribute('aria-expanded');
  // 3: Close button still works and close runs once.
  open(); document.getElementById('bmsgx').click(); res.closedOnX = !document.getElementById('bmsgpop');
  const cnt = a => a.filter(t => t === 'keydown' || t === 'focusin').length;
  res.added = cnt(added); res.removed = cnt(removed);
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class R8BotRowPopoverEscAndFocusOut(unittest.TestCase):
    def test_esc_on_document_and_focus_leaving_close_it(self):
        d = chrome(1280, BOT_KEY_PROBE, BOT_FNS)
        self.assertTrue(d["opened"], d)
        self.assertTrue(d["closedOnFocusOut"], d)
        self.assertTrue(d["focusStayed"], d)
        self.assertTrue(d["closedOnDocEsc"], d)
        self.assertTrue(d["focusBack"], d)
        self.assertEqual(d["expanded"], "false", d)
        self.assertTrue(d["closedOnX"], d)
        self.assertEqual(d["added"], 6, d)
        self.assertEqual(d["added"], d["removed"], d)


# F1: the Bots list repaints every 3 s. A repaint while the row popover is open must not swap the .bmsg out
# from under it: it waits until the popover closes, then runs once.
F1_PROBE = """
  const host = document.createElement('div');
  const rowHTML = '<div class="botrow" data-slug="otto"><span>O</span><div class="info"><button type="button" class="bmsg" ' +
    'data-bmsg="otto" aria-expanded="false"><span class="tx">Couldn\\'t reach Otto.</span></button></div></div>';
  host.innerHTML = rowHTML; document.body.appendChild(host);
  const el = {_bots: [{slug: 'otto', name: 'Otto'}]};
  let paints = 0;
  const repaint = () => { paints++; host.innerHTML = rowHTML; };
  // What the 3 s refresh calls: paintBots defers through botRepaintDeferred when a popover is open.
  const tick = () => { if (typeof botRepaintDeferred === 'function' && botRepaintDeferred(repaint)) return; repaint(); };
  showBotMessage(host.querySelector('.bmsg'), el);
  tick(); tick();
  const pop = document.getElementById('bmsgpop');
  const live = host.querySelector('.bmsg[data-bmsg="otto"]');
  res.expanded = live.getAttribute('aria-expanded');
  res.focusInPop = !!(pop && pop.contains(document.activeElement));
  res.paintsWhileOpen = paints;
  document.getElementById('bmsgx').click();
  res.paintsAfter = paints;
  res.closed = !document.getElementById('bmsgpop');
  const now = host.querySelector('.bmsg[data-bmsg="otto"]');
  res.focusBack = document.activeElement === now;
  res.expandedAfter = now.getAttribute('aria-expanded');
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class F1RepaintKeepsBotPopover(unittest.TestCase):
    def test_repaint_while_open_keeps_expanded_and_focus(self):
        d = chrome(1280, F1_PROBE, BOT_FNS)
        self.assertEqual(d["expanded"], "true", d)
        self.assertTrue(d["focusInPop"], d)
        self.assertEqual(d["paintsWhileOpen"], 0, d)
        self.assertTrue(d["closed"], d)
        self.assertEqual(d["paintsAfter"], 1, d)
        self.assertTrue(d["focusBack"], d)
        self.assertEqual(d["expandedAfter"], "false", d)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class F2CalloutSingularCopy(unittest.TestCase):
    def _one(self, name):
        return _callout(_fixture(main_name=name, imported=(), setup={"pip": {"status": "connected"}}))

    def test_one_bot_with_main_name(self):
        txt = self._one("Maeve")
        self.assertIn("Maeve can send Otto its setup message.", txt)
        self.assertNotIn("each one", txt)

    def test_one_bot_without_main_name(self):
        txt = self._one("")
        self.assertIn("Your main bot can send Otto its setup message.", txt)
        self.assertNotIn("each one", txt)

    def test_plural_unchanged(self):
        self.assertIn("Maeve can send each one its setup message.", _callout(_fixture(main_name="Maeve")))


if __name__ == "__main__":
    unittest.main()
