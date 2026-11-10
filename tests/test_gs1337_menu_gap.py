#!/usr/bin/env python3
"""0.13.37 regression: no gap between the Bots menu divider and "Revoke all bots...".

The menu's Revoke button has class "danger", and the Settings danger-zone rule `.danger{margin-top:24px}` (16px on a
phone) applied to it, so the divider sat 24px above it instead of the menu's normal 6px. Real app.js and admin.css
in headless Chrome, at 1280 and 390, idle and Rescanning.
"""
import html, json, os, re, shutil, subprocess, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = shutil.which("google-chrome") or shutil.which("chromium")


def measure(width, busy):
    js = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
    css = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
    busy_js = ("document.getElementById('rescan').classList.add('is-busy');"
               "document.querySelector('#rescan .rescanlab').textContent = 'Rescanning...';") if busy else ""
    page = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width">
<style>%s</style></head><body><div id="page" class="body bots"><div style="position:relative;height:400px" id="slot"></div></div>
<pre id=out></pre><script>window.fetch = () => new Promise(() => {});</script><script>%s</script><script>
const res = {};
try {
  document.getElementById('slot').innerHTML = botsHeaderShell();
  const m = document.getElementById('botmenu'); m.hidden = false; %s
  const r = id => document.getElementById(id).getBoundingClientRect();
  const sep = m.querySelector('li.sep').getBoundingClientRect();
  res.above = Math.round(sep.top - r('rescan').bottom);
  res.below = Math.round(r('revokeall').top - sep.bottom);
} catch (e) { res.err = String(e); }
document.getElementById('out').textContent = JSON.stringify(res);
</script></body></html>""" % (css, js.replace("</script", "<\\/script"), busy_js)
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "m.html")
        open(p, "w", encoding="utf-8").write(page)
        out = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=%d,900" % width,
                              "--dump-dom", "file://" + p], capture_output=True, text=True, timeout=120).stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return json.loads(html.unescape(re.search(r'<pre id="out">(.*?)</pre>', out, re.S).group(1)))


@unittest.skipUnless(CHROME, "needs Chrome")
class MenuGap(unittest.TestCase):
    def test_divider_spacing_is_even(self):
        for width in (1280, 390):
            for busy in (False, True):
                got = measure(width, busy)
                self.assertNotIn("err", got)
                self.assertEqual(got["above"], got["below"], (width, busy, got))
                self.assertLessEqual(got["below"], 8, (width, busy, got))


if __name__ == "__main__":
    unittest.main()
