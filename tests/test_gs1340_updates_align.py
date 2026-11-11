"""0.13.40: the Updates switch shares the wake switch's vertical center.

The Updates label and switch sat about 8px higher than the wake-word switch in the
same row: the group was top-aligned inside a cell that also reserved a caption line,
while the wake switch and Configure were centered. The row, the Updates cell, and
the label group use align-items:center. No margin nudge.

The center check renders the row through chrome_harness (20s). It skips when Chrome
hangs or is missing, unless GS_REQUIRE_CHROME=1.
"""
import json
import os
import re
import shutil
import tempfile
import unittest

from chrome_harness import find_chrome, run_chrome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="{css}">
<style>html,body{{overflow:auto;width:{width}px}} #blist{{width:{inner}px}}</style>
</head><body>
<div id="blist">
  <div class="botrow"><div class="avwrap"></div>
    <div><div class="lbl"><span class="nm">Ash</span></div><div class="l2"><span class="help">Connected</span></div></div>
    <div class="ww"><span class="phrase">“Hey Ash”</span><button class="tog on" type="button"></button></div>
    <div class="updcol"><span class="updslot"><span class="updsw"><button class="updbtn" type="button"><span class="tog on"></span><span>Updates</span></button><span class="updrule"></span></span></span></div>
    <div class="cfg"><button class="btn sm" type="button">Configure</button></div></div>
  <div class="botrow"><div class="avwrap"></div>
    <div><div class="lbl"><span class="nm">Juniper Hollow</span></div><div class="l2"><span class="help">Connected · replies from the den</span></div></div>
    <div class="ww"><span class="phrase">“Hey Juniper Hollow”</span><button class="tog on" type="button"></button></div>
    <div class="updcol"><span class="updslot"><span class="updsw"><button class="updbtn" type="button"><span class="tog on"></span><span>Updates</span></button><span class="updrule"></span></span></span></div>
    <div class="cfg"><button class="btn sm" type="button">Configure</button></div></div>
</div>
<script>
function mid(el) {{ const r = el.getBoundingClientRect(); return (r.top + r.bottom) / 2; }}
const rows = [...document.querySelectorAll("#blist .botrow")].map(row => {{
  const wake = row.querySelector(":scope > .ww .tog");
  const upd = row.querySelector(":scope > .updcol .tog");
  return {{wake: mid(wake), upd: mid(upd), d: mid(upd) - mid(wake)}};
}});
document.documentElement.setAttribute("data-measure", JSON.stringify(rows));
</script>
</body></html>
"""


def _rule(selector):
    needle = selector + "{"
    start = CSS.find(needle)
    if start < 0:
        return ""
    return CSS[start:CSS.find("}", start)]


def measure(width, inner):
    chrome = find_chrome()
    if not chrome:
        raise unittest.SkipTest("google-chrome is not installed")
    tmp = tempfile.mkdtemp(prefix="gs-upd-align-")
    try:
        html = os.path.join(tmp, "row.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write(PAGE.format(css=os.path.join(ROOT, "admin", "web", "admin.css"), width=width, inner=inner))
        text = run_chrome(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", "--user-data-dir=" + os.path.join(tmp, "chrome"),
             "--window-size=%d,900" % width, "--virtual-time-budget=1500", "--dump-dom",
             "file://" + html])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    m = re.search(r'data-measure="([^"]+)"', text)
    if not m:
        raise AssertionError("layout measurement did not render")
    return json.loads(m.group(1).replace("&quot;", '"'))


class UpdatesSwitchCenters(unittest.TestCase):
    def test_css_centers_the_row_the_cell_and_the_label_group(self):
        self.assertIn(
            "#blist .botrow{display:grid;grid-template-columns:[bot] minmax(240px,1fr) [wake] 200px [upd] 144px [act] 120px;grid-template-areas:none;column-gap:16px;row-gap:0;align-items:center;",
            CSS)
        cell = _rule("#blist .botrow>.updcol")
        self.assertIn("display:flex", cell)
        self.assertIn("align-items:center", cell)
        label = _rule(".updbtn")
        self.assertIn("display:inline-flex", label)
        self.assertIn("align-items:center", label)
        # The caption line must not be a second grid row that lifts the switch.
        group = _rule(".updcol .updsw")
        self.assertNotIn("grid-template-rows:32px 16px", group)
        self.assertNotIn("margin-top", cell)
        self.assertNotIn("margin-top", label)

    def test_switch_centers_match_on_every_row(self):
        rows = measure(1280, 1100)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertLessEqual(abs(row["d"]), 1, row)


if __name__ == "__main__":
    unittest.main()
