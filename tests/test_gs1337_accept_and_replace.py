#!/usr/bin/env python3
"""0.13.37 regression: Accept is obvious when a pairing waits, and Set up asks before it replaces a pending request.

At 0.13.36 a posted key showed a small Accept button in the panel and nothing on the row itself, and "Send request
again" (or a new Set up) replaced a pending request without a word. These run the real app.js in headless Chrome.
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gs1336_repaint_chrome as H  # noqa: E402

REVIEW = """
el._setup = {owner: '', rows: {otto: {status: 'sent', relay: 'review', relay_fp: '0123456789abcdef', paired_computer: 'den-mac', name: 'Otto', sent_at: 1}}};
paintBots(el, bots);
const acc = L().querySelector('[data-pair="accept"][data-slug="otto"]');
res.acceptText = acc ? acc.textContent : null;
res.acceptCls = acc ? acc.className : null;
res.badge = !!L().querySelector('.setpanel.is-review .needact');
res.rowBtn = (L().querySelector('[data-gotoaccept="otto"]') || {}).textContent || null;
res.status = L().textContent.indexOf('Action needed') >= 0;
const go = L().querySelector('[data-gotoaccept="otto"]');
if (go) go.click();
res.focus = document.activeElement && document.activeElement.getAttribute('data-pair');
"""

# Copy fixes on 0.13.37 review (after 0ac0394): no empty Computer block, and no helper line under the review row.
COPY = """
const one = (pc) => {
  el._setup = {owner: '', rows: {otto: {status: 'sent', relay: 'review', relay_fp: '0123456789abcdef', paired_computer: pc, name: 'Otto', sent_at: 1}}};
  paintBots(el, bots);
  const panel = L().querySelector('.setpanel.is-review');
  const labs = panel ? Array.from(panel.querySelectorAll('.pairlab')).map(p => p.textContent) : null;
  const helps = Array.from(L().querySelectorAll('.shelp')).map(p => p.textContent);
  return {labs: labs, panelText: panel ? panel.textContent : '', helps: helps,
          status: L().textContent.indexOf('Action needed \u00b7 Otto\u2019s key is waiting for you to accept') >= 0,
          rowBtn: !!L().querySelector('[data-gotoaccept="otto"]'), empties: panel ? Array.from(panel.querySelectorAll('p')).filter(p => !p.textContent.trim()).length : -1};
};
res.unknown = one('');
res.known = one('den-mac');
"""

REPLACE = """
window.answer = (url, opt) => {
  if (url.indexOf('setup-send') >= 0) return [200, {setup: el._setup}];
  return [200, {}];
};
el._setup = {owner: '', rows: {otto: {status: 'sent', relay: %r, name: 'Otto', sent_at: 1}}};
paintBots(el, bots);
const again = L().querySelector('[data-act="again"][data-slug="otto"]');
res.hasAgain = !!again;
if (again) again.click();
await tick(50);
res.sendsBefore = window.calls.filter(c => c.url.indexOf('setup-send') >= 0).length;
const sheet = document.querySelector('.sheet');
res.sheet = sheet ? sheet.querySelector('h2').textContent : null;
res.sheetText = sheet ? sheet.querySelector('.sub').textContent : null;
if (sheet) sheet.querySelector('#cfgo').click();
await tick(50);
res.sendsAfter = window.calls.filter(c => c.url.indexOf('setup-send') >= 0).length;
"""

FRESH = """
window.answer = (url, opt) => [200, {setup: el._setup}];
el._setup = {owner: '', rows: {otto: {status: 'sent', relay: 'expired', name: 'Otto', sent_at: 1}}};
paintBots(el, bots);
const again = L().querySelector('[data-act="again"][data-slug="otto"]');
if (again) again.click();
await tick(50);
res.sheet = !!document.querySelector('.sheet');
res.sends = window.calls.filter(c => c.url.indexOf('setup-send') >= 0).length;
"""


@unittest.skipUnless(H.CHROME, "needs Chrome")
class Accept(unittest.TestCase):
    def test_accept_is_prominent_and_reachable_from_the_row(self):
        r = H.chrome(REVIEW)
        self.assertEqual(r["acceptText"], "Accept key")
        self.assertIn("btn-accept", r["acceptCls"])
        self.assertNotIn(" sm", " " + r["acceptCls"])
        self.assertTrue(r["badge"])
        self.assertEqual(r["rowBtn"], "Accept key \u25be")
        self.assertTrue(r["status"])
        self.assertEqual(r["focus"], "accept")

    def test_unknown_computer_hides_the_computer_block(self):
        r = H.chrome(COPY)
        self.assertEqual(r["unknown"]["labs"], ["Fingerprint"])
        self.assertNotIn("Computer", r["unknown"]["panelText"])
        self.assertEqual(r["unknown"]["empties"], 0)
        self.assertEqual(r["known"]["labs"], ["Fingerprint", "Computer"])
        self.assertIn("den-mac", r["known"]["panelText"])

    def test_review_row_has_no_helper_line(self):
        r = H.chrome(COPY)
        for k in ("unknown", "known"):
            self.assertTrue(r[k]["status"], k)
            self.assertTrue(r[k]["rowBtn"], k)
            self.assertEqual(r[k]["helps"], [], k)
            self.assertNotIn("Compare the fingerprint below", r[k]["panelText"], k)


@unittest.skipUnless(H.CHROME, "needs Chrome")
class Replace(unittest.TestCase):
    def test_send_again_asks_before_replacing_a_pending_request(self):
        for relay in ("awaiting_confirm", "relayed", "needs_details"):
            r = H.chrome(REPLACE % relay)
            self.assertTrue(r["hasAgain"], relay)
            self.assertEqual(r["sendsBefore"], 0, relay)
            self.assertEqual(r["sheet"], "Replace the pending request?", relay)
            self.assertTrue(r["sheetText"], relay)
            self.assertEqual(r["sendsAfter"], 1, relay)

    def test_nothing_pending_sends_without_asking(self):
        r = H.chrome(FRESH)
        self.assertFalse(r["sheet"])
        self.assertEqual(r["sends"], 1)


if __name__ == "__main__":
    unittest.main()
