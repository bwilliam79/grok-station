"""0.13.46: a bot row's Update button stays in the bot cell, and 768 px does not scroll sideways."""
import json
import os
import re
import shutil
import tempfile
import unittest

from chrome_harness import find_chrome, quiet_css, run_chrome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
ART = os.environ.get("GS_FRAME_DIR", "/opt/cursor/artifacts/gs1346")

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><link rel=icon href="data:,">
<style>__CSS__</style></head><body>
<div class="app"><aside class="side"><div class="brand"><b>Grok Station</b></div></aside>
<main class="main"><header class="top"><h1>Bots</h1></header><div class="body"><div id="blist">
  <div class="botrow hasmsg" data-slug="ash">
    <div class="avwrap"></div>
    <div><div class="lbl"><span class="nm">Ash</span></div>
      <div class="l2"><p class="bmsg is-fail"><span class="tx">Update failed: No reply within 10 minutes.</span></p></div>
      <div class="rowupd"><button class="btn sm" type="button" data-upd="ash">Update</button></div></div>
    <div class="ww"><span class="phrase">“Hey Ash”</span><button class="tog on" type="button" aria-label="Wake word for Ash"></button></div>
    <div class="updcol"><span class="updslot"><span class="updsw"><button class="updbtn" type="button" aria-label="Updates for Ash"><span class="tog on"></span><span>Updates</span></button></span></span></div>
    <div class="cfg"><button class="btn sm" type="button">Configure</button></div></div>
  <div class="botrow" data-slug="kai">
    <div class="avwrap"></div>
    <div><div class="lbl"><span class="nm">Kai</span></div>
      <div class="l2"><div class="help">Replies from the home computer</div></div></div>
    <div class="ww"><span class="phrase">“Hey Kai”</span><button class="tog on" type="button" aria-label="Wake word for Kai"></button></div>
    <div class="updcol"><span class="updslot"><span class="updsw"><button class="updbtn" type="button" aria-label="Updates for Kai"><span class="tog on"></span><span>Updates</span></button></span></span></div>
    <div class="cfg"><button class="btn sm" type="button">Configure</button></div></div>
</div></div></main></div>
<script>
function box(el){const r=el.getBoundingClientRect();return {l:Math.round(r.left),r:Math.round(r.right),t:Math.round(r.top),b:Math.round(r.bottom)};}
const rows=[...document.querySelectorAll("#blist .botrow")].map(row=>{
  const upd=row.querySelector(".rowupd .btn");
  const fail=row.querySelector(".bmsg");
  return {
    slug: row.getAttribute("data-slug"),
    bot: box(row.querySelector(":scope > div:nth-child(2)")),
    wake: box(row.querySelector(":scope > .ww")),
    cfg: box(row.querySelector(":scope > .cfg")),
    upd: upd ? box(upd) : null,
    fail: fail ? box(fail) : null,
    order: [...row.querySelectorAll("button")].map(b=>b.getAttribute("aria-label")||b.textContent.trim())
  };
});
const root=document.documentElement;
document.documentElement.setAttribute("data-measure", JSON.stringify({
  rows: rows,
  scroll: root.scrollWidth,
  client: root.clientWidth
}));
</script>
</body></html>
"""


def _render(width, height, shot):
    chrome = find_chrome()
    if not chrome:
        raise unittest.SkipTest("google-chrome is not installed")
    os.makedirs(ART, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="gs1346-bots-")
    try:
        html = os.path.join(tmp, "bots.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write(PAGE.replace("__CSS__", quiet_css(CSS)))
        argv = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                "--disable-dev-shm-usage", "--disable-background-networking",
                "--window-size=%d,%d" % (width, height), "--screenshot=" + shot,
                "--dump-dom", "file://" + html]
        text = run_chrome(argv)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    match = re.search(r'data-measure="([^"]+)"', text)
    if not match:
        raise AssertionError("bots layout did not render")
    return json.loads(match.group(1).replace("&quot;", '"'))


class BotsUpdateCell(unittest.TestCase):
    def test_update_stays_in_the_bot_cell_and_768_does_not_overflow(self):
        frames = {1280: (900, "bots-1280.png"), 768: (900, "bots-768.png"), 390: (844, "bots-390.png")}
        measured = {}
        for width, (height, name) in frames.items():
            measured[width] = _render(width, height, os.path.join(ART, name))
            self.assertTrue(os.path.isfile(os.path.join(ART, name)), name)
        wide = measured[1280]
        mid = measured[768]
        phone = measured[390]
        for view in (wide, mid):
            ash = view["rows"][0]
            kai = view["rows"][1]
            self.assertIsNotNone(ash["upd"])
            self.assertLess(ash["fail"]["b"], ash["upd"]["t"])
            self.assertLessEqual(ash["upd"]["r"], ash["bot"]["r"] + 2)
            self.assertEqual(ash["wake"]["l"], kai["wake"]["l"])
            self.assertEqual(ash["cfg"]["l"], kai["cfg"]["l"])
            self.assertEqual(ash["order"][0], "Update")
            self.assertIn("Configure", ash["order"])
            self.assertLess(ash["order"].index("Update"), ash["order"].index("Configure"))
        self.assertLessEqual(mid["scroll"], mid["client"] + 1, mid)
        self.assertEqual(phone["rows"][0]["order"][0], "Update")
        self.assertLessEqual(phone["scroll"], phone["client"] + 1, phone)
        flat = re.sub(r"\s+", "", CSS)
        self.assertIn(
            "[bot]minmax(0,1fr)[wake]minmax(0,140px)[upd]minmax(0,112px)[act]minmax(0,108px)",
            flat)


if __name__ == "__main__":
    unittest.main()
