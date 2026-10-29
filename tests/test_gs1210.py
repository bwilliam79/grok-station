"""0.12.10: bots are behind only when what they run changed; no Finish update button; Bots menu dot; one
post-update notice; team tip invites an import until there are teammates; 0.12.10 sorts after 0.12.9.

Unit, API-level and node checks only. No browser. Names and keys are made up.
"""
import hashlib, importlib.util, json, os, re, shutil, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_admin as ta  # noqa: E402  (sets the admin env, then imports grok_admin)

ga = ta.ga
gs_routine = ga.gs_routine
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
C_OLD, C_NEW = "1" * 40, "2" * 40
S_OLD, S_NEW = "a" * 64, "b" * 64
REL = re.search(r"release (\d+\.\d+\.\d+)\.", open(os.path.join(ROOT, "admin", "routine-home.txt")).readline()).group(1)


class _Rel(unittest.TestCase):
    """The test env runs the admin as version 'dev'; these use this tree's release, as an installed station does."""

    def setUp(self):
        self._ver = ga.version
        ga.version = lambda: REL

    def tearDown(self):
        ga.version = self._ver


def _fn(name):
    i = JS.index("function " + name + "(")
    j = JS.index("\n}\n", i) + 3
    return JS[i:j]


def _node(prog):
    if not shutil.which("node"):
        raise unittest.SkipTest("node not installed")
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


class NoFalseBehind(_Rel):
    """(a) A bot on the previous release whose instructions differ only in the release line is not behind."""

    def setUp(self):
        super().setUp()
        self.rel = ga.version()
        maj, mi, pa = (int(x) for x in self.rel.split("."))
        self.prev = "%d.%d.%d" % ((maj, mi, pa - 1) if pa else (maj, mi - 1, 99))  # x.y.0: any older release
        self.pins = {"commit": C_NEW, "update_commit": "3" * 40, "files": {"scripts/gs-update.sh": S_NEW}}
        self._saved = getattr(ga, "bot_content", None)

    def tearDown(self):
        super().tearDown()
        if self._saved is not None and hasattr(self, "_orig"):
            self._saved.read_table = self._orig

    def _table(self, same=True):
        fp = "c" * 64
        rows = [{"release": self.prev, "update_commit": C_OLD, "update_sha": S_OLD,
                 "fp": {"home": fp if same else "d" * 64, "cloud": fp if same else "d" * 64}},
                {"release": self.rel, "update_commit": C_NEW, "update_sha": S_NEW, "fp": {"home": fp, "cloud": fp}}]
        if self._saved is not None:
            self._orig = self._saved.read_table
            self._saved.read_table = lambda path=None: rows

    def _old_instr(self, slug, changed=False):
        t = gs_routine.TEMPLATES["home"]
        first, rest = t.split("\n", 1)
        if changed:  # a real wording change in the template the bot adopted
            rest = rest.replace("in 400 characters or fewer", "in 300 characters or fewer")
            tmpl = "Grok Station instructions, release %s.\n" % self.prev + rest
            text = gs_routine.render(tmpl, "home", slug, C_OLD, S_OLD)
        else:
            text = gs_routine.render("Grok Station instructions, release %s.\n" % self.prev + rest, "home", slug, C_OLD, S_OLD)
        return gs_routine.instr_hash(text)

    def _state(self, instr, setup=None, helper_behind=False):
        bot = {"slug": "kai", "name": "Kai", "paired": True, "webhook": {"configured": True}, "key_mode": "home",
               "reply_host": "box.example", "helper": {"version": "0.11.8", "out_of_date": helper_behind},
               "update": {"release": self.prev, "instr": instr, "at": 100} if instr else {}}
        fin = {"sent": {}, "copied": {}, "updaters": {}, "setup": setup or {}, "failed": {}}
        view = ga.finish_rows([bot], "https://st.example", "", "", pins=self.pins, finish=fin, now=200)
        return view["computers"][0]["bots"][0]["state"], view["behind"]

    def test_release_line_only_is_up_to_date(self):
        self._table(True)
        self.assertEqual(self._state(self._old_instr("kai")), ("done", 0))

    def test_paired_on_previous_release_with_same_content(self):
        self._table(True)
        self.assertEqual(self._state(None, setup={"kai": {"release": self.prev, "at": 50}}), ("done", 0))

    def test_template_change_is_behind(self):
        self._table(True)
        self.assertEqual(self._state(self._old_instr("kai", changed=True)), ("needs", 1))

    def test_helper_or_script_change_is_behind(self):
        self._table(False)  # the previous release's fingerprint differs: its helper or update script changed
        self.assertEqual(self._state(self._old_instr("kai")), ("needs", 1))
        self.assertEqual(self._state(None, setup={"kai": {"release": self.prev, "at": 50}}), ("needs", 1))

    def test_helper_behind_still_needs(self):
        self._table(True)
        self.assertEqual(self._state(self._old_instr("kai"), helper_behind=True), ("needs", 1))

    def test_shipped_table_matches_this_tree(self):
        """bot-releases.txt (written by tools/gen-update-manifest.py) names this release with this tree's content."""
        import bot_content
        man = open(os.path.join(ROOT, "scripts", "update-manifest.txt")).read()
        rel = re.search(r"^release (\S+)$", man, re.M).group(1)
        upd = re.search(r"^update ([0-9a-f]{40}) ([0-9a-f]{64})$", man, re.M)
        rows = [r for r in bot_content.read_table() if r["release"] == rel]
        self.assertTrue(rows, "bot-releases.txt has no line for the manifest's release")
        self.assertEqual((rows[-1]["update_commit"], rows[-1]["update_sha"]), upd.groups())
        script = subprocess.run(["git", "-C", ROOT, "show", upd.group(1) + ":scripts/gs-update.sh"],
                                capture_output=True).stdout
        for m in ("home", "cloud"):
            want = bot_content.content_fp(open(os.path.join(ROOT, "admin", "routine-%s.txt" % m), "rb").read(), script,
                                          open(os.path.join(ROOT, bot_content.HELPER[m]), "rb").read())
            self.assertEqual(rows[-1]["fp"][m], want, m)

    def test_normalize_touches_only_the_header(self):
        import bot_content
        a = b"x\nGrok Station instructions, release 0.12.8.\ny release 0.12.8 z\n"
        self.assertEqual(bot_content.normalize(a), b"x\nGrok Station instructions, release X.Y.Z.\ny release 0.12.8 z\n")


class NoFinishButton(unittest.TestCase):
    """(b) The Updates page renders no Finish update button, and the header has no Finish update slot."""

    def test_updates_page_has_no_finish_button(self):
        i = JS.index("async function paintUpdates(el)")
        page = JS[i:JS.index("\n}\n", i)]
        self.assertNotIn("finpage", page)
        self.assertNotIn("finpage", JS)
        self.assertNotIn("finhead", JS)
        self.assertNotIn("finishBtnHTML", JS)
        paint = _fn("paintFinishButtons")
        self.assertNotIn("Finish update", paint)
        self.assertNotIn("openFinishDialog", paint)

    def test_dialog_only_from_bots_rows_for_paste_bots(self):
        calls = [m.start() for m in re.finditer(r"openFinishDialog\(", JS)]
        defs = JS.index("async function openFinishDialog(")
        uses = [c for c in calls if c != defs + len("async function ")]
        self.assertEqual(len(uses), 2)
        for c in uses:
            self.assertIn('"copy"', JS[c - 200:c])


NODE_DOM = r"""
const nodes = {};
function mk(id) { return nodes[id] = nodes[id] || {id, hidden: true, innerHTML: "", textContent: "", attrs: {},
  setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; }, onclick: null}; }
const badges = [mk("botbadge"), mk("botbadge-more")];
const posted = [];
const document = {
  getElementById: id => (id in nodes || id === "botsnote" || id === "botsnoticego" || id === "botsnoticex") ? mk(id) : null,
  querySelectorAll: sel => sel === "[data-botbadge]" ? badges : [],
};
const api = (m, p, b) => { posted.push([m, p]); return Promise.resolve({ok: true}); };
const FINISH = {view: null};
"""


def _ui(view, listed=None):
    fns = "".join(_fn(n) for n in ("finishListedSlugs", "finishBehind", "finishComputers", "paintBotDot",
                                   "updNoticeHTML", "paintUpdNotice", "paintFinishButtons"))
    setup = ""
    if listed is not None:
        setup = 'nodes.pg = {_bots: %s}; nodes.blist = {};' % json.dumps([{"slug": s} for s in listed])
    return _node(NODE_DOM + fns + setup + """
FINISH.view = %s;
paintFinishButtons();
const n = nodes.botsnote || {hidden: true, innerHTML: ""};
console.log(JSON.stringify({badge: badges.map(b => [b.hidden, b.attrs.role || null, b.attrs["aria-label"] || null]),
  notice: !n.hidden && n.innerHTML.includes("Some bots need an update.") && n.innerHTML.includes('href="#bots"'),
  html: n.innerHTML}));
""" % json.dumps(view))


def _view(states, notice=True):
    bots = [{"slug": s, "state": st} for s, st in states]
    return {"release": "9.9.9", "behind": sum(1 for _, st in states if st != "done"), "notice": notice,
            "computers": [{"key": "bot", "bots": bots}]}


class BotsMenuDot(unittest.TestCase):
    """(c) The Bots menu dot shows only while a listed bot needs an update, announced like the 0.12.3 dot."""

    def test_markup_reuses_update_dot(self):
        self.assertIn('${k === "bots" ? `<b class="b updot" id="botbadge" data-botbadge hidden aria-hidden="true"></b>` : ""}', JS)

    def test_dot_only_when_needed(self):
        r = _ui(_view([("kai", "needs"), ("rhea", "done")]))
        self.assertEqual(r["badge"][0], [False, "img", "Bot update available"])
        r = _ui(_view([("kai", "done"), ("rhea", "done")]))
        self.assertEqual(r["badge"][0], [True, None, None])

    def test_deleted_bot_not_counted(self):
        r = _ui(_view([("gone", "needs"), ("rhea", "done")]), listed=["rhea"])
        self.assertEqual(r["badge"][0], [True, None, None])


class PostUpdateNotice(_Rel):
    """(d) One dismissible 'Some bots need an update.' notice per station release, only when a bot needs one."""

    def test_ui_shows_only_when_needed(self):
        self.assertTrue(_ui(_view([("kai", "needs")]))["notice"])
        self.assertFalse(_ui(_view([("kai", "done")]))["notice"])
        self.assertFalse(_ui(_view([("kai", "needs")], notice=False))["notice"])

    def _rows(self, state_done, fin):
        bot = {"slug": "kai", "name": "Kai", "paired": True, "webhook": {"configured": True}, "key_mode": "cloud"}
        if state_done:
            fin["setup"] = {"kai": {"release": ga.version(), "at": 1}}
        else:
            fin["setup"] = {"kai": {"release": "0.0.1", "at": 1}}
        return ga.finish_rows([bot], "https://st.example", ":".join(["AB"] * 32), "A" * 43 + "=",
                              pins={"commit": "4" * 40, "update_commit": "5" * 40,
                                    "files": {"scripts/gs-update.sh": "6" * 64}}, finish=fin, now=1)

    def test_server_once_per_release(self):
        fin = {"sent": {}, "copied": {}, "updaters": {}, "failed": {}}
        self.assertIs(self._rows(False, dict(fin)).get("notice"), True)
        self.assertIs(self._rows(True, dict(fin)).get("notice"), False)
        dismissed = dict(fin, notice_dismissed=ga.version())
        self.assertIs(self._rows(False, dismissed).get("notice"), False)
        older = dict(fin, notice_dismissed="0.0.1")  # dismissed for an earlier station update: show again
        self.assertIs(self._rows(False, older).get("notice"), True)

    def test_dismiss_route_stores_release(self):
        src = open(os.path.join(ROOT, "admin", "grok_admin.py")).read()
        i = src.index('if p == "/api/finish-update/notice":')
        self.assertIn('doc["notice_dismissed"] = version()', src[i:i + 300])
        self.assertIn('api("POST", "/api/finish-update/notice"', _fn("paintUpdNotice"))


class TeamTip(unittest.TestCase):
    """0.12.10: only Grok and the main bot means invite the import, not 'update my team list'."""

    def _html(self, bots, imported):
        phrase = re.search(r'const TEAM_PHRASE = "([^"]+)";', JS).group(1)
        upd = re.search(r'const TEAM_UPDATE_PHRASE = "([^"]+)";', JS).group(1)
        return _node("const TEAM_PHRASE = %s; const TEAM_UPDATE_PHRASE = %s; const esc = s => String(s);"
                     % (json.dumps(phrase), json.dumps(upd)) + _fn("teamState") + _fn("teamPhraseHtml")
                     + "console.log(JSON.stringify(teamPhraseHtml(teamState({_bots: %s, _imported: %s}))));"
                     % (json.dumps(bots), json.dumps(imported)))

    def test_only_main_bot_invites_import(self):
        want = 'Say <span class="saychip">“Hey Grok, import the rest of my team.”</span> to add your other bots.'
        self.assertEqual(self._html([{"slug": "rhea", "main": True}], {"rows": [], "updated_at": 1710000000}), want)
        self.assertEqual(self._html([{"slug": "rhea", "main": True}], {"rows": []}), want)

    def test_teammates_keep_update_phrase(self):
        want = 'Added a bot? Say <span class="saychip">“Hey Grok, update my team list.”</span>'
        self.assertEqual(self._html([{"slug": "rhea", "main": True}, {"slug": "kai"}], {"rows": [], "updated_at": 1}), want)
        self.assertEqual(self._html([{"slug": "rhea", "main": True}], {"rows": [{"name": "Kai"}]}), want)

    def test_phrase_is_a_station_phrase(self):
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import relay
        self.assertTrue(relay.import_team_request("Hey Grok, import the rest of my team."))


class VersionOrder(unittest.TestCase):
    """0.12.10 is newer than 0.12.9 everywhere versions are compared (numbers, not strings)."""

    def test_patch_ten_after_nine(self):
        spec = importlib.util.spec_from_file_location("cv", os.path.join(ROOT, "tools", "check-version.py"))
        cv = importlib.util.module_from_spec(spec); spec.loader.exec_module(cv)
        self.assertEqual(cv.version_order("0.12.9", "0.12.10"), "newer")
        self.assertEqual(cv.version_order("0.12.10", "0.12.9"), "older")
        self.assertEqual(ga.version_order("0.12.9", "0.12.10"), "newer")
        sh = subprocess.run(["sh", os.path.join(ROOT, "tools", "semver-order.sh"), "0.12.9", "0.12.10"],
                            capture_output=True, text=True)
        self.assertEqual(sh.stdout.strip(), "newer")
        # gs-update.sh's no-downgrade check (B) compares integer tuples
        upd = open(os.path.join(ROOT, "scripts", "gs-update.sh")).read()
        self.assertIn('v = lambda s: tuple(int(x) for x in s.split("."))', upd)
        v = lambda s: tuple(int(x) for x in s.split("."))
        self.assertGreater(v("0.12.10"), v("0.12.9"))


if __name__ == "__main__":
    unittest.main()
