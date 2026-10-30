#!/usr/bin/env python3
"""0.13.9 (Noor at c5811bd): bot avatars used an inline onerror="avatarOnError(this)" attribute, which the admin
CSP blocks (console errors, and the fallback never ran when the handler was blocked).

1. No rendered markup in the admin page carries an inline on<event>= attribute.
2. In headless Chrome under a CSP that forbids inline handlers (nonce scripts only), a broken portrait swaps to the
   default avatar once, and when that also fails the <img> is removed so the letter shows. Same fallback as before.
Fails on c5811bd.
"""
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = base.JS
CHROME = shutil.which("google-chrome") or shutil.which("chromium")
INLINE = re.compile(r"<[a-zA-Z][^<>]*?\son[a-z]+\s*=", re.S)


def _markup_files():
    out = []
    for d in (os.path.join(ROOT, "admin", "web"), os.path.join(ROOT, "admin")):
        for n in sorted(os.listdir(d)):
            if n.endswith((".js", ".html", ".py")):
                out.append(os.path.join(d, n))
    return out


class NoInlineHandlers(unittest.TestCase):
    def test_no_on_event_attributes_in_markup(self):
        hits = []
        for f in _markup_files():
            text = open(f, encoding="utf-8").read()
            for m in INLINE.finditer(text):
                hits.append("%s:%d" % (os.path.relpath(f, ROOT), text.count("\n", 0, m.start()) + 1))
        self.assertEqual(hits, [])


PAGE = """<!doctype html><html><head>
<meta http-equiv="Content-Security-Policy" content="default-src 'self' data:; img-src 'self' data:; script-src 'nonce-t'">
</head><body><div id="a"></div><div id="b"></div><pre id="out"></pre>
<script nonce="t">
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
let S = {avatarV: 0, avatarVs: {}};
let FB = "missing-default.svg";
function avatarSrc(slug) { return "missing-" + slug + ".png"; }
function defaultAvatarSrc(slug) { return FB; }
%s
%s
document.getElementById('a').innerHTML = avatar('otto', 'Otto', false, null);
FB = "data:image/svg+xml,%%3Csvg xmlns='http://www.w3.org/2000/svg' width='4' height='4'/%%3E";
document.getElementById('b').innerHTML = avatar('pip', 'Pip', false, null);
setTimeout(() => {
  const a = document.querySelector('#a img'), b = document.querySelector('#b img');
  document.getElementById('out').textContent = JSON.stringify({
    aGone: !a, aLetter: document.querySelector('#a .monoav').textContent,
    bSrc: b ? b.getAttribute('src') : null, bFb: b ? b.dataset.fb : null,
    inline: /\son[a-z]+=/.test(document.getElementById('a').innerHTML + document.getElementById('b').innerHTML)});
}, 1500);
</script></body></html>"""


def _run_page():
    names = ["avatarOnError", "avatar"]
    src = "\n".join(base._fn(n) for n in names)
    install = ""
    if "\nfunction installAvatarFallback(" in JS:
        install = base._fn("onAvatarError") + base._fn("installAvatarFallback") + "\ninstallAvatarFallback(document);"
    d = tempfile.mkdtemp()
    page = os.path.join(d, "p.html")
    with open(page, "w") as fh:
        fh.write(PAGE % (src, install))
    out = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--allow-file-access-from-files",
                          "--virtual-time-budget=4000", "--dump-dom", "file://" + page],
                         capture_output=True, text=True, timeout=120).stdout
    shutil.rmtree(d, ignore_errors=True)
    m = re.search(r'<pre id="out">(.*?)</pre>', out, re.S)
    assert m and m.group(1).strip(), "no result from Chrome"
    return json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))


@unittest.skipUnless(CHROME, "Chrome not installed")
class FallbackUnderCSP(unittest.TestCase):
    def test_fallback_runs_without_inline_handler(self):
        r = _run_page()
        self.assertFalse(r["inline"], r)
        self.assertTrue(r["aGone"], r)  # portrait and default both failed: the letter shows
        self.assertEqual(r["aLetter"], "O")
        self.assertTrue(r["bSrc"].startswith("data:image/svg+xml"), r)  # swapped to the default once
        self.assertEqual(r["bFb"], "1", r)

    def test_listener_is_installed_once_at_startup(self):
        self.assertIn('doc.addEventListener("error", onAvatarError, true)', base._fn("installAvatarFallback"))
        self.assertEqual(JS.count("installAvatarFallback(document)"), 1)
        self.assertNotIn("img.onerror", JS)  # one path only, so a failure is never handled twice


if __name__ == "__main__":
    unittest.main()
