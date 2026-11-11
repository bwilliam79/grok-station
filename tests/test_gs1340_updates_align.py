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

from chrome_harness import find_chrome, quiet_css, run_chrome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><link rel=icon href="data:,">
<style>__CSS__
html,body{overflow:auto;width:__WIDTH__px} #blist{width:__INNER__px}</style>
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
    <div class="updcol"><span class="updslot"><span class="updsw"><button class="updbtn" type="button"><span class="tog on"></span><span>Updates</span></button><span class="updrule">2 behind</span></span></span></div>
    <div class="cfg"><button class="btn sm" type="button">Configure</button></div></div>
</div>
<script>
function mid(el) { const r = el.getBoundingClientRect(); return (r.top + r.bottom) / 2; }
const rows = [...document.querySelectorAll("#blist .botrow")].map(row => {
  const wake = row.querySelector(":scope > .ww .tog");
  const upd = row.querySelector(":scope > .updcol .tog");
  const cap = row.querySelector(".updrule");
  return {wake: mid(wake), upd: mid(upd), d: mid(upd) - mid(wake),
    h: row.getBoundingClientRect().height, cap: cap ? cap.textContent : ""};
});
document.documentElement.setAttribute("data-measure", JSON.stringify(rows));
</script>
</body></html>
"""


def _outside_desktop(css):
    """CSS with every @media (min-width:768px) block removed."""
    needle = "@media (min-width:768px){"
    out, i = [], 0
    while True:
        j = css.find(needle, i)
        if j < 0:
            out.append(css[i:])
            break
        out.append(css[i:j])
        k, depth = j + len(needle) - 1, 0
        while k < len(css):
            if css[k] == "{":
                depth += 1
            elif css[k] == "}":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        i = k
    return "".join(out)


def _rule(selector):
    """The first rule whose selector is exactly `selector` (not a longer selector that ends with it)."""
    needle = selector + "{"
    start = 0
    while True:
        start = CSS.find(needle, start)
        if start < 0:
            return ""
        if start == 0 or CSS[start - 1] in "{;\n \t":
            return CSS[start:CSS.find("}", start)]
        start += 1


def measure(width, inner):
    chrome = find_chrome()
    if not chrome:
        raise unittest.SkipTest("google-chrome is not installed")
    tmp = tempfile.mkdtemp(prefix="gs-upd-align-")
    try:
        html = os.path.join(tmp, "row.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write(PAGE.replace("__CSS__", quiet_css(CSS)).replace("__WIDTH__", str(width)).replace("__INNER__", str(inner)))
        text = run_chrome(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", "--disable-background-networking",
             "--window-size=%d,900" % width, "--dump-dom", "file://" + html])
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
        # The 1fr stack is desktop only. The base rule stays 32px over 16px, which is what the phone inherits.
        group = _rule(".updcol .updsw")
        self.assertIn("grid-template-rows:32px 16px", group)
        self.assertNotIn("minmax(0,1fr)", group)
        desktop = _rule("#blist .botrow>.updcol .updsw")
        self.assertIn("grid-template-rows:minmax(0,1fr) auto minmax(0,1fr)", desktop)
        self.assertNotIn(
            "grid-template-rows:minmax(0,1fr) auto minmax(0,1fr)",
            _outside_desktop(CSS))
        self.assertNotIn("margin-top", cell)
        self.assertNotIn("margin-top", label)

    def test_switch_centers_match_on_every_row(self):
        rows = measure(1280, 1100)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["cap"], "2 behind")
        for row in rows:
            self.assertLessEqual(abs(row["d"]), 1, row)
            self.assertEqual(round(row["h"]), 81, row)


if __name__ == "__main__":
    unittest.main()
