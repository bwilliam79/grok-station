"""Headless Chrome harness for the phone pending-row sheet (#pendpop), Noor's b8b1fe6 re-check (P1-P4).

Renders the real openPendingSheet/closePendingSheet (and trapTab when present) from app.js with admin.css,
plus the phone sticky header and tab bar, runs a scenario and returns its JSON.
"""
import html as _html
import json
import os
import re
import shutil
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8") as _f:
    JS = _f.read()
with open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8") as _f:
    CSS = _f.read()
CHROME = shutil.which("google-chrome") or shutil.which("chromium")
FNS = ("trapTab", "closePendingSheet", "openPendingSheet")


def fn(name):
    body = JS[JS.index("\nfunction %s(" % name) + 1:]
    return body[:body.index("\n}\n") + 2]


def _src():
    return "\n".join(fn(n) for n in FNS if "\nfunction %s(" % n in JS)


def chrome(width, scenario):
    page = """<!doctype html><html><head><meta name=viewport content="width=device-width"><style>%s</style></head>
<body><div class="top" id="hdr"><h1>Bots</h1></div><div class="body bots" id="host"><div id="blist">
<div class="botrow" data-pending="k1"><div></div><div><div class="lbl">Otto</div></div><div class="ww"></div>
<div class="cfg"><button class="btn sm" type="button" data-setup="k1">Set up</button></div></div></div></div>
<nav class="phonebar" id="pbar"><button type="button">Home</button><button type="button" class="on">Bots</button></nav>
<pre id=out></pre><script>window.onerror=(m,s,l)=>{document.getElementById('out').textContent=JSON.stringify({err:m,l:l});};</script>
<script>
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
window.removed = [];
function removePending(k) { window.removed.push(k); }
%s
const row = document.querySelector('.botrow[data-pending]');
const R = n => { const q = n.getBoundingClientRect(); return {x:Math.round(q.left), y:Math.round(q.top), w:Math.round(q.width), h:Math.round(q.height)}; };
const who = n => !n ? null : (n.classList && n.classList.contains('scrim') ? 'scrim' : (n.closest && n.closest('.pop') ? 'sheet' : (n.id || n.className || n.tagName)));
const ae = () => { const a = document.activeElement; return a ? (a.tagName + (a.dataset && a.dataset.close !== undefined ? '[close]' : '') + (a.dataset && a.dataset.remove ? '[remove]' : '') + (a.dataset && a.dataset.setup ? '[setup]' : '')) : null; };
const key = (k, shift) => (document.activeElement || document.body).dispatchEvent(new KeyboardEvent('keydown', {key:k, shiftKey:!!shift, bubbles:true, cancelable:true}));
const res = {width: innerWidth};
(async () => { try { openPendingSheet(row, document.body); %s } catch (e) { res.err = String(e); }
  document.getElementById('out').textContent = JSON.stringify(res); })();
</script></body></html>""" % (CSS, _src(), scenario)
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
