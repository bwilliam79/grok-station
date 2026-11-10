#!/usr/bin/env python3
"""0.13.34: a bot Update that gets no reply ends as "No reply" on the Bots page, even when the page can't re-read.

The station already turns a sent update into noreply FINISH_WAIT_S after the send. finish_rows computes it from the
saved send time on every read, so it holds after an admin restart (checked here in a fresh process). The page kept
its last "waiting" view while its re-reads failed (the station restarting, for example), so the row said
"Updating…" well past the bot's own 10-minute window. The page now also ends a waiting row on its own clock at the
server's wait_left, shows "Update failed: No reply within 10 minutes." and offers Update again.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
NAMES = ("finishComputers", "finishFind", "finishNeedsUpdate", "finishSendable", "finishAdopt", "finishExpire",
         "rowUpdating", "rowFailLine", "updateRowBtnHTML", "rowUpdatePoll")


def _fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}\n" % re.escape(name), JS, re.S)
    return m.group(0) if m else ""


def page_run(steps):
    """Run the real Bots-page functions under node with a fake clock and a fake station."""
    if not shutil.which("node"):
        raise unittest.SkipTest("node not installed")
    funcs = "".join(_fn(n) for n in NAMES) + _fn("refreshFinishChrome")
    const = re.search(r'\nconst FINISH_NOREPLY = .*?;\n', JS)
    prog = r"""
let NOW = 1700000000000; Date.now = () => NOW;
const timers = [];
global.setTimeout = (f, ms) => { const t = {at: NOW + ms, f}; timers.push(t); return t; };
global.clearTimeout = t => { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); };
async function advance(ms) {
  const end = NOW + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    if (!timers.length || timers[0].at > end) break;
    const t = timers.shift(); NOW = t.at; await t.f();
  }
  NOW = end;
}
const esc = s => String(s);
const ROW_SENDING = {};
const FINISH = {view: null};
let ANSWERS = [];
async function api(method, path) { const a = ANSWERS.length > 1 ? ANSWERS.shift() : ANSWERS[0]; return JSON.parse(JSON.stringify(a)); }
function paintFinishButtons() {}
const pg = {_bots: [{slug: "alpha"}], _holdPaint: false};
const document = {getElementById: id => id === "pg" ? pg : id === "blist" ? {} : null};
let PAINT = null;
function paintBots(p, bots) { const b = bots[0]; PAINT = {btn: updateRowBtnHTML(b), fail: rowFailLine(b)}; rowUpdatePoll(); }
""" + (const.group(0) if const else "") + funcs + r"""
const waiting = wl => ({ok: true, status: 200, data: {computers: [{bots: [
  {slug: "alpha", name: "Alpha", state: "waiting", wait_left: wl, instr: "a".repeat(16), fail: ""}]}]}});
const down = {ok: false, status: 0, data: {error: "The station isn't answering."}};
const noreply = {ok: true, status: 200, data: {computers: [{bots: [
  {slug: "alpha", name: "Alpha", state: "noreply", wait_left: 0, instr: "a".repeat(16), fail: "No reply within 10 minutes."}]}]}};
const row = () => finishFind(FINISH.view, "alpha");
const snap = () => ({btn: PAINT && PAINT.btn, fail: PAINT && PAINT.fail, updating: rowUpdating({slug: "alpha"}),
                     sendable: finishSendable(row()), state: row() && row().state});
(async () => {
  const out = {};
  STEPS
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    prog = prog.replace("STEPS", steps)
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(r.stderr[-1500:])
    return json.loads(r.stdout)


class PageTimeout(unittest.TestCase):
    def test_station_down_row_ends_at_wait_left(self):
        """Last good read said waiting, 600 s left. Every later read fails. At 600 s the row is no longer Updating."""
        got = page_run(r"""
  ANSWERS = [waiting(600), down];
  await refreshFinishChrome(); rowUpdatePoll();
  out.start = snap();
  await advance(300 * 1000);
  out.mid = snap();
  await advance(301 * 1000);
  out.end = snap();
  out.timers = timers.length;
""")
        self.assertIn("Updating…", got["start"]["btn"])
        self.assertIn("Updating…", got["mid"]["btn"])
        self.assertFalse(got["mid"]["sendable"])
        end = got["end"]
        self.assertNotIn("Updating…", end["btn"] or "", end)
        self.assertIn('data-upd="alpha"', end["btn"] or "", end)
        self.assertEqual(end["fail"], "Update failed: No reply within 10 minutes.")
        self.assertFalse(end["updating"])
        self.assertTrue(end["sendable"])
        self.assertEqual(end["state"], "noreply")
        self.assertEqual(got["timers"], 0)  # nothing left polling once no row is waiting

    def test_server_noreply_and_a_fresh_wait_still_work(self):
        got = page_run(r"""
  ANSWERS = [waiting(30), noreply];
  await refreshFinishChrome(); rowUpdatePoll();
  await advance(16 * 1000);
  out.server = snap();
  ANSWERS = [waiting(600)];
  await refreshFinishChrome();
  out.again = snap();
""")
        self.assertEqual(got["server"]["fail"], "Update failed: No reply within 10 minutes.")
        self.assertIn('data-upd="alpha"', got["server"]["btn"])
        self.assertIn("Updating…", got["again"]["btn"])  # a new send waits again from the server's wait_left
        self.assertFalse(got["again"]["sendable"])


class ServerTimeout(unittest.TestCase):
    def test_window_matches_the_bot(self):
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import botreply
        src = open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read()
        wait = int(re.search(r"(?m)^FINISH_WAIT_S = (\d+)", src).group(1))
        self.assertLessEqual(wait, botreply.UPDATE_REPLY_BY_S)
        self.assertEqual(wait, 600)

    def test_expiry_is_computed_on_read_after_a_restart(self):
        """A fresh admin process reads finish-update.json and answers noreply for a send older than the window."""
        d = tempfile.mkdtemp(prefix="gs1334-")
        try:
            state, run = os.path.join(d, "state"), os.path.join(d, "run")
            os.makedirs(state); os.makedirs(run)
            code = r"""
import json, os, sys, time
sys.path.insert(0, %r)
import grok_admin as ga
bot = {"slug": "alpha", "name": "Alpha", "paired": True, "webhook": {"configured": True}, "key_mode": "cloud",
       "update": {"release": "0.0.1", "instr": "1" * 16}}
import hashlib
pins = {"commit": "d" * 40, "update_commit": "c" * 40, "files": {"scripts/gs-update.sh": hashlib.sha256(
    open(os.path.join(%r, "scripts", "gs-update.sh"), "rb").read()).hexdigest()}}
want = ga.finish_rows([bot], "https://st.example", ":".join(["AB"] * 32), "A" * 43 + "=", pins, {"sent": {}, "copied": {},
                      "updaters": {}, "setup": {}, "failed": {}})["computers"][0]["bots"][0]["instr"]
out = {}
for label, age in (("fresh", 30), ("old", ga.FINISH_WAIT_S + 1)):
    ga.write_json(ga.path(ga.FINISH), {"sent": {"alpha": {"instr": want, "release": ga.version(), "at": int(time.time()) - age}}})
    row = ga.finish_rows([bot], "https://st.example", ":".join(["AB"] * 32), "A" * 43 + "=", pins)["computers"][0]["bots"][0]
    out[label] = [row["state"], row["fail"], row["wait_left"]]
print(json.dumps(out))
""" % (os.path.join(ROOT, "admin"), ROOT)
            env = dict(os.environ)  # a child process only; this run keeps test_admin's sandbox
            env.update({"ADMIN_STATE_DIR": state, "ADMIN_RUN_DIR": run, "ADMIN_HELPER": "/bin/false",
                        "ADMIN_VERSION_FILE": os.path.join(ROOT, "VERSION")})
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
            out = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertEqual(out["fresh"][0], "waiting")
            self.assertGreater(out["fresh"][2], 500)
            self.assertEqual(out["old"], ["noreply", "No reply within 10 minutes.", 0])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class RowLayout(unittest.TestCase):
    """0.13.34 review: a failure line wraps (no ellipsis), other status lines keep one line; Update sits in its own
    column at 768 and up; on a phone Update is 44 px tall with 8 px above Configure. Frames and measured values:
    /workspace/ash/gs1334/frames/measurements.txt (Playwright, not part of this suite)."""

    def test_only_failure_lines_get_the_modifier(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        prog = "const esc = s => String(s);" + _fn("botMessageLineHTML") + _fn("botRowL2HTML") + r"""
console.log(JSON.stringify({fail: botRowL2HTML("Replies from: X", "", "Update failed: No reply within 10 minutes.", false, "", "a", true),
  note: botRowL2HTML("Replies from: X", "", "Update available for this bot", false, "", "a", false),
  none: botRowL2HTML("Replies from: X", "", "", false, "", "a", true)}));"""
        r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertIn('class="bmsg is-fail"', got["fail"])
        self.assertIn('class="bmsg"', got["note"])
        self.assertNotIn("bmsg", got["none"])
        paint = JS[JS.index("const noteFail"):]
        self.assertIn("const noteFail = !!note && note === rowFailLine(b);", paint[:200])
        self.assertIn("b.slug, noteFail)}", paint[:3000])

    def test_css(self):
        css = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
        flat = re.sub(r"\s+", "", css)
        self.assertIn(".bmsg.tx{min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}", flat)
        self.assertIn(".bmsg.is-fail.tx{white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}", flat)
        self.assertIn(".botrow.l2:has(>.bmsg.is-fail){height:auto}", flat)
        self.assertIn("#blist.botrow:has(>.rowupd){grid-template-columns:[bot]minmax(216px,1fr)[ru]auto[wake]200px[upd]144px[act]120px}", flat)
        self.assertIn("#blist.botrow>.rowupd{grid-column:ru;justify-self:start}", flat)
        phone = flat.split("@media(max-width:767px){#blist.botrow{display:grid;grid-template-columns:44pxminmax(0,1fr)auto;grid-template-areas:\"avnmnm\"\"avl2l2\"", 1)[1]
        self.assertIn("#blist.botrow>.rowupd{grid-area:ru;margin-bottom:4px}", phone)
        self.assertIn("#blist.botrow>.rowupd.btn{min-height:44px;height:44px}", phone)


if __name__ == "__main__":
    unittest.main()
