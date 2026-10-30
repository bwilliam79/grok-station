#!/usr/bin/env python3
"""0.13.9 fix round item 7 (Noor gs-fixround-0139 rev1): the System page is grouped.

Status (open), Password and security, Troubleshooting (collapsed <details>), then an always-visible Danger zone
with Reboot and Run setup again. Template and CSS only: every id the handlers bind is still there exactly once.
Fails on 0.13.8 (six flat cards, no groups).
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()


def _page():
    i = JS.index("async function pageSystem(el) {")
    return JS[i:JS.index("\nfunction pageMore(", i)]


class SystemGroups(unittest.TestCase):
    def test_group_order_and_open_state(self):
        p = _page()
        order = [p.index(x) for x in ('sysGroup("g-status"', 'sysGroup("g-sec"', 'sysGroup("g-trouble"', 'class="danger sysdanger"')]
        self.assertEqual(order, sorted(order))
        self.assertIn('sysGroup("g-status", "Status", "CPU, memory, disk, services, network", `<div id="hl"></div>`, true)', p)
        self.assertRegex(p, re.compile(r'sysGroup\("g-sec", .*?`, false\)', re.S))
        self.assertRegex(p, re.compile(r'sysGroup\("g-trouble", .*?`, false\)', re.S))

    def test_every_id_kept_once(self):
        p = _page()
        for i in ("hl", "tls", "lp", "ls", "lr", "ld", "log", "rv", "rb", "again", "se", "pw", "old", "pgo", "pe", "gracecard"):
            self.assertEqual(len(re.findall(r'id="%s"' % i, p)), 1, i)
        for h in ('$("#again", el).onclick', '$("#rv", el).onclick', '$("#rb", el).onclick', '$("#lr", el).onclick = logs'):
            self.assertIn(h, p)

    def test_sections_hold_the_right_controls(self):
        p = _page()
        sec = p[p.index('sysGroup("g-sec"'):p.index('sysGroup("g-trouble"')]
        for i in ('id="pw"', 'id="gracecard"', 'id="tls"'):
            self.assertIn(i, sec)
        tr = p[p.index('sysGroup("g-trouble"'):p.index('class="danger sysdanger"')]
        for i in ('id="lp"', 'id="rv"', "Restarts the voice listener only. The screen and settings stay as they are."):
            self.assertIn(i, tr)
        dz = p[p.index('class="danger sysdanger"'):]
        self.assertIn('<button class="btn dg" id="rb">Reboot station…</button>', dz)
        self.assertIn('<button class="btn dg" id="again">Run setup again</button>', dz)
        self.assertIn("Back in about a minute. A conversation in progress is cut off.", dz)
        # Checked against the code: Run setup again only rewrites wizard.json (grok_admin api_post_wizard).
        self.assertIn("Opens the setup steps again. Your bots and settings are kept until you change them.", dz)

    def test_health_has_no_second_heading(self):
        self.assertNotIn('$("#hl", el).innerHTML = `<h2>Health</h2>', JS)

    def test_css(self):
        flat = re.sub(r"\s+", "", CSS)
        self.assertIn(".sysgrp>summary{list-style:none;display:flex;align-items:center;gap:var(--sp-3);min-height:56px", flat)
        self.assertIn(".sysgrp>summary:focus-visible{outline:2pxsolidvar(--gs-teal);outline-offset:2px}", flat)
        self.assertIn(".sysdanger.drow2.btn{min-height:44px;width:100%}", flat)
        self.assertNotIn("\n.body{overflow:visible}\n", CSS)


if __name__ == "__main__":
    unittest.main()
