"""Headless Chrome harness for the Bots list repaint (0.13.36).

Loads the real admin/web/app.js (fetch stubbed, so boot() stays idle), builds a Bots page element with an
accepted setup row, runs a scenario against the real paintBots and returns its JSON.
"""
import html as _html
import json
import os
import re
import shutil
import tempfile

from chrome_harness import find_chrome, run_chrome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = find_chrome()


def chrome(scenario, root=ROOT, budget=4000):
    with open(os.path.join(root, "admin", "web", "app.js"), encoding="utf-8") as f:
        js = f.read()
    page = """<!doctype html><html><head><meta charset=utf-8></head><body>
<div id="page" class="body bots"><div id="bsum"></div><div id="blist"></div><div id="vere"></div></div>
<pre id=out></pre>
<script>
window.onerror = (m, s, l) => { document.getElementById('out').textContent = JSON.stringify({err: m, l: l}); };
window.calls = [];
window.fetch = (url, opt) => {
  window.calls.push({url: String(url), method: (opt && opt.method) || 'GET', body: (opt && opt.body) || ''});
  if (!window.answer) return new Promise(() => {});
  const r = window.answer(String(url), opt || {});
  if (!r) return new Promise(() => {});
  return Promise.resolve(new Response(JSON.stringify(r[1] || {}), {status: r[0], headers: {'Content-Type': 'application/json'}}));
};
</script>
<script>%s</script>
<script>
const res = {};
const el = document.getElementById('page');
const bots = [{slug: 'otto', name: 'Otto', webhook: {configured: false}}];
el._setup = {owner: '', rows: {otto: {status: 'not_set_up', relay: 'accepted', relay_path: 'relay'}}};
const L = () => document.getElementById('blist');
const hook = f => L().querySelector('.setpanel[data-slug="otto"] input[data-hook="' + f + '"]');
const ae = () => { const a = document.activeElement; return a && a.getAttribute ? (a.getAttribute('data-hook') || a.getAttribute('data-setedit') || a.tagName) : null; };
const tick = ms => new Promise(r => setTimeout(r, ms || 0));
(async () => { try { paintBots(el, bots); %s } catch (e) { res.err = String(e) + ' ' + (e.stack || ''); }
  document.getElementById('out').textContent = JSON.stringify(res); })();
</script></body></html>""" % (js.replace("</script", "<\\/script"), scenario)
    d = tempfile.mkdtemp()
    try:
        inner = os.path.join(d, "inner.html")
        with open(inner, "w", encoding="utf-8") as f:
            f.write(page)
        out = run_chrome([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--allow-file-access-from-files",
                          "--window-size=1200,900", "--virtual-time-budget=%d" % budget, "--dump-dom", "file://" + inner])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    m = re.search(r'<pre id="out">(.*?)</pre>', out, re.S)
    assert m and m.group(1).strip(), "no result from Chrome"
    data = json.loads(_html.unescape(m.group(1)))
    assert "err" not in data, data
    return data
