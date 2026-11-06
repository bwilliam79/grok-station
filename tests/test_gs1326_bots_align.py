#!/usr/bin/env python3
"""0.13.26: Bots-row switches share the trailing edge of their column (bq-0065).

The wake column and the Updates column are fixed tracks. The switch used to sit
immediately after the label, so "Hey Ash" and "Hey Hadley" put it at different x.
The switch, and Train wake word, stay on the column's trailing edge whatever the label.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_PATH = os.path.join(ROOT, "admin", "web", "admin.css")
CSS = open(CSS_PATH, encoding="utf-8").read()

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="{css}">
<style>html,body{{overflow:auto;width:1280px}} #blist{{width:1100px}}</style>
</head><body>
<div id="blist">
  <div class="botrow"><div class="avwrap"></div>
    <div><div class="lbl"><span class="nm">Ash</span></div></div>
    <div class="ww"><span class="ico teal">~</span><span class="phrase">“Hey Ash”</span><button class="tog on" type="button"></button></div>
    <div class="updcol"><span class="updslot"><span class="updsw"><button class="updbtn" type="button"><span class="tog on"></span><span>Updates</span></button></span></span></div>
    <div class="cfg"><button class="btn sm" type="button">Configure</button></div></div>
  <div class="botrow"><div class="avwrap"></div>
    <div><div class="lbl"><span class="nm">Hadley</span></div></div>
    <div class="ww"><span class="ico teal">~</span><span class="phrase">“Hey Hadley”</span><button class="tog on" type="button"></button></div>
    <div class="updcol"><span class="updslot"><span class="updsw"><button class="updbtn" type="button"><span class="tog on"></span><span>Updates</span></button></span></span></div>
    <div class="cfg"><button class="btn sm" type="button">Configure</button></div></div>
  <div class="botrow"><div class="avwrap"></div>
    <div><div class="lbl"><span class="nm">Miles</span></div></div>
    <div class="ww"><span class="ico teal">~</span><span class="phrase">“Hey Miles”</span><button class="tog on" type="button"></button></div>
    <div class="updcol"><span class="updslot"><span class="updsw"><button class="updbtn" type="button"><span class="tog on"></span><span>Updates</span></button></span></span></div>
    <div class="cfg"><button class="btn sm" type="button">Configure</button></div></div>
  <div class="botrow"><div class="avwrap"></div>
    <div><div class="lbl"><span class="nm">Finn</span></div></div>
    <div class="ww"><button class="btn sm wwtrain" type="button">+ Train wake word</button></div>
    <div class="updcol"><span class="updslot"><span class="updsw"><button class="updbtn" type="button"><span class="tog on"></span><span>Updates</span></button></span></span></div>
    <div class="cfg"><button class="btn sm" type="button">Configure</button></div></div>
</div>
<script>
function box(el) {{ const r = el.getBoundingClientRect(); return {{l: r.left, r: r.right}}; }}
const rows = [...document.querySelectorAll("#blist .botrow")].map(row => {{
  const ww = row.querySelector(":scope > .ww");
  const upd = row.querySelector(":scope > .updcol");
  const wake = ww.querySelector(".tog") || ww.querySelector(".wwtrain");
  const phrase = ww.querySelector(".phrase");
  return {{
    ww: box(ww), wake: box(wake),
    phrase: phrase ? box(phrase) : null,
    upd: box(upd), us: box(upd.querySelector(".tog"))
  }};
}});
document.documentElement.setAttribute("data-measure", JSON.stringify(rows));
</script>
</body></html>
"""


def measure_rows():
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        raise unittest.SkipTest("google-chrome is not installed")
    tmp = tempfile.mkdtemp(prefix="gs-bots-align-")
    try:
        html = os.path.join(tmp, "row.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write(PAGE.format(css=CSS_PATH))
        out = os.path.join(tmp, "dom.html")
        udd = os.path.join(tmp, "chrome")
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(
                ["timeout", "12", chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--disable-dev-shm-usage", "--user-data-dir=" + udd, "--window-size=1280,900",
                 "--virtual-time-budget=1500", "--dump-dom", "file://" + html],
                stdout=fh, stderr=subprocess.DEVNULL, check=False)
        with open(out, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    m = re.search(r'data-measure="([^"]+)"', text)
    if not m:
        raise AssertionError("layout measurement did not render")
    return json.loads(m.group(1).replace("&quot;", '"'))


class BotsSwitchEdge(unittest.TestCase):
    def test_css_pins_the_switch_to_the_column_end(self):
        self.assertIn(
            "#blist .botrow>.ww>.tog,#blist .botrow>.ww>.wwtrain{margin-left:auto;flex:none}",
            CSS)
        self.assertIn(
            "#blist .botrow>.updcol .updbtn{flex-direction:row-reverse;width:100%;padding-right:0;margin-left:0;margin-right:0}",
            CSS)
        # Column tracks stay the 0.13.15 sizes. This pass does not move them.
        self.assertIn(
            "#blist .botrow{display:grid;grid-template-columns:[bot] minmax(240px,1fr) [wake] 200px [upd] 144px [act] 120px;",
            CSS)

    def test_switch_right_edge_ignores_label_length(self):
        rows = measure_rows()
        self.assertEqual(len(rows), 4)
        wake_right = {round(r["wake"]["r"]) for r in rows}
        wake_col = {round(r["ww"]["r"]) for r in rows}
        upd_right = {round(r["us"]["r"]) for r in rows}
        upd_col = {round(r["upd"]["r"]) for r in rows}
        self.assertEqual(len(wake_right), 1, rows)
        self.assertEqual(wake_right, wake_col, rows)
        self.assertEqual(len(upd_right), 1, rows)
        self.assertEqual(upd_right, upd_col, rows)
        for row in rows:
            if row["phrase"]:
                self.assertLessEqual(row["phrase"]["r"], row["wake"]["l"] + 1, row)


if __name__ == "__main__":
    unittest.main()
