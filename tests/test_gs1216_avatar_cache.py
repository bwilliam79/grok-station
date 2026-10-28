"""0.12.16: the Bots page avatars stop flickering. A portrait requested with its current ?v= is cacheable, and
the 3 s list repaint keeps each avatar <img> node instead of recreating it."""
import json, os, shutil, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def _fn(name):
    i = JS.index("function " + name + "(")
    return JS[i:JS.index("\n}\n", i) + 3]


class PortraitCacheHeaders(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, HERE)
        import test_admin
        self.ta, ga = test_admin, test_admin.ga
        base = os.path.dirname(ga.PERSONAS)
        os.makedirs(base, exist_ok=True)
        f = os.path.join(base, "zed.png")
        with open(f, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.addCleanup(lambda: os.path.exists(f) and os.unlink(f))
        self.c = test_admin.signed_in()
        self.v = ga.portrait_version("zed")

    def get(self, q):
        st, body = self.c.req("GET", "/api/portrait/zed" + q, raw=True)
        return st, self.c.last.getheader("Cache-Control"), body, self.c.last

    def test_current_version_is_cacheable(self):
        st, cc, body, r = self.get("?v=" + self.v)
        self.assertEqual(st, 200)
        self.assertEqual(cc, "private, max-age=86400")
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")  # other headers unchanged
        self.assertEqual(r.getheader("Content-Type"), "image/png")

    def test_no_version_or_stale_version_stays_no_store(self):
        for q in ("", "?v=0-0", "?v=" + self.v + "x", "?v=1735000000000"):
            st, cc, _b, _r = self.get(q)
            self.assertEqual((st, cc), (200, "no-store"), q)


class KeepAvatarNodes(unittest.TestCase):
    PROG = r"""
class El {
  constructor(tag, attrs) { this.tag = tag; this.attrs = Object.assign({}, attrs || {}); this.dataset = {};
    this.children = []; this.parentNode = null; this.id = El.n = (El.n || 0) + 1; }
  getAttribute(k) { return this.attrs[k]; }
  append(c) { c.parentNode = this; this.children.push(c); }
  replaceWith(n) { const p = this.parentNode, i = p.children.indexOf(this);
    if (n.parentNode) n.parentNode.children.splice(n.parentNode.children.indexOf(n), 1);
    p.children[i] = n; n.parentNode = p; this.parentNode = null; }
  all() { return this.children.flatMap(c => [c, ...c.all()]); }
  querySelectorAll(_sel) { return this.all().filter(e => e.tag === "img" && e.parentNode && e.parentNode.dataset.slug); }
}
// what list.innerHTML = rowsHTML builds: one .avwrap[data-slug] with a fresh <img class=av> per bot
function rows(list, srcs) { list.children = []; for (const [slug, src] of srcs) {
  const w = new El("span"); w.dataset.slug = slug; w.append(new El("img", {src})); list.append(w); } }
const list = new El("div");
const v1 = [["grok", "/assets/mark.svg"], ["rhea", "/api/portrait/rhea?v=a1"]];
keepAvatarImgs(list, () => rows(list, v1));
const first = list.querySelectorAll().map(i => i.id);
keepAvatarImgs(list, () => rows(list, v1));
const second = list.querySelectorAll().map(i => [i.id, i.getAttribute("src")]);
keepAvatarImgs(list, () => rows(list, [["grok", "/assets/mark.svg"], ["rhea", "/api/portrait/rhea?v=b2"]]));
const third = list.querySelectorAll().map(i => [i.id, i.getAttribute("src")]);
console.log(JSON.stringify({first, second, third}));
"""

    def test_two_repaints_keep_the_same_img_node(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        self.assertIn("keepAvatarImgs(list, () => { list.innerHTML = rowsHTML; });", _fn("paintBots"))
        out = subprocess.run(["node", "-e", _fn("keepAvatarImgs") + self.PROG], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        r = json.loads(out.stdout)
        self.assertEqual([i for i, _ in r["second"]], r["first"])  # same nodes after a repaint
        self.assertEqual([s for _, s in r["second"]], ["/assets/mark.svg", "/api/portrait/rhea?v=a1"])
        self.assertEqual(r["third"][0][0], r["first"][0])           # unchanged avatar kept
        self.assertNotEqual(r["third"][1][0], r["first"][1])         # a new version gets a new img
        self.assertEqual(r["third"][1][1], "/api/portrait/rhea?v=b2")


if __name__ == "__main__":
    unittest.main()


class PerPersonaPicturePath(unittest.TestCase):
    """0.12.16: the instructions name a per-persona picture file. Bots had grabbed a shared avatar.png that held
    another bot's picture."""

    def test_every_template_uses_the_persona_path(self):
        sys.path.insert(0, os.path.join(ROOT, "admin"))
        import gs_routine
        path = '"$HOME/.local/share/grok-station/avatar-<slug>.png"'
        texts = dict(gs_routine.TEMPLATES)
        for name in ("routine-cloud.txt", "routine-home.txt"):
            texts[name] = open(os.path.join(ROOT, "admin", name), encoding="utf-8").read()
        for i in ("BOT_ROUTINE", "BOT_ROUTINE_HOME"):
            start = JS.index("const " + i + " = `")
            texts["app.js " + i] = JS[start:JS.index("`;", start)]
        for name, t in texts.items():
            pic = t[t.index("Profile picture."):t.index("Station update.")]
            self.assertIn("Copy your own profile picture", pic, name)
            self.assertEqual(pic.count(path), 2, name)  # where to copy it, and the file the command reads
            self.assertIn("Never use a shared avatar.png", pic, name)
            self.assertNotIn("PICTURE_FILE", pic, name)
            self.assertNotIn("grok-station/avatar.png", pic, name)
