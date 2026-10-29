"""0.13.3 Bots page: muted dash until the setup test passed, then Train, disabled when offline.

The dialog closes back to the row. It does not open Configure. Copy matches the training frames.
"""
import json, os, shutil, subprocess, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "admin", "web", "app.js")
CSS = os.path.join(ROOT, "admin", "web", "admin.css")
OFFLINE = "Offline: can\u2019t reach the bot"


def _fn(src, name):
    i = src.index("function " + name + "(")
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError("unclosed " + name)


class TrainCell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = open(JS).read()
        cls.css = open(CSS).read()

    def test_paint_does_not_inline_the_offline_sentence(self):
        paint = _fn(self.js, "paintBots")
        self.assertNotIn("Offline: ", paint)
        self.assertNotIn("${check", paint)
        self.assertIn("trainOfflineTip()", paint)
        self.assertIn("trainOfflineSub()", paint)
        self.assertIn('class="ww none"></div>', paint)
        self.assertEqual(_fn(self.js, "trainOfflineTip") .count(OFFLINE), 0)
        self.assertIn("TRAIN_OFFLINE", _fn(self.js, "trainOfflineTip"))
        self.assertIn(OFFLINE, self.js.split("function trainOfflineTip", 1)[0])

    def test_dash_until_the_bot_can_train(self):
        wake = _fn(self.js, "wakeControlHTML")
        self.assertIn('aria-label="No wake word">—</span>', wake)
        self.assertIn('class="ww none"', wake)
        cell = _fn(self.js, "trainCellHTML")
        self.assertIn('aria-label="Train a wake word for ${esc(name)}"', cell)
        self.assertIn('data-train="${esc(slug)}"', cell)
        self.assertIn('aria-disabled="true"', cell)
        self.assertIn('tabindex="-1"', cell)
        self.assertIn("M12 5v14M5 12h14", self.js)
        self.assertIn("M6.5 17.5l11-11", self.js)
        self.assertNotIn("data-train", cell.split("if (!on)", 1)[1].split("return", 1)[0] + cell.split("if (!on)", 1)[1].split("return `", 1)[1].split("`;", 1)[0])

    def test_dialog_copy_and_it_does_not_open_the_bot(self):
        fn = _fn(self.js, "openSetWake")
        self.assertNotIn("openBot(", fn)
        self.assertIn("You’ll say it 5 times", fn)
        self.assertIn("Hey Grok, train my wake word for ", fn)
        self.assertIn("station’s microphone", fn)
        self.assertIn(">Cancel<", fn)
        self.assertIn(">Done<", fn)
        self.assertIn(">Not now<", fn)
        self.assertIn('api("POST", "/api/enroll/open-tile"', fn)
        self.assertIn("{op: \"discard\"}", fn)
        self.assertNotIn("password", fn.split("leavePrompt", 1)[1].split("const showIdle", 1)[0])
        begin = fn.split("const begin = async topup", 1)[1].split("const poll =", 1)[0]
        paint = begin.index('paintRow({state: "recording"')
        post = begin.index('api("POST", "/api/enroll"')
        self.assertLess(paint, post)
        self.assertIn("if (topup)", begin[:paint])
        self.assertIn("r.status === 403", begin[post:])
        self.assertEqual(fn.count(">Close<"), 1)
        self.assertIn(">Close<", fn.split('id="wwtrain"', 1)[1].split('phase = "recording"', 1)[0])
        self.assertIn("i <= 5", self.js)

    def test_phone_keeps_the_pending_cell_hidden(self):
        css = self.css.replace(" ", "")
        self.assertIn(".ww.none{display:none}", css)
        self.assertIn(".botrow .ww.none:has(.dash){display:flex!important}".replace(" ", ""), css)
        self.assertIn(".wwtrain[aria-disabled=\"true\"]{opacity:.45;cursor:not-allowed;filter:grayscale(1)}".replace(" ", ""), css)
        self.assertIn(".offsub{display:none}", css)
        self.assertIn(".botrow .help:has(+.offsub){display:none}".replace(" ", ""), css)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_cell_states_in_node(self):
        src = self.js
        decl = next(ln for ln in src.splitlines() if ln.startswith("const TRAIN_OFFLINE"))
        prog = "\n".join([
            "const BOT_CHECKS = {};",
            "const esc = s => String(s);",
            decl,
            _fn(src, "trainOfflineTip"),
            _fn(src, "trainPlusSVG"),
            _fn(src, "trainBanSVG"),
            _fn(src, "trainCellHTML"),
            _fn(src, "botCanTrain"),
            _fn(src, "wordLabel") if "function wordLabel" in src else "function wordLabel(w){return w.name}",
            r"""
const otto = {slug: "otto", name: "Otto"};
const flags = {otto: {can_train: true, online: true}};
const off = {otto: {can_train: true, online: false}};
const dash = botCanTrain(otto, {});
const on = botCanTrain(otto, flags);
const down = botCanTrain(otto, off);
BOT_CHECKS.otto = {state: "online"};
const just = botCanTrain(otto, {});
delete BOT_CHECKS.otto;
BOT_CHECKS.otto = {state: "offline"};
const livedown = botCanTrain(otto, flags);
const htmlOn = trainCellHTML(otto, true);
const htmlOff = trainCellHTML(otto, false);
if (dash.can || dash.on) throw new Error("missing flags enabled");
if (!on.can || !on.on) throw new Error("online flag");
if (!down.can || down.on) throw new Error("offline flag");
if (!just.can || !just.on) throw new Error("live online");
if (!livedown.can || livedown.on) throw new Error("live offline");
if (htmlOn.indexOf('data-train="otto"') < 0) throw new Error("train attr");
if (htmlOn.indexOf("aria-disabled") >= 0) throw new Error("enabled disabled");
if (htmlOff.indexOf('aria-disabled="true"') < 0) throw new Error("no aria");
if (htmlOff.indexOf("data-train") >= 0) throw new Error("disabled posts");
if (htmlOff.indexOf("avtip-otto") < 0) throw new Error("tip id");
if (trainOfflineTip() !== "Offline: can\u2019t reach the bot") throw new Error("tip text");
console.log("ok");
"""])
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ok", out.stdout)


if __name__ == "__main__":
    unittest.main()
