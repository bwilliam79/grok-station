#!/usr/bin/env python3
"""0.13.11 Noor design QA batch (CONDITIONAL PASS on b71eb1c): sent-row buttons, phone Details list,
chip+Edit units, Updates rule line, off switch edge, Set up aria and focus. Node runs the real app.js functions."""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gs1311_bots_merge import JS, CSS, FAKE_DOM, _fn, _run, SETUP, OPTS, _row  # noqa: E402
import subprocess  # noqa: E402

DET = ("setupDetailsItems", "setupDetailsEl")


def _media(width):
    i = CSS.index("@media (max-width:%dpx){" % width)
    depth, j = 0, CSS.index("{", i)
    while True:
        c = CSS[j]
        depth += c == "{"
        depth -= c == "}"
        if not depth:
            return CSS[i:j]
        j += 1


class SentRowButtons(unittest.TestCase):
    def test_order_and_same_kind(self):
        a = _run(SETUP, "setupRowActionHTML(%s, %s)" % (json.dumps(_row(status="sent", sent_at=900)), json.dumps(OPTS)))
        btns = re.findall(r'<button class="([^"]*)"[^>]*>([^<]*)</button>', a)
        self.assertEqual([t for _, t in btns], ["Paste values", "Resend"])
        self.assertEqual({c for c, _ in btns}, {"btn sm sec"})
        self.assertNotIn("tlnk", a)

    def test_remove_follows_and_matches_size(self):
        self.assertIn('${su ? su.action : ""}\n        <button class="btn g sm${su && /setacts sent/.test(su.action) ? " sec" : ""}" type="button" data-remove=', JS)
        self.assertIn(".botrow .cfg .btn.sec{min-width:112px;height:38px", CSS)


class DetailsList(unittest.TestCase):
    def test_details_rows_built_with_dom(self):
        got = _run(DET, "dump(setupDetailsEl(%s, false))" % json.dumps(_row(name="Pip", wake="Hey Pip", slug="pip")), dom=True)
        self.assertEqual(got["cls"], "setdetails")
        self.assertEqual(got["kids"][0]["text"], "Details")
        rows = got["kids"][1]["kids"]
        self.assertEqual([r["kids"][0]["text"] for r in rows],
                         ["Computer \u00b7 Office computer", "Station name \u00b7 Pip", "Wake word \u00b7 Hey Pip"])
        self.assertEqual([r["kids"][1]["attrs"]["data-edit"] for r in rows], ["computer", "name", "wake"])
        self.assertEqual({r["kids"][1]["text"] for r in rows}, {"Edit"})

    def test_bot_text_stays_text(self):
        got = _run(DET, "dump(setupDetailsEl(%s, false))" % json.dumps(_row(name="<b>x</b>", computer="<i>y</i>")), dom=True)
        self.assertEqual(got["kids"][1]["kids"][0]["kids"][0]["text"], "Computer \u00b7 <i>y</i>")

    def test_panel_has_slot_and_paint_fills_it(self):
        p = _run(SETUP, "setupRowPanelHTML(%s, %s)" % (json.dumps(_row()), json.dumps(OPTS)))
        self.assertIn('data-detfor="otto"', p)
        self.assertLess(p.index("</ol>"), p.index("data-detfor"))
        self.assertIn("fillSetupDetails(list, rows);", _fn("bindSetupRows"))

    def test_phone_only_and_44px(self):
        ph = _media(599)
        self.assertIn(".setdetails{display:block", ph)
        self.assertIn("min-height:44px", ph[ph.index(".setdetails li{"):])
        self.assertIn(".steps .chipedit .tlnk{display:none}", ph)
        self.assertIn(".setdetails{display:none}", CSS)


class ChipEditUnit(unittest.TestCase):
    def test_chip_and_edit_one_nowrap_unit(self):
        m = _run(("setupMark",), "setupMark('Office computer', 'computer', false)")
        self.assertTrue(m.startswith('<span class="chipedit"><mark>Office computer</mark> <button'))
        self.assertTrue(m.endswith(">Edit</button></span>"))
        self.assertIn(".chipedit{display:inline-flex;align-items:center;gap:4px;white-space:nowrap}", CSS)

    def test_edit_hit_area_24(self):
        self.assertIn(".tlnk[data-edit]{display:inline-flex;align-items:center;justify-content:center;min-width:24px;min-height:24px;padding:5px 4px;margin:-5px 0", CSS)


class UpdatesLine(unittest.TestCase):
    def test_rule_line_not_clickable(self):
        self.assertIn(".updsw .updrule{color:var(--gs-muted);text-decoration:none;cursor:default}", CSS)

    def test_off_track_edge(self):
        self.assertIn(".tog:not(.on){border-color:var(--gs-field-edge)}", CSS)


class PhoneRows(unittest.TestCase):
    def test_connected_row_stacks(self):
        ph = _media(599)
        self.assertIn('grid-template-areas:"av nm nm" "av l2 ww" "cf cf cf"', ph)
        self.assertIn("#blist .botrow .cfg .btn{display:inline-flex;min-height:44px", ph)
        self.assertIn("flex-wrap:wrap", ph[ph.index("#blist .botrow .lbl{"):])
        self.assertIn('<span class="nm">${esc(b.name)}</span>', JS)


class SetupAriaFocus(unittest.TestCase):
    def test_aria_on_set_up(self):
        for open_, arrow in ((False, "\u25be"), (True, "\u25b4")):
            a = _run(SETUP, "setupRowActionHTML(%s, %s)" % (json.dumps(_row()), json.dumps(dict(OPTS, expanded=open_))))
            self.assertIn('aria-expanded="%s"' % ("true" if open_ else "false"), a)
            self.assertIn('aria-controls="setp-otto"', a)
            self.assertIn("Set up " + arrow, a)
        p = _run(SETUP, "setupRowPanelHTML(%s, %s)" % (json.dumps(_row()), json.dumps(OPTS)))
        self.assertIn('id="setp-otto"', p)

    def test_focus_returns_on_cancel_and_esc(self):
        prog = (r"""
let focused = null, painted = 0;
const btn = {focus(){ focused = 'setup:' + this.k; }, k: 'otto'};
const document = {querySelector: s => s === '[data-setupbtn="otto"]' ? btn : null};
function paintBots(){ painted++; }
const handlers = {};
function mk(attr, val){ return {getAttribute: a => a === attr ? val : null, set onclick(f){ handlers[attr] = f; }, set onkeydown(f){ handlers.key = f; }}; }
const list = {querySelectorAll: sel => sel === '[data-setcancel]' ? [mk('data-setcancel','otto')] :
  sel === '.setpanel' ? [mk('data-slug','otto')] : []};
""" + _fn("setupToggle") + _fn("bindSetupRows").replace("  fillSetupDetails(list, rows);\n", "") + r"""
const el = {_setupOpen: 'otto', _bots: []};
bindSetupRows(list, el, {});
handlers['data-setcancel']({stopPropagation(){}});
const a = [focused, el._setupOpen];
focused = null; el._setupOpen = 'otto';
let prevented = false;
handlers.key({key: 'Escape', preventDefault(){ prevented = true; }});
process.stdout.write(JSON.stringify([a, [focused, el._setupOpen, prevented]]));
""")
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [["setup:otto", None], ["setup:otto", None, True]])


if __name__ == "__main__":
    unittest.main()
