"""Headless Chrome for the Bots header actions (0.13.37).

Loads the real admin/web/app.js and boots the Bots page against a stubbed admin API. Chrome's
--dump-dom prints the page only after virtual time runs out, and virtual time does not advance
while a load is still open. A font or picture that never finishes, or a poll that keeps a task
pending, means the dump never starts. The page below resolves every fetch, does not schedule
polls, and does not ask for a file or a network picture, so the page goes idle and Chrome exits.
Same shape as tests/gs1336_repaint_chrome.py. The Chrome process goes through chrome_harness.run_chrome.
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

# A 1x1 gif. Image loads use this instead of /api/portrait or /assets, which are files this page
# does not have. A file read that never finishes holds virtual time open forever.
_DOT = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


def _css(text):
    # @font-face and url(assets/...) fetch files next to the page. Drop the fetches.
    text = re.sub(r"@font-face\{.*?\}", "", text)
    text = re.sub(r"url\((?!data:)[^)]*\)", "url(" + _DOT + ")", text)
    text += "\n*{animation:none!important;transition:none!important;font-family:sans-serif!important}\n"
    return text


def chrome(scenario, budget=4000):
    with open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8") as f:
        js = f.read()
    with open(os.path.join(ROOT, "admin", "web", "deploy-pins.js"), encoding="utf-8") as f:
        pins = f.read()
    with open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8") as f:
        css = _css(f.read())
    page = """<!doctype html><html><head><meta charset=utf-8><link rel=icon href="data:,"><style>%s</style></head><body>
<div id="root"></div><div id="toast"></div><pre id="gsprobe"></pre>
<script>
window.__errs = [];
window.onerror = (m, s, l, c, e) => { window.__errs.push(String(m) + (e && e.stack ? '\\n' + e.stack : '')); };
window.addEventListener('unhandledrejection', ev => {
  const r = ev.reason;
  window.__errs.push(String(r && (r.stack || r.message) || r));
});
window.__calls = [];
const DATA = %s;
DATA.state.stepup_until = Date.now() / 1000 + 600;
DATA.bots.stepup_until = DATA.state.stepup_until;
window.fetch = (url, opt) => {
  const path = String(url).split('?')[0];
  const method = (opt && opt.method) || 'GET';
  window.__calls.push(method + ' ' + path);
  const json = (code, data) => Promise.resolve(new Response(JSON.stringify(data == null ? {} : data),
    {status: code, headers: {'Content-Type': 'application/json'}}));
  if (path === '/api/state') return json(200, DATA.state);
  if (path === '/api/bots') return json(200, DATA.bots);
  if (path === '/api/enroll/status') return json(200, DATA.enroll);
  if (path === '/api/wakewords') return json(200, DATA.wake);
  if (path === '/api/updates') return json(200, {update_available: false, installed: '0.13.37'});
  if (path === '/api/finish-update') return json(200, {computers: []});
  if (path === '/api/bots/refresh-avatars') return json(202, {ok: true, running: false});
  if (path === '/api/bots/rescan') return json(200, {ok: true, added: 0});
  if (path.indexOf('/api/portrait/') === 0) return Promise.resolve(new Response('', {status: 404}));
  return json(200, {});
};
// Fonts and animations are stripped above so --virtual-time-budget can finish.
// Intervals are dropped. Waits over a second are dropped. Short waits still run.
window.setInterval = function () { return 0; };
const _setTimeout = window.setTimeout;
window.setTimeout = function (fn, ms) {
  const n = Number(ms) || 0;
  if (n > 1000) return 0;
  return _setTimeout.apply(this, arguments);
};
window.WebSocket = function () { return {close() {}, send() {}, addEventListener() {}}; };
window.EventSource = function () { return {close() {}, addEventListener() {}}; };
(function () {
  const desc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
  const dot = %s;
  Object.defineProperty(HTMLImageElement.prototype, 'src', {
    configurable: true, enumerable: true,
    get() { return desc.get.call(this); },
    set() { desc.set.call(this, dot); }
  });
})();
location.hash = '#bots';
</script>
<script>%s</script>
<script>%s</script>
<script>
const tick = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const res = {};
  try { %s }
  catch (e) { res.scriptErr = String(e) + (e && e.stack ? '\\n' + e.stack : ''); }
  document.getElementById('gsprobe').textContent = JSON.stringify(res);
})();
</script></body></html>""" % (
        css,
        json.dumps(_payload()),
        json.dumps(_DOT),
        pins.replace("</script", "<\\/script"),
        js.replace("</script", "<\\/script"),
        scenario,
    )
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "page.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        out = run_chrome(
            [CHROME, "--headless=old", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
             "--disable-background-networking", "--disable-component-update", "--disable-sync",
             "--disable-default-apps", "--disable-extensions", "--no-first-run",
             "--allow-file-access-from-files",
             "--window-size=1280,900", "--virtual-time-budget=%d" % max(1000, int(budget)),
             "--dump-dom",
             "file://" + path])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    m = re.search(r'<pre id="gsprobe">(.*?)</pre>', out, re.S)
    assert m and m.group(1).strip(), "no result from Chrome"
    data = json.loads(_html.unescape(m.group(1)))
    assert "scriptErr" not in data, data
    return data


def _payload():
    import base64
    pub = base64.b64encode(b"k" * 32).decode()
    bot = {
        "paired": False, "public_key": None, "main": False, "source": "added", "key_move": None,
        "voice": "ara", "reply_host": "home",
        "webhook": {"configured": False},
        "status": {"state": "not_configured", "last_answered_at": 0, "last_error": "", "last_error_at": 0},
        "helper": {"version": "0.13.37", "release": "0.13.37", "state": "current", "out_of_date": False},
        "update": {"routine": "0.13.37", "helper": "0.13.37"},
        "feed_updates": {"on": False, "rule": ""},
        "stats": {"avg_rtt_ms": 400, "timeouts": 0}, "avatar_v": "1",
    }
    def one(**kw):
        out = dict(bot)
        out.update(kw)
        return out
    bots = [
        one(slug="sol", name="Sol", main=True, paired=True, public_key=pub,
            webhook={"configured": True, "last4": "ab12", "saved_at": 1700000000},
            status={"state": "connected", "last_answered_at": 1700000000, "last_error": "", "last_error_at": 0}),
        one(slug="juniper", name="Juniper", paired=True, public_key=pub,
            webhook={"configured": True, "last4": "cd34", "saved_at": 1700000000},
            status={"state": "connected", "last_answered_at": 1700000000, "last_error": "", "last_error_at": 0}),
        one(slug="kai", name="Kai", key_move="no-evidence"),
        one(slug="piper", name="Piper"),
    ]
    return {
        "state": {"signed_in": True, "host": "station", "version": "0.13.37",
                  "wizard": {"done": True}, "setup_required": False, "stepup_until": 0},
        "bots": {
            "bots": bots, "voices": ["ara"], "default_voice": "ara", "stepup_until": 0,
            "station_pub": pub, "killed": False, "stats": {"avg_rtt_ms": 400, "timeouts": 0},
            "list_source": "sol", "imported": {"rows": []},
            "fingerprint": "ab" * 32,
            "setup": {"owner": "", "hint": None, "rows": {
                "juniper": {"name": "Juniper", "status": "sent", "sent_at": 1760000000, "fails": 0,
                            "relay": "review", "relay_fp": "abcd1234abcd1234", "paired_computer": "home"},
                "kai": {"name": "Kai", "status": "not_set_up", "sent_at": 0, "fails": 0},
                "piper": {"name": "Piper", "status": "sent", "sent_at": 1760000000, "fails": 0, "relay": "accepted"},
            }},
        },
        "enroll": {"buttons": {}, "words": {}, "level_dbfs": None, "session": False,
                   "bots": {"sol": {"can_train": True, "online": True},
                            "juniper": {"can_train": True, "online": True}}},
        "wake": {"words": [{"name": "hey_sol", "persona": "sol", "has_model": True}], "unused": []},
    }
