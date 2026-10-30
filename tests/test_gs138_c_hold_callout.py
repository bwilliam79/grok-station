#!/usr/bin/env python3
"""Noor option C (0.13.8): the Configure all / Set up callout waits for 0.13.9's "Set up bots" list.

0.13.8 renders no setup callout for 1 or 2+ bots not set up, nothing sets a bot up automatically,
the other callouts keep their order, and the Not set up rows keep Set up and Remove.
"""
import json
import os
import re
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gs138_bots_header import JS, _fn, _run  # noqa: E402

MAIN = [{"slug": "w", "name": "Wren", "main": True}]
BEHIND = [{"slug": "w", "name": "Wren"}]


def _html(bots, pending, behind=None):
    return _run("botsCalloutHTML(%s)" % json.dumps({"bots": bots, "pending": pending, "behind": behind or []}))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class HoldSetupCallout(unittest.TestCase):
    def test_no_callout_for_one_bot_not_set_up(self):
        self.assertEqual(_html(MAIN, [{"name": "Otto"}]), "")

    def test_no_callout_for_two_or_more(self):
        self.assertEqual(_html(MAIN, [{"name": "Otto"}, {"name": "Pip"}, {"name": "Maple"}]), "")

    def test_no_automatic_setup_path(self):
        self.assertNotIn("function openConfigureAll(", JS)
        self.assertNotIn("cfgall", JS)
        self.assertNotIn("Configure all", JS)
        # addBot has exactly two callers: a row's Set up click and the Add bot button.
        callers = [JS[JS.rfind("\n", 0, m.start()) + 1:JS.find("\n", m.start())].strip()
                   for m in re.finditer(r"\baddBot\(", JS)]
        callers = [c for c in callers if not c.startswith("function addBot(")]
        self.assertEqual(callers, ["if (row) addBot(el._voices || [], el, {name: row.name});",
                                   "const openAdd = () => addBot(el._voices || data.voices || [], el);"])
        self.assertIn("if (addBtn) addBtn.onclick = openAdd;", JS)
        self.assertNotIn("openAdd()", JS)

    def test_other_callouts_keep_their_order(self):
        self.assertIn("Add your main bot first", _html([], [{"name": "Otto"}, {"name": "Pip"}]))
        upd = _html(MAIN, [{"name": "Otto"}, {"name": "Pip"}], BEHIND)
        self.assertIn("1 bots need an update", upd)
        self.assertIn('id="updallbtn"', upd)
        self.assertEqual(_html(MAIN, []), "")

    def test_not_set_up_rows_keep_set_up_and_remove(self):
        self.assertIn('<button class="btn sm" type="button" data-setup="${esc(row.key)}">Set up</button>', JS)
        self.assertIn('<button class="btn g sm" type="button" data-remove="${esc(row.key)}">Remove</button>', JS)
        self.assertIn('list.querySelectorAll("[data-setup]").forEach(b => b.onclick = ev => {', JS)
        self.assertIn('list.querySelectorAll("[data-remove]").forEach(b => b.onclick = ev => {', JS)

    def test_team_setup_block_stays_on_main_bot_more_tab(self):
        self.assertIn("${(bot.main && pendingTeammates(el).length) ? `<h3>Team setup</h3>", JS)


if __name__ == "__main__":
    unittest.main()
