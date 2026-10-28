"""0.12.6: a new avatar version changes the img src, so the Bots page shows it without a reload."""
import json, os, shutil, subprocess, sys, tempfile, time, unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def _fn(name):
    i = JS.index("function " + name + "(")
    return JS[i:JS.index("\n}\n", i) + 3]


class AvatarSrc(unittest.TestCase):
    def test_new_version_changes_src(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        prog = """
const S = {avatarV: 0};
const imgs = [{dataset: {}, src: "old"}];
const document = {hidden: false, querySelectorAll: () => [{querySelector: () => imgs[0], prepend(){}}], createElement(){ return {dataset:{}}; }};
function avatarOnError(){}
const esc = s => String(s);
""" + _fn("avatarSrc") + _fn("noteAvatarVersions") + """
noteAvatarVersions([{slug: "rhea", avatar_v: "a1"}]);
const first = avatarSrc("rhea"), afterFirst = imgs[0].src;
noteAvatarVersions([{slug: "rhea", avatar_v: "b2"}]);
console.log(JSON.stringify({first, afterFirst, second: avatarSrc("rhea"), img: imgs[0].src}));
"""
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        r = json.loads(out.stdout)
        self.assertEqual(r["first"], "/api/portrait/rhea?v=a1")
        self.assertEqual(r["afterFirst"], "old")  # first sight: nothing to swap
        self.assertEqual(r["second"], "/api/portrait/rhea?v=b2")
        self.assertEqual(r["img"], "/api/portrait/rhea?v=b2")  # swapped in place, no reload

    def test_bots_page_polls(self):
        self.assertIn("noteAvatarVersions(a.data.bots)", JS)


class ServerVersion(unittest.TestCase):
    def test_portrait_version_changes_with_the_file(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import test_admin
        ga = test_admin.ga
        base = os.path.dirname(ga.PERSONAS)
        os.makedirs(base, exist_ok=True)
        f = os.path.join(base, "zed.png")
        self.addCleanup(lambda: os.path.exists(f) and os.unlink(f))
        self.assertEqual(ga.portrait_version("zed"), "")
        with open(f, "wb") as fh:
            fh.write(b"one")
        v1 = ga.portrait_version("zed")
        os.utime(f, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        self.assertNotEqual(ga.portrait_version("zed"), v1)
        self.assertEqual(ga.portrait_version("../x"), "")


if __name__ == "__main__":
    unittest.main()


class ComputerName(unittest.TestCase):
    def test_name_is_editable_and_kept_as_typed(self):
        self.assertIn('id="chname" maxlength="40" autocomplete="off" spellcheck="false" value="${esc(host)}"', JS)
        self.assertIn("Save name", JS)
        self.assertIn("exactly as it shows in the Grok Bot app", JS)
        self.assertNotIn("shortComputerName", JS)
