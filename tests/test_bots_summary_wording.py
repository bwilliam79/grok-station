"""Bots page: no visible bot count above the list (gs-configure-all rev3.6 #1, 2026-10-30).

The count moves to the list's aria-label "Bots, N" (N counts Grok too). The old '#bsum' line and its spacing
are gone; the row that held it only shows while the step-up unlock note is up.
Before 0.13.9 these fail: paintSummary wrote "11 bots" into a visible #bsum line.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

APP = (pathlib.Path(__file__).resolve().parents[1] / "admin" / "web" / "app.js").read_text()


def _fn(name):
    start = APP.index("\nfunction " + name + "(")
    return APP[start:APP.find("\nfunction ", start + 1)]


def test_no_visible_count_line():
    assert 'id="bsum"' not in APP
    assert '<div class="bsumrow inline" hidden>' in APP
    assert '<div id="blist" role="group" aria-label="Bots"></div>' in APP


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_list_label_carries_the_count():
    prog = """
    const attrs = {};
    const list = {setAttribute: (k, v) => { attrs[k] = v; }};
    const $ = (sel) => sel === "#blist" ? list : null;
    """ + _fn("paintSummary") + """
    paintSummary({}, new Array(10).fill({}));
    const ten = attrs["aria-label"];
    paintSummary({}, []);
    process.stdout.write(JSON.stringify([ten, attrs["aria-label"]]));
    """
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == ["Bots, 11", "Bots, 1"]


def test_unlock_note_row_follows_the_note():
    assert 'n.parentNode.classList.contains("bsumrow")) n.parentNode.hidden = !t;' in _fn("noteStepup")
